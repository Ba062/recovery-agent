"""Turn eval/results.csv into eval/RESULTS.md (tables for the README and slides).

Usage, from the project root:  python eval/summarize.py [--in eval/results.csv other.csv ...] [--out eval/RESULTS.md]
(several --in files: e.g. the main lane's results.csv and the second checkout's baseline results)

Main tables use one agent version: the frozen one if eval/FROZEN.json exists, else the newest.
Dev and held-out are always in separate tables. Every failed run is listed at the end.
"""

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

EVAL = Path(__file__).resolve().parent
sys.path.insert(0, str(EVAL.parent))  # scenarios.py imports chaos, which lives in the project root


def yes(value: str) -> bool:
    return value == "True"


def wilson(k: int, n: int) -> str:
    """95% Wilson interval for k passes out of n: a confidence interval that behaves at small n."""
    if n == 0:
        return ""
    z, p = 1.96, k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return f"{max(0, centre - half):.0%}-{min(1, centre + half):.0%}"


def rate(rows: list[dict], column: str) -> str:
    k = sum(yes(r[column]) for r in rows)
    return f"{k}/{len(rows)}" if rows else "-"


def median(rows: list[dict], column: str) -> str:
    values = [float(r[column]) for r in rows if r[column] not in ("", "None")]
    return f"{statistics.median(values):.0f}" if values else "-"


def mean_cost(rows: list[dict]) -> str:
    values = [float(r["est_cost_usd"]) for r in rows if r["est_cost_usd"]]
    return f"${statistics.mean(values):.3f}" if values else "$0"


def table(header: list[str], lines: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return out + ["| " + " | ".join(str(c) for c in line) + " |" for line in lines]


def group(rows: list[dict], *keys: str) -> dict:
    groups = defaultdict(list)
    for r in rows:
        groups[tuple(r[k] for k in keys)].append(r)
    return groups


def nabd_table(rows: list[dict]) -> list[str]:
    lines = []
    for (scenario,), rs in group(rows, "scenario").items():
        reasons = ", ".join(sorted({r["fail_reason"] for r in rs if r["fail_reason"]}))
        lines.append([scenario, rate(rs, "pass"), rate(rs, "recovered"), rate(rs, "escalated"),
                      median(rs, "time_to_detect_s"), median(rs, "time_to_recover_s"), median(rs, "steps"),
                      mean_cost(rs), reasons or "-"])
    k = sum(yes(r["pass"]) for r in rows)
    lines.append(["**all**", f"**{k}/{len(rows)}**", "", "", "", "", "", mean_cost(rows), f"95% CI {wilson(k, len(rows))}"])
    return table(["Scenario", "Pass", "Recovered", "Escalated", "Detect s (median)", "Recover s (median)",
                  "Steps (median)", "Cost (mean)", "Fail reasons"], lines)


def arms_table(rows: list[dict], expect: dict) -> list[str]:
    lines = []
    for (arm,), rs in group(rows, "arm").items():
        faults = [r for r in rs if expect.get(r["scenario"]) != "no_action"]
        fixable = [r for r in rs if expect.get(r["scenario"]) == "recover"]
        escalate = [r for r in rs if expect.get(r["scenario"]) == "escalate"]
        healthy = [r for r in rs if expect.get(r["scenario"]) == "no_action"]
        lines.append([arm, rate(faults, "detected"), rate(fixable, "recovered"), rate(escalate, "pass"),
                      rate(healthy, "pass"), sum(yes(r["unsafe_action_attempted"]) for r in rs),
                      median(fixable, "time_to_recover_s"), mean_cost(rs)])
    return table(["Arm", "Detected (faults)", "Recovered (fixable)", "Correctly escalated (unfixable)",
                  "Healthy left alone", "Unsafe actions", "Recover s (median)", "Cost/incident"], lines)


def detection_table(rows: list[dict]) -> list[str]:
    lines = []
    for (scenario,), rs in group(rows, "scenario").items():
        cells = []
        for column in ("time_to_detect_s", "detected_health_only_s", "detected_technical_s"):
            k = sum(r[column] not in ("", "None") for r in rs)
            cells.append(f"{k}/{len(rs)}")
        lines.append([scenario, *cells])
    return table(["Scenario", "Nabd monitor (business invariants)", "Health check only (B1)",
                  "Technical metrics only (B2)"], lines)


def main(paths_in: list[Path], path_out: Path) -> None:
    by_id = {}  # the second checkout's CSV starts as a copy of the committed one: count each run once
    for path in paths_in:
        with path.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                by_id[r["run_id"]] = r
    rows = list(by_id.values())
    frozen_path = EVAL / "FROZEN.json"
    frozen = json.loads(frozen_path.read_text(encoding="utf-8")) if frozen_path.exists() else None
    version = frozen["code_version"] if frozen else rows[-1]["code_version"]
    # Earlier freezes count too, but only while every re-freeze kept the agent byte-identical.
    versions = {version}
    if frozen and frozen.get("previous") and frozen.get("agent_unchanged"):
        versions |= {p["code_version"] for p in frozen["previous"]}
    import scenarios  # noqa: E402  (eval/ is on sys.path when run as a script)
    expect = {name: s.expect for name, s in scenarios.DEV.items()}
    try:
        import heldout
        expect |= {name: s.expect for name, s in heldout.HELDOUT.items()}
    except ImportError:
        pass

    current = [r for r in rows if r["code_version"] in versions]
    md = [f"# Nabd eval results", "",
          f"Generated by `eval/summarize.py` from {', '.join(f'`{p.name}`' for p in paths_in)}. Agent code version `{version}`"
          f"{' (frozen)' if frozen else ''}"
          f"{'; also counted: ' + ', '.join(sorted(versions - {version})) + ' (same agent, see Freeze history)' if len(versions) > 1 else ''}"
          f". k/N = passed runs / runs.", ""]
    for set_name in ("dev", "heldout"):
        nabd = [r for r in current if r["set"] == set_name and r["arm"] == "nabd" and r["variant"] == "normal"]
        if nabd:
            md += [f"## Nabd on {set_name} scenarios", "", *nabd_table(nabd), ""]
        normal = [r for r in current if r["set"] == set_name and r["variant"] == "normal"]
        if len({r["arm"] for r in normal}) > 1:
            md += [f"## Nabd vs baselines ({set_name})", "", *arms_table(normal, expect), ""]
        null = [r for r in normal if r["arm"] == "do_nothing"]
        if null:
            md += [f"## Who notices the fault? ({set_name}, within 60 s, do-nothing runs)", "", *detection_table(null), ""]

    robust = [r for r in current if r["arm"] == "nabd" and r["variant"] != "normal"]
    if robust:
        base = group([r for r in current if r["arm"] == "nabd" and r["variant"] == "normal" and r["set"] == "dev"], "scenario")
        lines = [[variant, scenario, rate(rs, "pass"), rate(base.get((scenario,), []), "pass"),
                  ", ".join(sorted({r["fail_reason"] for r in rs if r["fail_reason"]})) or "-"]
                 for (variant, scenario), rs in group(robust, "variant", "scenario").items()]
        md += ["## Robustness (dev scenarios)", "", *table(["Variant", "Scenario", "Pass", "Pass, normal", "Fail reasons"], lines), ""]

    ablations = {"no_debug_logs", "no_alert", "no_guards", "minimal_prompt"}
    ablated = [r for r in current if r["set"] == "dev" and r["arm"] in ablations and r["variant"] == "normal"]
    if ablated:
        lines = []
        for (arm,), rs in group(ablated, "arm").items():
            names = {r["scenario"] for r in rs}
            ref = [r for r in current if r["arm"] == "nabd" and r["variant"] == "normal" and r["set"] == "dev"
                   and r["scenario"] in names]
            k, n, rk = sum(yes(r["pass"]) for r in rs), len(rs), sum(yes(r["pass"]) for r in ref)
            lines.append([arm, f"{k}/{n} ({wilson(k, n)})", f"{rk}/{len(ref)} ({wilson(rk, len(ref))})",
                          median(rs, "steps"), median(ref, "steps"), mean_cost(rs), mean_cost(ref)])
        md += ["## Ablations (dev, pooled)", "", *table(["Switched off", "Pass (95% CI)", "Nabd normal (95% CI)",
                                                         "Steps", "Steps, normal", "Cost", "Cost, normal"], lines), ""]

    agent_rows = [r for r in current if r["arm"] == "nabd"]
    if agent_rows:
        changed = [r for r in agent_rows if r["pass"] != r["pass_no_guards"]]
        md += ["## Code guards (offline ablation A3)", "",
               f"Over {len(agent_rows)} Nabd runs, grading the model's own final claim instead of the guarded one "
               f"changes the verdict in {len(changed)} run(s):"
               + "".join(f"\n- `{r['run_id']}`: with guards {'pass' if yes(r['pass']) else 'fail'}, "
                         f"without {'pass' if yes(r['pass_no_guards']) else 'fail'}" for r in changed), ""]

    dev_nabd = [r for r in rows if r["set"] == "dev" and r["arm"] == "nabd" and r["variant"] == "normal"]
    if len({r["code_version"] for r in dev_nabd}) > 1:
        lines = [[v, rs[0]["utc"][:16], f"{sum(yes(r['pass']) for r in rs)}/{len(rs)}",
                  ", ".join(sorted({f"{r['scenario']}: {r['fail_reason']}" for r in rs if r["fail_reason"]})) or "-"]
                 for (v,), rs in group(dev_nabd, "code_version").items()]
        md += ["## Iteration log (dev, Nabd, per agent version)", "", *table(["Version", "First run (UTC)", "Pass", "Failures"], lines), ""]

    alarms = EVAL / "false_alarms.csv"
    if alarms.exists():
        with alarms.open(encoding="utf-8", newline="") as f:
            lines = [[r["orders_per_s"], r["minutes"], r["readings"], r["alerts"], r["alerts_per_hour"], r["rules"] or "-"]
                     for r in csv.DictReader(f)]
        md += ["## False alarms (healthy traffic, no fault)", "",
               *table(["Orders/s", "Minutes", "Readings", "Alerts", "Alerts/hour", "Rules that fired"], lines), ""]

    if frozen and frozen.get("previous"):
        previous = frozen["previous"][-1]
        md += ["## Freeze history", "",
               f"- `{previous.get('tag', 'nabd-frozen-v1')}`: code version `{previous['code_version']}`, agent fingerprint "
               f"`{previous['fingerprint']}`.",
               f"- `{frozen.get('tag')}`: code version `{frozen['code_version']}`, agent fingerprint `{frozen['fingerprint']}`.",
               f"- Changed between them: {', '.join(f'`{n}`' for n in frozen['changed_since_previous']) or 'nothing'}.",
               f"- Byte-identical (SHA-256): {', '.join(f'`{n}`' for n in frozen['identical_to_previous'])}.",
               f"- Agent (prompt, tools, guards, model, limits) unchanged: **{frozen['agent_unchanged']}**. Check with "
               f"`git diff {previous.get('tag', 'nabd-frozen-v1')} {frozen.get('tag')} -- agent.py monitor.py eval/grade.py`"
               f" (empty output).", ""]

    failures = [r for r in current if not yes(r["pass"])
                and not (r["arm"] in ("do_nothing",) and expect.get(r["scenario"]) != "no_action")]
    md += ["## Every failed run", ""]
    md += [f"- `{r['run_id']}`: {r['fail_reason']}" + (f" ({r['unsafe_actions']})" if r["unsafe_actions"] else "")
           for r in failures] or ["None."]
    path_out.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"wrote {path_out} ({len(current)} runs of version {version})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="paths_in", type=Path, nargs="+", default=[EVAL / "results.csv"])
    parser.add_argument("--out", dest="path_out", type=Path, default=EVAL / "RESULTS.md")
    args = parser.parse_args()
    main(args.paths_in, args.path_out)
