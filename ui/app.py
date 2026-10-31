"""Nabd dashboard, replay-first: step through a saved incident without the service or an API key.

Run from the project root:  streamlit run ui/app.py --server.port 8502

Saved incidents come from replays/ (curated showcase runs with incident reports) and eval/runs/ (every eval run,
with the eval's own ground truth and verdict). Live mode (inject a fault, approve fixes) comes later.
"""

import csv
import json
import re
import sys
import time
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import report  # noqa: E402  (renders the bilingual incident report from a record; no API call)

SOURCES = {"Showcase replays": ROOT / "replays", "Eval runs": ROOT / "eval" / "runs"}
RESULTS_CSV = ROOT / "eval" / "results.csv"
FROZEN_JSON = ROOT / "eval" / "FROZEN.json"
WRITE_TOOLS = {"set_config", "rollback_deploy", "restart_worker", "reprocess_orders", "repair_ledger"}
TAGLINE = "Health checks say everything is fine. Nabd checks whether the business is actually working."
PLAY_SECONDS = 1.0  # "Play from the start": about one event per second

# What each read tool does, in plain words (shown instead of the tool name).
READS = {
    "get_business_metrics": "Checks the business metrics", "get_metrics": "Checks the technical metrics",
    "check_health": "Checks /health", "read_logs": "Reads the logs", "read_config": "Reads the live config",
    "recent_changes": "Reads the recent changes", "check_data_integrity": "Checks which orders are affected",
    "check_invariants": "Verifies the business invariants",
}
# Business metrics in plain words (for formatted tool results).
METRIC_NAMES = {
    "orders_created": "Orders created", "payments_recorded": "Payments recorded",
    "orders_without_payment": "Processed but unpaid", "integrity_failures": "Ledger rows wrong",
    "enqueued": "Accepted into the queue", "processed": "Processed", "dropped": "Dropped from the queue",
    "queue_depth": "Waiting in the queue", "worker_heartbeat_age_s": "Worker last heartbeat (s ago)",
    "sync_last_run_age_s": "Ledger sync last ran (s ago)", "error_rate": "Error rate",
    "p95_latency_ms": "p95 latency (ms)", "memory_mb": "Memory (MB)", "request_count": "Requests served",
}
RULE_NAMES = {
    "payment_gap": "Payments missing", "messages_dropped": "Orders dropped", "ledger_mismatch": "Ledger wrong",
    "worker_stalled": "Worker stuck", "queue_backlog": "Orders piling up", "high_error_rate": "Orders failing",
    "high_latency": "Slow responses", "service_down": "Service down",
}
# Monitor message -> plain sentence. Unknown messages fall back to the rule's plain name + the message.
FRIENDLY = [
    ("payment_gap", r"(\d+) of (\d+) orders", "Payments missing: {0} of {1} orders unpaid"),
    ("messages_dropped", r"(\d+) orders dropped.*last (\d+)s", "Orders dropped: {0} lost from the queue in {1} s"),
    ("ledger_mismatch", r"(\d+) ledger entries", "Ledger wrong: {0} entries don't match their order"),
    ("worker_stalled", r"heartbeat is (\d+)s old", "Worker stuck: no heartbeat for {0} s"),
    ("queue_backlog", r"Queue depth (\d+) and growing \(was (\d+)\)", "Orders piling up: {0} waiting (was {1})"),
    ("high_error_rate", r"(\d+)% of recent", "Orders failing: {0}% of requests return an error"),
    ("high_latency", r"p95 latency (\d+)ms", "Slow responses: p95 latency {0} ms"),
]


def friendly(rule: str, message: str) -> str:
    for name, pattern, text in FRIENDLY:
        match = re.search(pattern, message) if name == rule else None
        if match:
            return text.format(*match.groups())
    return f"{RULE_NAMES.get(rule, rule)}: {message}"


def anomalies(text: str) -> list[tuple[str, str]]:
    """(rule, message) for every '- [kind] rule: message' line in an alert or check_invariants result."""
    return re.findall(r"^- \[\w+\] (\w+): (.*)$", text, flags=re.MULTILINE)


# ---------------------------------------------------------------- loading saved runs
def load_run(path: Path) -> dict:
    """A saved run as {record, row, ground_truth}. Eval files wrap the agent record; plain records also work."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if "agent_record" in data:
        return {"record": data["agent_record"], "row": data["row"], "ground_truth": data.get("ground_truth")}
    return {"record": data, "row": None, "ground_truth": None}


def replay_name(stem: str) -> str:
    """'08_relapse_code_guard_overrides' -> '8. Relapse code guard overrides'."""
    number, _, words = stem.partition("_")
    if not number.isdigit():
        return stem.replace("_", " ")
    words = words.replace("_", " ")
    words = "Held-out: " + words[len("heldout "):] if words.startswith("heldout ") else words.capitalize()
    return f"{int(number)}. {words}"


@st.cache_data
def index(folder: str, curated: bool, newest_mtime: float) -> list[dict]:
    """One entry per replayable run (agent runs only: baselines have no investigation to replay).
    newest_mtime is only there so the cache refreshes when new runs are saved."""
    entries = []
    for path in sorted(Path(folder).glob("*.json")):
        run = load_run(path)
        if not run["record"]:
            continue
        row = run["row"] or {}
        if curated or not row:
            label = replay_name(path.stem)
        else:
            verdict = "PASS" if row["pass"] in (True, "True") else "FAIL"
            label = f"{row['scenario']} · {row['variant']} · {row['arm']} · rep {row['rep']} · {verdict}"
        entries.append({"path": str(path), "label": label, "scenario": row.get("scenario", "")})
    return entries


# ---------------------------------------------------------------- 1. before / after
def before_after(run: dict) -> None:
    record, truth = run["record"], run["ground_truth"]
    found = anomalies(record["alert"])
    left, right = st.columns(2)
    with left:
        if found:
            st.error(f"### At alert: {len(found)} broken\n" + "\n".join(f"- {friendly(r, m)}" for r, m in found))
        else:
            st.info("### At alert: no monitor alert\nAn on-call report started this run.")
        if record["alert"].startswith("Health check: passing"):
            st.caption("Meanwhile /health said **passing** ✅ (the shallow check most monitoring relies on).")
    with right:
        d = record["diagnosis"]
        if truth:  # what the eval measured on the service after the run (15 s of monitor readings)
            still = sorted({rule for reading in truth["readings"] for rule in reading["broken"]})
        else:
            still = [] if d["recovered"] else ["not verified"]
        if not still:
            changed = "nothing changed" if not d["actions_taken"] else f"{len(d['actions_taken'])} fix(es) applied"
            st.success(f"### After Nabd: all green\nVerified on the service for 15 s; {changed}.")
        else:
            names = ", ".join(RULE_NAMES.get(r, r) for r in still)
            handoff = "escalated to a human" if d["escalate"] else "not escalated"
            st.warning(f"### After Nabd: still broken ({handoff})\n{names}")


# ---------------------------------------------------------------- 2. money
def sar(m: dict | None, *keys: str) -> float:
    return sum(m[k]["total_sar"] for k in keys) if m else 0.0


def count(m: dict | None, key: str) -> int:
    return m[key]["count"] if m else 0


def money_banner(record: dict) -> None:
    d = record["diagnosis"]
    peak = d.get("money_impact_peak") or d.get("money_impact_before")  # no fix applied: exposure at alert
    end, recovered = d.get("money_impact_after"), d.get("money_recovered")
    cols = st.columns(4)
    cols[0].metric("SAR at risk", f"{sar(peak, 'unpaid_orders', 'ledger_overstatement'):,.0f} SAR", border=True,
                   help="Unpaid orders + ledger overstatement, just before Nabd's first fix (or at alert).")
    cols[0].caption(f"{count(peak, 'unpaid_orders')} orders unpaid · {count(peak, 'ledger_overstatement')} ledger rows wrong")
    cols[1].metric("SAR recovered", f"{sar(recovered, 'orders_paid', 'ledger_corrected'):,.0f} SAR", border=True,
                   help="Orders paid by reprocessing + ledger overstatement corrected, measured by the service.")
    cols[1].caption(f"{count(recovered, 'orders_paid')} orders paid · {count(recovered, 'ledger_corrected')} ledger rows fixed")
    cols[2].metric("Orders delayed (queued)", f"{count(peak, 'queued_orders')}", border=True,
                   help="Accepted but not yet processed: delayed, not lost.")
    cols[2].caption(f"{sar(peak, 'queued_orders'):,.0f} SAR waiting")
    cols[3].metric("Still at risk at the end", f"{sar(end, 'unpaid_orders', 'ledger_overstatement'):,.0f} SAR", border=True)
    cols[3].caption(f"{count(end, 'unpaid_orders')} unpaid · {count(end, 'queued_orders')} still queued")
    st.caption("All money figures are computed from the database by code, never by the model.")


# ---------------------------------------------------------------- 5. readable tool results
def table(pairs: list[tuple[str, object]]) -> None:
    st.markdown("| | |\n|---|---|\n" + "\n".join(f"| {k} | {v} |" for k, v in pairs))


def leading_fields(body: str) -> dict:
    """The simple "key": value fields of a JSON object that was cut off (the run record keeps only the first
    1500 characters of a tool result). Stops at the first nested object, where the cut usually is."""
    head = re.split(r'"\w+":\s*[{\[]', body, maxsplit=1)[0]
    return {k: json.loads(v) for k, v in re.findall(r'"(\w+)":\s*("[^"]*"|-?[\d.]+(?:[eE][-+]?\d+)?|true|false|null)', head)}


def show_result(tool: str, text: str) -> None:
    """Tool output in readable form. Anything unexpected falls back to the raw text."""
    body = text.partition(": ")[2]
    if text.startswith("HTTP 200: "):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:  # cut off at 1500 characters: show the fields we have
            data = leading_fields(body) or None
            if data:
                st.caption("(longer output, shortened in the run record)")
        if isinstance(data, dict):
            money = data.pop("money_impact", None) or data.pop("money_recovered", None)
            if tool == "check_data_integrity":
                table([("Processed but unpaid", data["unpaid_processed_orders"]["count"]),
                       ("Ledger rows wrong", data["ledger_mismatches"]["count"])])
            else:
                table([(METRIC_NAMES.get(k, k.replace("_", " ")), v) for k, v in data.items()
                       if not isinstance(v, (dict, list))])
            if money and "unpaid_orders" in money:
                st.caption(f"Money: {count(money, 'unpaid_orders')} unpaid = {sar(money, 'unpaid_orders'):,.2f} SAR · "
                           f"{count(money, 'queued_orders')} queued = {sar(money, 'queued_orders'):,.2f} SAR · "
                           f"ledger overstated by {sar(money, 'ledger_overstatement'):,.2f} SAR")
            elif money:
                st.caption(f"Money recovered: {money['count']} = {money['total_sar']:,.2f} SAR")
            return
    if tool == "check_invariants":
        # Green ONLY for the exact green answer: an error or an unreadable result must never look green.
        found = anomalies(text)
        if text.startswith("All business invariants green"):
            st.success("All business invariants green")
        elif found:
            st.error("Still broken:\n" + "\n".join(f"- {friendly(r, m)}" for r, m in found))
        else:
            st.warning("The check did not give a result (shown as returned):")
            st.code(text[:700], language=None, wrap_lines=True)
        return
    if tool == "read_logs" and "Most recent" in text:
        summary, _, recent = text.partition("Most recent")
        st.code(summary.strip(), language=None, wrap_lines=True)
        with st.expander("Latest log lines"):
            for line in recent.splitlines()[1:]:
                try:
                    r = json.loads(line)
                    st.text(f"{r['ts'][11:19]}  {r['level']:<7} {r.get('component', '')}: {r['msg']}")
                except (json.JSONDecodeError, KeyError):
                    st.text(line)
        return
    if tool == "recent_changes" and text.startswith("["):
        rows = []
        for line in text.splitlines():
            age, _, entry = line.partition("] ")
            try:
                e = json.loads(entry)
                rows.append((age.strip("["), f"{e['version']} · {e['change']} · by {e['by']}"))
            except (json.JSONDecodeError, KeyError):
                rows.append(("", line))
        table(rows)
        return
    st.code(text[:700] + (" …" if len(text) > 700 else ""), language="yaml" if tool == "read_config" else None,
            wrap_lines=True)


# ---------------------------------------------------------------- one event of the investigation
def write_status(text: str, is_error: bool) -> str:
    for prefix, status in (("REJECTED", "❌ rejected by the human"), ("BLOCKED", "⛔ blocked by Nabd's safety rule"),
                           ("SKIPPED", "⏭️ skipped: one fix per turn")):
        if text.startswith(prefix):
            return status
    return "⚠️ failed" if is_error else "✅ approved and applied"


def show_event(e: dict) -> None:
    t = f"`{e['t']:>5.1f}s`"
    if e["kind"] == "alert":
        st.error(f"{t} **Alert from the monitor (rule-based, no LLM)**\n\n"
                 + "\n".join(f"- {friendly(r, m)}" for r, m in anomalies(e["text"])))
    elif e["kind"] == "thinking":
        with st.expander(f"{e['t']:.1f}s  🧠 Reasoning (summary)"):
            st.write(e["text"])
    elif e["kind"] == "thought":
        st.markdown(f"{t} 💬 {e['text']}")
    elif e["kind"] == "tool_call" and e["tool"] == "submit_diagnosis":
        st.markdown(f"{t} 📝 **Submits its diagnosis**")
    elif e["kind"] == "tool_call":
        args = ", ".join(f"{k}={v}" for k, v in e["args"].items())
        if e["tool"] in WRITE_TOOLS:
            st.warning(f"{t} 🛠️ **Proposes a fix: `{e['tool']}({args})`** (needs human approval)")
        else:
            st.markdown(f"{t} 🔍 {READS.get(e['tool'], e['tool'])}" + (f" ({args})" if args else ""))
    elif e["kind"] == "tool_result":
        if e["tool"] in WRITE_TOOLS:
            st.markdown(f"{t} → {write_status(e['text'], e['is_error'])}")
            if not e["text"].startswith(("REJECTED", "BLOCKED", "SKIPPED")):
                show_result(e["tool"], e["text"])
        else:
            show_result(e["tool"], e["text"])
    elif e["kind"] == "verification":
        st.warning(f"{t} 🛡️ **Nabd's code check overrides the model:** {e['text']}")
    elif e["kind"] == "fallback":
        st.info(f"{t} {e['text']}")


# ---------------------------------------------------------------- panels
def diagnosis_card(record: dict) -> None:
    d, stats = record["diagnosis"], record["stats"]
    outcome = ("✅ Recovered (verified in code)" if d["recovered"]
               else "🧑‍💻 Escalated to a human" if d["escalate"] else "❌ Not recovered")
    cols = st.columns(4)
    cols[0].metric("Confidence", f"{d['confidence']:.2f}")
    cols[1].metric("Agent steps", stats["steps"])
    cols[2].metric("Time", f"{stats['seconds']:.0f} s")
    cols[3].metric("Cost", f"${stats['est_cost_usd']:.3f}")
    st.markdown(f"**{outcome}**  \n{d['summary']}")
    left, right = st.columns(2)
    with left:
        st.markdown("**Root causes**\n" + "\n".join(f"- {c}" for c in d["root_causes"]))
        st.markdown("**Actions taken**\n" + ("\n".join(f"- `{a}`" for a in d["actions_taken"]) or "- none"))
        if d["recommended_actions"]:
            st.markdown("**Recommended for a human**\n" + "\n".join(f"- {a}" for a in d["recommended_actions"]))
    with right:
        st.markdown("**Evidence**\n" + "\n".join(f"- {e}" for e in d["evidence"]))


def eval_verdict(row: dict) -> None:
    passed = row["pass"] in (True, "True")
    (st.success if passed else st.error)(
        f"**Eval verdict: {'PASS' if passed else 'FAIL: ' + row['fail_reason']}**. Judged from the service itself "
        f"(15 s of monitor readings after the run), not from what the agent said.")


def report_tab(path: Path, record: dict) -> None:
    saved = path.with_suffix(".report.md")
    if saved.exists():  # a report saved next to the record (English, ---, Arabic)
        english, _, arabic = saved.read_text(encoding="utf-8").partition("\n\n---\n\n")
    else:
        prose = (record.get("report") or {}).get("prose")
        if not prose:
            st.info("This run has no model-written prose (eval runs skip it to save cost). "
                    "Every figure below is inserted by code from the run record.")
        english, arabic = report.render(record, prose, "en"), report.render(record, prose, "ar")
    en, ar = st.tabs(["English", "العربية"])
    en.markdown(english, unsafe_allow_html=True)
    ar.markdown(arabic, unsafe_allow_html=True)  # keeps <div dir="rtl"> and <span dir="ltr">


# ---------------------------------------------------------------- 8. evaluation headline
HEADLINE_ARMS = {"nabd": "Nabd", "runbook": "Static runbook", "always_restart": "Always restart",
                 "do_nothing": "Do nothing"}


def frozen_versions() -> set[str]:
    """Code versions of the frozen agent (the same set eval/summarize.py counts)."""
    if not FROZEN_JSON.exists():
        return set()
    frozen = json.loads(FROZEN_JSON.read_text(encoding="utf-8"))
    versions = {frozen["code_version"]}
    if frozen.get("agent_unchanged"):
        versions |= {p["code_version"] for p in frozen.get("previous", [])}
    return versions


def headline_data() -> pd.DataFrame:
    """Pass rate per arm and set (normal conditions, frozen agent), from eval/results.csv."""
    if not RESULTS_CSV.exists():
        return pd.DataFrame()
    with RESULTS_CSV.open(encoding="utf-8", newline="") as f:
        rows = {r["run_id"]: r for r in csv.DictReader(f) if r.get("run_id") and r.get("pass")}  # skip a half-written line
    versions = frozen_versions()
    tally = {}
    for r in rows.values():
        if r["variant"] != "normal" or r["arm"] not in HEADLINE_ARMS or (versions and r["code_version"] not in versions):
            continue
        k, n = tally.get((r["set"], r["arm"]), (0, 0))
        tally[(r["set"], r["arm"])] = (k + (r["pass"] == "True"), n + 1)
    return pd.DataFrame([{"Set": "Dev" if s == "dev" else "Held-out", "Arm": HEADLINE_ARMS[a],
                          "Pass rate": k / n, "Label": f"{k}/{n}"} for (s, a), (k, n) in tally.items()])


def headline_chart(data: pd.DataFrame) -> alt.FacetChart:
    order = list(HEADLINE_ARMS.values())
    base = alt.Chart(data).encode(
        x=alt.X("Arm:N", sort=order, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("Pass rate:Q", axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1.1]), title="Pass rate"))
    colours = alt.Scale(domain=order, range=["#1f77b4", "#9aa5b1", "#bcc4cc", "#dde2e6"])  # Nabd stands out
    bars = base.mark_bar().encode(color=alt.Color("Arm:N", scale=colours, legend=None))
    labels = base.mark_text(dy=-8, fontWeight="bold").encode(text="Label:N")
    return alt.layer(bars, labels, data=data).properties(width=320, height=260).facet(
        column=alt.Column("Set:N", sort=["Dev", "Held-out"], title=None))


def evaluation_tab() -> None:
    st.subheader("Nabd vs simpler options: pass rate under normal conditions")
    data = headline_data()
    if data.empty:
        st.info("No eval results yet: run `python eval/run_eval.py ...`.")
    else:
        st.altair_chart(headline_chart(data))
        st.caption("Pass = the pre-registered rules in eval/PLAN.md 6.3, judged on the service itself. Labels are "
                   "passed/runs. Baselines aren't asked for a root cause. The runbook was written from the dev answers, "
                   "so dev is its home ground; held-out scenarios were designed after the agent was frozen.")
    results = ROOT / "eval" / "RESULTS.md"
    if results.exists():
        with st.expander("Full results (eval/RESULTS.md)"):
            st.markdown(results.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- page
st.set_page_config(page_title="Nabd · نبض", page_icon="💓", layout="wide")
st.title("Nabd · نبض")
st.caption(TAGLINE)

with st.sidebar:
    st.header("Replay an incident")
    has_runs = [name for name, folder in SOURCES.items() if folder.exists() and any(folder.glob("*.json"))]
    source = st.radio("Source", list(SOURCES), index=list(SOURCES).index(has_runs[0]) if has_runs else 0)
    folder = SOURCES[source]
    files = list(folder.glob("*.json")) if folder.exists() else []
    runs = index(str(folder), source == "Showcase replays", max((f.stat().st_mtime for f in files), default=0.0))
    if source == "Eval runs":
        scenarios = sorted({r["scenario"] for r in runs if r["scenario"]})
        chosen = st.selectbox("Scenario", ["all"] + scenarios)
        runs = [r for r in runs if chosen == "all" or r["scenario"] == chosen]
    picked = st.selectbox("Run", runs, format_func=lambda r: r["label"]) if runs else None
    st.divider()
    st.caption("Live mode (inject a fault, approve fixes yourself) needs the service running: coming next.")

incident, report_view, evaluation = st.tabs(["Incident", "Incident report", "Evaluation"])
with evaluation:
    evaluation_tab()
if picked is None:
    with incident:
        st.info(f"No saved runs in `{folder.relative_to(ROOT)}` yet.")
    st.stop()

path = Path(picked["path"])
run = load_run(path)
record = run["record"]

with incident:
    before_after(run)
    money_banner(record)
    if run["row"]:
        eval_verdict(run["row"])
    events = [e for e in record["events"] if e["kind"] != "diagnosis"]
    st.subheader("The investigation, step by step")
    left, right = st.columns([3, 1])
    shown = (left.slider("Replay up to event", 1, len(events), len(events)) if len(events) > 1 else len(events))
    play = right.button("▶ Play from the start")
    if play:
        progress = st.progress(0.0, text="Starting the replay…")
        timeline = st.container(height=560, border=True, autoscroll=True)  # keeps the newest event in view
        for i, e in enumerate(events, start=1):
            with timeline:
                show_event(e)
            progress.progress(i / len(events), text=f"Event {i} of {len(events)} · {e['t']:.1f} s into the run")
            time.sleep(PLAY_SECONDS)
    else:
        with st.container(border=True):
            for e in events[:shown]:
                show_event(e)
    if play or shown == len(events):
        st.subheader("Diagnosis")
        diagnosis_card(record)
with report_view:
    report_tab(path, record)
