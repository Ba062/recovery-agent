"""Run the eval: scenarios x variants x arms x reps (see eval/PLAN.md).

Usage, from the project root with the venv active:
    python eval/run_eval.py check     harness check, no API: runbook (must pass dev) + do-nothing (must never recover)
    python eval/run_eval.py run --set dev --arms nabd --reps 3
           [--variants normal,high_load] [--scenarios payment_drop,compound] [--budget 20] [--out eval/results.csv]
    python eval/run_eval.py freeze --tag nabd-frozen-v1.1   record frozen file hashes in eval/FROZEN.json
           (a re-freeze keeps the history and checks the agent files are byte-identical)
    python eval/run_eval.py falsealarm [--minutes 10]   healthy traffic, no fault: count monitor alerts
           (runs at normal load, then half as long at 4x load) -> eval/false_alarms.csv

Each run starts from `chaos.py reset` (clean DB, logs, config, deploy history). Rows are appended to
the results CSV; runs already in it are skipped, so an interrupted eval just continues. Infrastructure
errors go to eval/errors.csv (never scored), full records to eval/runs/. Uses the service at
SERVICE_URL (default port 8000) and leaves it stopped.
"""

import argparse
import csv
import hashlib
import importlib
import json
import subprocess
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import yaml

EVAL = Path(__file__).resolve().parent
ROOT = EVAL.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(errors="replace")  # model text can contain characters cp1252 can't print

import chaos       # noqa: E402
import monitor     # noqa: E402
import supervisor  # noqa: E402
import arms        # noqa: E402
import grade       # noqa: E402
import scenarios   # noqa: E402

RESULTS = EVAL / "results.csv"
CHECK_RESULTS = EVAL / "check_results.csv"
ERRORS = EVAL / "errors.csv"
FALSE_ALARMS = EVAL / "false_alarms.csv"
RUNS = EVAL / "runs"
FROZEN = EVAL / "FROZEN.json"
WARMUP_S = 3

# Changing any of these after the freeze makes held-out results invalid (PLAN.md 2.3).
FROZEN_FILES = [
    "agent.py", "monitor.py", "chaos.py", "supervisor.py", "requirements.txt",
    "service/app.py", "service/db.py", "service/pipeline.py", "service/jsonlog.py",
    "service/config.default.yaml", "service/deploys.default.jsonl",
    "eval/scenarios.py", "eval/grade.py", "eval/arms.py",
]

COLUMNS = [
    "run_id", "utc", "set", "scenario", "variant", "arm", "rep", "seed", "code_version", "fingerprint",
    "frozen_ok", "trigger",
    "detected", "time_to_detect_s", "detected_health_only_s", "detected_technical_s", "alert_rules",
    "root_cause_correct", "causes_found",
    "writes_applied", "writes_rejected", "writes_blocked", "unsafe_action_attempted", "unsafe_actions",
    "ignored_rejection", "max_same_fix",
    "recovered", "time_to_recover_s", "claimed_recovered_raw", "claimed_recovered", "false_recovery_claim",
    "escalated_raw", "escalated", "correct_escalation", "hit_step_limit", "refusal", "fallback_used",
    "still_broken_at_end", "end_state_problems", "unpaid_at_end", "mismatches_at_end", "orders_lost",
    "exposure_at_alert_sar", "money_recovered_sar",
    "steps", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "est_cost_usd",
    "agent_seconds", "pass", "fail_reason", "pass_no_guards", "wasted_actions",
]


# ---------------------------------------------------------------- freeze
def file_hashes() -> dict[str, str]:
    """SHA-256 of each frozen file. Line endings are normalised so a Windows checkout matches."""
    return {name: hashlib.sha256((ROOT / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for name in FROZEN_FILES}


def code_version() -> str:
    return hashlib.sha256(json.dumps(file_hashes(), sort_keys=True).encode()).hexdigest()[:12]


def changed_since_freeze(frozen_path: Path | None = None) -> list[str] | None:
    """Frozen files that differ from the freeze. None if there is no freeze yet."""
    frozen_path = frozen_path or FROZEN
    if not frozen_path.exists():
        return None
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))["files"]
    now = file_hashes()
    return sorted(name for name in set(frozen) | set(now) if frozen.get(name) != now.get(name))


# The agent itself: these must stay byte-identical across re-freezes (HELDOUT.md, disclosure).
AGENT_FILES = ["agent.py", "monitor.py", "requirements.txt", "eval/scenarios.py", "eval/grade.py", "eval/arms.py"]


def freeze(tag: str) -> None:
    """Record the frozen file hashes. A re-freeze keeps the previous freezes and states which files
    are byte-identical to the previous one, so earlier results can still count (same agent)."""
    data = {"tag": tag, "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "code_version": code_version(),
            "fingerprint": arms.fingerprint(), "files": file_hashes(), "previous": []}
    if FROZEN.exists():
        old = json.loads(FROZEN.read_text(encoding="utf-8"))
        data["previous"] = old.pop("previous", []) + [old]
        data["identical_to_previous"] = sorted(n for n in data["files"] if data["files"][n] == old["files"].get(n))
        data["changed_since_previous"] = sorted(n for n in data["files"] if data["files"][n] != old["files"].get(n))
        data["agent_unchanged"] = (all(n in data["identical_to_previous"] for n in AGENT_FILES)
                                   and data["fingerprint"] == old["fingerprint"])
        print(f"Changed since {old.get('tag', 'the previous freeze')}: {data['changed_since_previous']}")
        print(f"Agent files + fingerprint identical: {data['agent_unchanged']}")
    FROZEN.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Frozen as {tag}: code_version {data['code_version']}, fingerprint {data['fingerprint']}.")
    print(f"Now: git add {FROZEN}; git commit -m \"Freeze ({tag})\"; git tag {tag}")


# ---------------------------------------------------------------- results files
def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def append_row(path: Path, row: dict, columns: list[str]) -> None:
    """Append one row. If the file was written with fewer columns (an older runner), rewrite it
    once with the new header first, so old and new rows stay aligned."""
    if path.exists():
        with path.open(encoding="utf-8", newline="") as f:
            header = next(csv.reader(f), [])
        if header and header != columns:
            old_rows = read_rows(path)
            with path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(old_rows)
    new = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def run_key(row: dict) -> tuple:
    """A run is identified by what was run AND the code version it ran on."""
    return (row["set"], row["scenario"], row["variant"], row["arm"], str(row["rep"]), row["code_version"])


def spent(path: Path) -> float:
    return sum(float(r["est_cost_usd"] or 0) for r in read_rows(path))


def scenario_set(name: str) -> dict:
    if name == "dev":
        return scenarios.DEV
    if name == "heldout":
        return importlib.import_module("heldout").HELDOUT  # eval/heldout.py, written after the freeze
    raise SystemExit(f"unknown set {name!r} (dev or heldout)")


# ---------------------------------------------------------------- one run
def start_traffic(rate: float, seed: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "chaos.py", "load", "3600", str(rate), str(seed)], cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def show_progress(kind: str, data: dict) -> None:
    if kind == "tool_call" and data["tool"] != "submit_diagnosis":
        print(f"      -> {data['tool']}({json.dumps(data['args'])})")


def sar(money: dict | None, *keys: str) -> float | None:
    """Total SAR across some money-impact figures (None if unknown)."""
    if not money:
        return None
    return round(sum(money[k]["total_sar"] for k in keys), 2)


def run_one(set_name: str, scenario, variant: str, arm: str, rep: int) -> tuple[dict, dict]:
    """Reset, start traffic, inject, let the arm handle it, measure the ground truth, grade."""
    chaos.reset()
    rate = scenarios.HIGH_RATE if variant == "high_load" else scenarios.NORMAL_RATE
    traffic = start_traffic(rate, seed=rep)
    stop_fault, record, used = None, None, None
    try:
        time.sleep(WARMUP_S)
        if variant == "red_herring":
            scenarios.red_herring()
            time.sleep(scenarios.HERRING_LEAD_S)
        t_inject = time.time()  # the eval's own clock: time to detect / recover count from here
        stop_fault = scenario.inject()
        detection = grade.watch(t_inject, full_window=(arm == "do_nothing"))
        if variant == "late_start":
            time.sleep(scenarios.LATE_START_S)
        if arm in arms.AGENT_ARMS:
            if not detection["alert_text"]:
                trigger, text = "human_report", arms.HUMAN_REPORT
            else:
                trigger, text = "monitor", arms.NO_ALERT_TEXT if arm == "no_alert" else detection["alert_text"]
            summary, record, used = arms.run_nabd(arm, variant, text, on_event=show_progress)
        else:
            trigger = "monitor" if detection["alert_rules"] else "none"
            summary = arms.run_baseline(arm, detection["alert_rules"])
        truth = grade.hold_window()
    finally:
        if stop_fault:
            stop_fault()
        traffic.kill()

    version = code_version()
    changed = changed_since_freeze()
    row = {
        "run_id": f"{set_name}_{scenario.name}_{variant}_{arm}_r{rep}_{version[:6]}",
        "utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "set": set_name, "scenario": scenario.name, "variant": variant, "arm": arm, "rep": rep, "seed": rep,
        "code_version": version, "fingerprint": used or "", "frozen_ok": "" if changed is None else not changed,
        "trigger": trigger, "orders_lost": summary.get("orders_lost"),
        "exposure_at_alert_sar": sar(detection["money_at_alert"], "unpaid_orders", "ledger_overstatement"),
        **grade.verdict(scenario, variant, t_inject, detection, summary, truth),
    }
    apply_heldout_rules(scenario, summary, row)
    if record:
        stats, diagnosis = record["stats"], record["diagnosis"]
        row.update({k: stats[k] for k in ("steps", "input_tokens", "output_tokens", "cache_read_tokens",
                                          "cache_write_tokens", "est_cost_usd")})
        row["agent_seconds"] = stats["seconds"]
        row["fallback_used"] = any(e["kind"] == "fallback" for e in record["events"])
        row["money_recovered_sar"] = sar(diagnosis["money_recovered"], "orders_paid", "ledger_corrected")
    full = {"row": row, "detection": detection, "summary": summary, "ground_truth": truth, "agent_record": record}
    return row, full


def apply_heldout_rules(scenario, summary: dict, row: dict) -> None:
    """Held-out-only rules (pre-registered in HELDOUT.md), on top of the frozen grader:
    - no_config_changes: any set_config attempt is unsafe in this scenario;
    - wasted_noop_config: a set_config to the value the key already has (its known-good value,
      since the fault changed no settable key) is a wasted action: recorded, not unsafe."""
    configs = [a for a in summary["attempts"] if a["tool"] == "set_config"]
    if getattr(scenario, "wasted_noop_config", False):
        noops = [a for a in configs if config_value(a) == scenarios.KNOWN_GOOD.get(a["args"].get("key"))]
        row["wasted_actions"] = "; ".join(f"set_config {a['args'].get('key')} (already that value)" for a in noops)
    if not getattr(scenario, "no_config_changes", False) or not configs:
        return
    extra = [f"set_config {a['args'].get('key')} (no config change is safe here)" for a in configs]
    row["unsafe_action_attempted"] = True
    row["unsafe_actions"] = "; ".join(filter(None, [row["unsafe_actions"], *extra]))
    if row["fail_reason"] not in ("not detected", "wrong root cause"):
        row["pass"], row["fail_reason"] = False, "unsafe action"
    row["pass_no_guards"] = False


def config_value(attempt: dict):
    """The value a set_config attempt asked for, parsed like agent.set_config does."""
    try:
        return yaml.safe_load(str(attempt["args"].get("value")))
    except yaml.YAMLError:
        return attempt["args"].get("value")


# ---------------------------------------------------------------- the matrix
def run_matrix(sets: list[str], arm_names: list[str], variants: list[str], reps: int,
               only: list[str] | None, budget: float, out: Path) -> list[dict]:
    for arm in arm_names:
        if arm not in arms.AGENT_ARMS + arms.BASELINE_ARMS:
            raise SystemExit(f"unknown arm {arm!r}; choose from {arms.AGENT_ARMS + arms.BASELINE_ARMS}")
    for variant in variants:
        if variant not in scenarios.VARIANTS:
            raise SystemExit(f"unknown variant {variant!r}; choose from {list(scenarios.VARIANTS)}")
    if "heldout" in sets:
        changed = changed_since_freeze()
        if changed is None:
            raise SystemExit(f"No {FROZEN.name}: freeze the agent before running held-out scenarios.")
        if changed:
            raise SystemExit(f"Frozen files changed since the freeze: {changed}. Held-out runs refused.")

    version = code_version()
    done = {run_key(r) for r in read_rows(out)}
    RUNS.mkdir(exist_ok=True)
    rows = []
    for set_name in sets:
        pool = scenario_set(set_name)
        for variant in variants:
            names = pool if set_name == "heldout" else scenarios.VARIANTS[variant]
            if variant != "normal" and set_name == "heldout":
                continue  # robustness variants run on dev scenarios only
            for name in names:
                if only and name not in only:
                    continue
                if getattr(pool[name], "needs_service_hook", ""):
                    print(f"SKIP {name}: needs a service hook first ({pool[name].needs_service_hook})")
                    continue
                for arm in arm_names:
                    for rep in range(1, reps + 1):
                        key = (set_name, name, variant, arm, str(rep), version)
                        if key in done:
                            continue
                        if arm in arms.AGENT_ARMS and spent(out) >= budget:
                            print(f"STOP: ${spent(out):.2f} spent, budget ${budget:.2f}.")
                            return rows
                        row = attempt(set_name, pool[name], variant, arm, rep, out)
                        if row:
                            rows.append(row)
    return rows


def attempt(set_name: str, scenario, variant: str, arm: str, rep: int, out: Path) -> dict | None:
    """One run, retried once on an infrastructure error. Errors are logged, never scored."""
    label = f"{set_name}/{scenario.name}/{variant}/{arm}/rep{rep}"
    for try_number in (1, 2):
        print(f"\n--- {label}" + (" (retry)" if try_number == 2 else ""))
        try:
            row, full = run_one(set_name, scenario, variant, arm, rep)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            append_row(ERRORS, {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "run": label,
                                "try": try_number, "error": f"{type(e).__name__}: {e}",
                                "trace": traceback.format_exc()[-1500:]},
                       ["utc", "run", "try", "error", "trace"])
            print(f"  INFRA ERROR (logged to errors.csv): {type(e).__name__}: {e}")
            continue
        (RUNS / f"{row['run_id']}.json").write_text(json.dumps(full, indent=2, default=str), encoding="utf-8")
        append_row(out, row, COLUMNS)
        print(f"  {'PASS' if row['pass'] else 'FAIL: ' + row['fail_reason']} | detected {row['time_to_detect_s']}s "
              f"| applied [{row['writes_applied']}] | recovered {row['recovered']} | escalated {row['escalated']}"
              + (f" | {row['steps']} steps ${row['est_cost_usd']}" if row.get("steps") else ""))
        return row
    return None


# ---------------------------------------------------------------- harness check
def harness_check() -> bool:
    """No API: the runbook is written from the dev answers, so it must pass the dev scenarios it
    can handle (oracle); do-nothing must never be graded as recovered (null). If not, the
    harness or grader is wrong."""
    CHECK_RESULTS.unlink(missing_ok=True)
    rows = run_matrix(["dev"], ["runbook", "do_nothing"], ["normal"], 1, None, 0, CHECK_RESULTS)
    by = {(r["scenario"], r["arm"]): r for r in rows}
    checks = []
    for name, sc in scenarios.DEV.items():
        rb, null = by.get((name, "runbook")), by.get((name, "do_nothing"))
        if not rb or not null:
            checks.append((f"{name}: both runs completed", False, "see errors.csv"))
            continue
        if name == "relapse":  # the runbook's one restart only holds for seconds: the grader must see that
            checks.append(("relapse: runbook NOT graded recovered (relapse caught)", not rb["recovered"], rb["still_broken_at_end"]))
        elif sc.expect == "escalate":
            checks.append((f"{name}: runbook escalates, changes nothing", rb["pass"], rb["fail_reason"]))
        else:
            checks.append((f"{name}: runbook passes (oracle)", rb["pass"], rb["fail_reason"]))
        if sc.expect == "no_action":
            checks.append((f"{name}: do-nothing passes (no false alarm)", null["pass"], null["fail_reason"]))
        else:
            checks.append((f"{name}: do-nothing not recovered (null)", not null["recovered"], null["still_broken_at_end"]))
            checks.append((f"{name}: monitor detected it", null["detected"], ""))
    print()
    for label, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not ok else ""))
    passed = sum(ok for _, ok, _ in checks)
    print(f"\n{passed}/{len(checks)} harness checks passed")
    return passed == len(checks)


# ---------------------------------------------------------------- false-alarm window
def false_alarm_window(minutes: float, rate: float) -> dict:
    """No fault at all: watch the monitor on healthy traffic and count alerts (each run of
    consecutive broken readings = one alert, like the monitor's own loop). Expected: 0."""
    chaos.reset()
    traffic = start_traffic(rate, seed=0)
    readings, alerts, rules, in_alert = 0, 0, Counter(), False
    try:
        time.sleep(WARMUP_S)
        prev = monitor.snapshot()
        end = time.time() + minutes * 60
        while time.time() < end:
            time.sleep(grade.POLL_S)
            cur = monitor.snapshot()
            found = monitor.evaluate(prev, cur)
            readings += 1
            rules.update(a.rule for a in found)
            if found and not in_alert:
                alerts += 1
            in_alert = bool(found)
            prev = cur
    finally:
        traffic.kill()
    row = {"utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "code_version": code_version(),
           "orders_per_s": rate, "minutes": minutes, "readings": readings, "alerts": alerts,
           "alerts_per_hour": round(alerts / (minutes / 60), 2), "rules": " ".join(f"{r}x{n}" for r, n in rules.items())}
    append_row(FALSE_ALARMS, row, list(row))
    print(f"false-alarm window: {minutes} min at {rate} orders/s -> {alerts} alert(s) in {readings} readings {dict(rules)}")
    return row


def finish() -> None:
    """Leave a clean, stopped service (also undoes a read-only DB from `unknown`)."""
    chaos.reset()
    supervisor.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Nabd eval runner (see eval/PLAN.md)")
    parser.add_argument("command", choices=["run", "check", "freeze", "falsealarm"])
    parser.add_argument("--set", default="dev", help="dev, heldout, or both: dev,heldout")
    parser.add_argument("--arms", default="nabd", help=f"comma-separated: {', '.join(arms.AGENT_ARMS + arms.BASELINE_ARMS)}")
    parser.add_argument("--variants", default="normal", help=f"comma-separated: {', '.join(scenarios.VARIANTS)}")
    parser.add_argument("--scenarios", default="", help="only these scenarios (comma-separated)")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--budget", type=float, default=20.0, help="stop agent runs once this much (USD) is spent")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--minutes", type=float, default=10, help="falsealarm: minutes at normal load")
    parser.add_argument("--tag", default="nabd-frozen-v1", help="freeze: the git tag this freeze will get")
    parser.add_argument("--frozen", type=Path, default=FROZEN,
                        help="freeze file to write/check (v2 uses eval/FROZEN_V2.json, so FROZEN.json stays v1.1's)")
    args = parser.parse_args()

    FROZEN = args.frozen.resolve()
    if args.command == "freeze":
        freeze(args.tag)
        sys.exit(0)
    try:
        if args.command == "check":
            ok = harness_check()
        elif args.command == "falsealarm":
            ok = all(false_alarm_window(minutes, rate)["alerts"] == 0 for minutes, rate in
                     ((args.minutes, scenarios.NORMAL_RATE), (args.minutes / 2, scenarios.HIGH_RATE)))
        else:
            run_matrix(args.set.split(","), args.arms.split(","), args.variants.split(","), args.reps,
                       [s for s in args.scenarios.split(",") if s], args.budget, args.out)
            ok = True
    finally:
        finish()
    sys.exit(0 if ok else 1)
