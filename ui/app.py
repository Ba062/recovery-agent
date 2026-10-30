"""Nabd dashboard, replay-first: step through a saved incident without the service or an API key.

Run from the project root:  streamlit run ui/app.py

Saved incidents come from replays/ (runs saved by `python agent.py`) and eval/runs/ (every eval run,
with the eval's own ground truth and verdict). Live mode (inject a fault, approve fixes) comes later.
"""

import json
import sys
import time
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import report  # noqa: E402  (renders the bilingual incident report from a record; no API call)

SOURCES = {"Showcase replays": ROOT / "replays", "Eval runs": ROOT / "eval" / "runs"}
WRITE_TOOLS = {"set_config", "rollback_deploy", "restart_worker", "reprocess_orders", "repair_ledger"}
TAGLINE = "Health checks say everything is fine. Nabd checks whether the business is actually working."


# ---------------------------------------------------------------- loading saved runs
def load_run(path: Path) -> dict:
    """A saved run as {record, row, ground_truth}. Eval files wrap the agent record; replays are the record."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if "agent_record" in data:
        return {"record": data["agent_record"], "row": data["row"], "ground_truth": data.get("ground_truth")}
    return {"record": data, "row": None, "ground_truth": None}


@st.cache_data
def index(folder: str, newest_mtime: float) -> list[dict]:
    """One entry per replayable run (agent runs only: baselines have no investigation to replay).
    newest_mtime is only there so the cache refreshes when new runs are saved."""
    entries = []
    for path in sorted(Path(folder).glob("*.json")):
        run = load_run(path)
        if not run["record"]:
            continue
        row = run["row"] or {}
        d = run["record"]["diagnosis"]
        outcome = "recovered" if d["recovered"] else "escalated" if d["escalate"] else "not recovered"
        verdict = "" if not row else ("PASS" if row["pass"] in (True, "True") else "FAIL")
        label = (f"{row['scenario']} · {row['variant']} · {row['arm']} · rep {row['rep']} · {verdict}" if row
                 else f"{path.stem} · {outcome}")
        entries.append({"path": str(path), "label": label, "scenario": row.get("scenario", "")})
    return entries


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
        st.error(f"{t} **Alert from the monitor (rule-based, no LLM)**\n\n" + e["text"].split("\nBusiness metrics:")[0])
    elif e["kind"] == "thinking":
        with st.expander(f"{e['t']:.1f}s  🧠 Reasoning (summary)"):
            st.write(e["text"])
    elif e["kind"] == "thought":
        st.markdown(f"{t} 💬 {e['text']}")
    elif e["kind"] == "tool_call" and e["tool"] == "submit_diagnosis":
        st.markdown(f"{t} 📝 **Submits its diagnosis**")
    elif e["kind"] == "tool_call":
        args = json.dumps(e["args"]) if e["args"] else ""
        if e["tool"] in WRITE_TOOLS:
            st.warning(f"{t} 🛠️ **Proposes a fix: `{e['tool']}({args})`** (needs human approval)")
        else:
            st.markdown(f"{t} 🔍 Reads `{e['tool']}({args})`")
    elif e["kind"] == "tool_result":
        if e["tool"] in WRITE_TOOLS:
            st.markdown(f"{t} → {write_status(e['text'], e['is_error'])}")
        st.code(e["text"][:700] + (" …" if len(e["text"]) > 700 else ""), language=None, wrap_lines=True)
    elif e["kind"] == "verification":
        st.warning(f"{t} 🛡️ **Nabd's code check overrides the model:** {e['text']}")
    elif e["kind"] == "fallback":
        st.info(f"{t} {e['text']}")


# ---------------------------------------------------------------- panels
def status_strip(record: dict) -> None:
    alert = record["alert"]
    anomalies = [line[2:] for line in alert.splitlines() if line.startswith("- [")]
    left, right = st.columns(2)
    if alert.startswith("Health check: passing"):
        left.success("**/health:** passing ✅  (the shallow check most monitoring relies on)")
    elif alert.startswith("Health check"):
        left.error("**/health:** failing")
    else:
        left.info("**No monitor alert:** an on-call report started this run")
    if anomalies:
        right.error(f"**Business invariants:** {len(anomalies)} broken ❌\n\n" + "\n".join(f"- {a}" for a in anomalies))
    else:
        right.success("**Business invariants:** no alert")


def money_line(m: dict | None) -> str:
    if not m:
        return "—"
    exposed = m["unpaid_orders"]["total_sar"] + m["ledger_overstatement"]["total_sar"]
    return f"{exposed:,.2f} SAR"


def diagnosis_card(record: dict) -> None:
    d, stats = record["diagnosis"], record["stats"]
    outcome = ("✅ Recovered (verified in code)" if d["recovered"]
               else "🧑‍💻 Escalated to a human" if d["escalate"] else "❌ Not recovered")
    cols = st.columns(5)
    cols[0].metric("Outcome", outcome.split(" ", 1)[1].split(" (")[0])
    cols[1].metric("Confidence", f"{d['confidence']:.2f}")
    cols[2].metric("Agent steps", stats["steps"])
    cols[3].metric("Time", f"{stats['seconds']:.0f} s")
    cols[4].metric("Cost", f"${stats['est_cost_usd']:.3f}")
    st.markdown(f"**{outcome}**  \n{d['summary']}")
    left, right = st.columns(2)
    with left:
        st.markdown("**Root causes**\n" + "\n".join(f"- {c}" for c in d["root_causes"]))
        st.markdown("**Actions taken**\n" + ("\n".join(f"- `{a}`" for a in d["actions_taken"]) or "- none"))
        if d["recommended_actions"]:
            st.markdown("**Recommended for a human**\n" + "\n".join(f"- {a}" for a in d["recommended_actions"]))
    with right:
        st.markdown("**Evidence**\n" + "\n".join(f"- {e}" for e in d["evidence"]))
    st.markdown("**Money at risk** (computed from the database by code, never by the model)")
    m = st.columns(4)
    m[0].metric("At alert", money_line(d.get("money_impact_before")))
    m[1].metric("Peak, before the first fix", money_line(d.get("money_impact_peak")))
    recovered = d.get("money_recovered")
    m[2].metric("Recovered", "—" if not recovered else
                f"{recovered['orders_paid']['total_sar'] + recovered['ledger_corrected']['total_sar']:,.2f} SAR")
    m[3].metric("At end of run", money_line(d.get("money_impact_after")))


def eval_verdict(row: dict, ground_truth: dict | None) -> None:
    passed = row["pass"] in (True, "True")
    (st.success if passed else st.error)(
        f"**Eval verdict: {'PASS' if passed else 'FAIL: ' + row['fail_reason']}**. Judged from the service itself "
        f"(15 s of monitor readings after the run), not from what the agent said.")
    if ground_truth:
        readings = ["green" if not r["broken"] else ", ".join(r["broken"]) for r in ground_truth["readings"]]
        st.caption("Hold-window readings: " + " → ".join(readings))


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


def evaluation_tab() -> None:
    results = ROOT / "eval" / "RESULTS.md"
    if results.exists():
        st.markdown(results.read_text(encoding="utf-8"))
    else:
        st.info("No eval results yet: run `python eval/run_eval.py ...` then `python eval/summarize.py`.")


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
    runs = index(str(folder), max((f.stat().st_mtime for f in files), default=0.0))
    scenarios = sorted({r["scenario"] for r in runs if r["scenario"]})
    if scenarios:
        chosen = st.selectbox("Scenario", ["all"] + scenarios)
        runs = [r for r in runs if chosen == "all" or r["scenario"] == chosen]
    picked = st.selectbox("Run", runs, format_func=lambda r: r["label"]) if runs else None
    st.divider()
    st.caption("Live mode (inject a fault, approve fixes yourself) needs the service running: coming next.")

if picked is None:
    st.info(f"No saved runs in `{folder.relative_to(ROOT)}` yet.")
    st.stop()

path = Path(picked["path"])
run = load_run(path)
record = run["record"]

incident, report_view, evaluation = st.tabs(["Incident", "Incident report", "Evaluation"])
with incident:
    status_strip(record)
    if run["row"]:
        eval_verdict(run["row"], run["ground_truth"])
    events = [e for e in record["events"] if e["kind"] != "diagnosis"]
    st.subheader("The investigation, step by step")
    shown = st.slider("Replay up to event", 1, len(events), len(events)) if len(events) > 1 else len(events)
    play = st.button("▶ Play from the start")
    timeline = st.container()
    with timeline:
        if play:
            for e in events:
                show_event(e)
                time.sleep(0.4)
        else:
            for e in events[:shown]:
                show_event(e)
    if play or shown == len(events):
        st.subheader("Diagnosis")
        diagnosis_card(record)
with report_view:
    report_tab(path, record)
with evaluation:
    evaluation_tab()
