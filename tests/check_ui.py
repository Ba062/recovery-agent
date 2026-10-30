"""Dashboard check: ui/app.py renders every saved run in replay mode, with no service and no API key.
Uses Streamlit's AppTest (no browser, no server, no port). Run from the project root:  python tests/check_ui.py
On a fresh clone (no saved runs yet) it checks the empty state instead.
"""

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
passed = total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    passed += bool(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))


at = AppTest.from_file(str(ROOT / "ui" / "app.py"), default_timeout=60).run()
check("dashboard loads without an exception", not at.exception, at.exception[0].message if at.exception else "")
saved = any((ROOT / folder).glob("*.json") for folder in ("replays", "eval/runs") if (ROOT / folder).exists())
if not saved:
    check("no saved runs: says so instead of crashing", any("No saved runs" in i.value for i in at.info))
else:
    runs = at.sidebar.selectbox[-1]
    failures = []
    for i in range(len(runs.options)):
        at.sidebar.selectbox[-1].select_index(i)
        at.run()
        if at.exception:
            failures.append(f"{at.sidebar.selectbox[-1].options[i]}: {at.exception[0].message}")
    check(f"every saved run renders ({len(runs.options)} runs)", not failures, "; ".join(failures[:3]))
    check("tagline and health-vs-business status are shown",
          any("business is actually working" in c.value for c in at.caption)
          and any("/health" in s.value for s in [*at.success, *at.error, *at.info]))
    check("Arabic report keeps its right-to-left markup", any('dir="rtl"' in m.value for m in at.markdown))

print(f"\n{passed}/{total} checks passed")
sys.exit(0 if passed == total else 1)
