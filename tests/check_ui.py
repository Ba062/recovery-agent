"""Dashboard check: ui/app.py renders every saved run in replay mode, with no service and no API key.
Uses Streamlit's AppTest (no browser, no server, no port). Run from the project root:  python tests/check_ui.py
On a fresh clone without saved runs it checks the empty state instead.
"""

import json
import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
RAW_RULES = ["payment_gap", "queue_backlog", "worker_stalled", "ledger_mismatch", "high_error_rate", "messages_dropped"]
passed = total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    passed += bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


def texts(at, kind: str) -> list[str]:
    return [e.value for e in getattr(at, kind)]


def charts(at) -> list:
    """Altair charts on the page (AppTest has no wrapper for them yet: they show up as VegaLiteChart protos)."""
    return [e for e in at.main if type(e).__name__ == "UnknownElement" and e.proto.DESCRIPTOR.name == "VegaLiteChart"]


def blocks(node) -> list:
    """Every layout block under a node (containers are nested inside tabs and columns)."""
    found = []
    for child in getattr(node, "children", {}).values():
        if type(child).__name__ == "Block":
            found.append(child)
        found += blocks(child)
    return found


at = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=120).run()
check("dashboard loads without an exception", not at.exception, at.exception[0].message if at.exception else "")

# ---- 8. evaluation headline chart (from eval/results.csv)
if (ROOT / "eval" / "results.csv").exists():
    found = charts(at)
    spec = json.loads(found[0].proto.spec) if found else {}
    check("evaluation: headline chart shown", bool(found))
    check("evaluation: bars faceted dev vs held-out, labelled with k/N",
          spec.get("facet", {}).get("column", {}).get("field") == "Set"
          and any(layer.get("encoding", {}).get("text", {}).get("field") == "Label"
                  for layer in spec.get("spec", {}).get("layer", [])), str(spec)[:200])

saved = any((ROOT / folder).glob("*.json") for folder in ("replays", "eval/runs") if (ROOT / folder).exists())
if not saved:
    check("no saved runs: says so instead of crashing", any("No saved runs" in i for i in texts(at, "info")))
else:
    runs = at.sidebar.selectbox[-1]
    names = list(runs.options)
    if at.sidebar.radio[0].value == "Showcase replays":
        # ---- 6. readable run names from the replay filenames
        check("replays: default source with readable names", names[0].startswith("1. ") and "_" not in " ".join(names),
              str(names[:3]))
    failures, raw_json, metrics_tables = [], [], 0
    for i in range(len(names)):
        at.sidebar.selectbox[-1].select_index(i)
        at.run()
        if at.exception:
            failures.append(f"{names[i]}: {at.exception[0].message}")
        raw_json += [names[i] for t in texts(at, "code") if t.startswith("HTTP 200: {")]
        metrics_tables += any("| Orders created |" in t for t in texts(at, "markdown"))
    check(f"every saved run renders ({len(names)} runs)", not failures, "; ".join(failures[:3]))
    # ---- 5. readable tool results (incl. business metrics cut off at 1500 characters in the record)
    check("tool results formatted: no raw JSON on any run, business metrics shown as a table",
          not raw_json and metrics_tables > 0, f"raw JSON in {sorted(set(raw_json))[:3]}, metrics tables {metrics_tables}")

    at.sidebar.selectbox[-1].select_index(0)  # first run: a fault Nabd fixed
    at.run()
    alert_box = [t for t in texts(at, "error") if t.startswith("### At alert")]
    after_box = [t for t in texts(at, "success") + texts(at, "warning") if t.startswith("### After Nabd")]
    # ---- 1. before and after
    check("before/after: 'At alert: N broken' and 'After Nabd: ...' shown", bool(alert_box) and bool(after_box),
          f"{alert_box[:1]} {after_box[:1]}")
    # ---- 3. friendly labels, not rule names
    check("status uses friendly labels, not rule names",
          alert_box and not any(rule in alert_box[0] for rule in RAW_RULES), alert_box[0][:120] if alert_box else "")
    # ---- 2. money on top
    labels = [m.label for m in at.metric]
    check("money banner: SAR at risk, SAR recovered, delayed orders shown first",
          labels[:3] == ["SAR at risk", "SAR recovered", "Orders delayed (queued)"], str(labels[:4]))
    check("tagline shown", any("business is actually working" in c for c in texts(at, "caption")))
    check("Arabic report keeps its right-to-left markup", any('dir="rtl"' in m for m in texts(at, "markdown")))

    # ---- 4. Play from the start: animates into an auto-scrolling container with a progress bar
    shortest = min(range(len(names)), key=lambda i: len(names[i]) if "ealthy" in names[i] else 99)
    at.sidebar.selectbox[-1].select_index(shortest)
    at.run()
    at.button[0].click()
    at.run(timeout=180)
    scrolling = [b for b in blocks(at.main) if b.proto.autoscroll]
    check("Play: runs without an exception, progress bar and auto-scrolling timeline shown",
          not at.exception and len(at.get("progress")) == 1 and bool(scrolling),
          at.exception[0].message if at.exception else f"progress {len(at.get('progress'))}, autoscroll {len(scrolling)}")

# ---- a check_invariants result is shown green ONLY for the exact green answer (never for an error)
APP_FILE = ROOT / "ui" / "app.py"
CHECK_TEXTS = {
    "error": "Tool error: service unreachable",
    "broken": "Still broken:\n- [business] payment_gap: 3 of 9 orders were processed but have no payment.",
    "green": "All business invariants green (health, payments, queue, worker, ledger, errors, latency).",
}


def render_check(text: str, app_file: Path = APP_FILE) -> AppTest:
    """Run just show_result("check_invariants", text) from ui/app.py: its functions, not the page."""
    script = (
        "from pathlib import Path\n"
        f"app = Path({str(app_file)!r})\n"
        "source = app.read_text(encoding='utf-8')\n"
        "marker = next(l for l in source.splitlines() if l.startswith('# ---') and l.endswith(' page'))\n"
        "env = {'__file__': str(app), '__name__': 'app_functions'}\n"
        "exec(compile(source.split(marker)[0], str(app), 'exec'), env)\n"
        f"env['show_result']('check_invariants', {text!r})\n"
    )
    return AppTest.from_string(script, default_timeout=60).run()


shown = {name: render_check(text) for name, text in CHECK_TEXTS.items()}
check("check_invariants error: never green, shown as a warning with the raw text",
      not shown["error"].exception and not shown["error"].success
      and any("did not give a result" in w.value for w in shown["error"].warning)
      and any("Tool error" in c.value for c in shown["error"].code))
check("check_invariants still broken: red, with friendly labels",
      not shown["broken"].success
      and any("Payments missing: 3 of 9 orders unpaid" in e.value for e in shown["broken"].error))
check("check_invariants green answer: green",
      [s.value for s in shown["green"].success] == ["All business invariants green"])

print(f"\n{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
