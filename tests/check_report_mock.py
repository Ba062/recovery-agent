"""Incident report checks with a fake model: no API key, no service needed.

The key property: every figure in BOTH the English and the Arabic report comes from code (the run
record), never from the model. Checked two ways: the code values appear in both versions, and once
every code-inserted value is removed from a version, not a single digit is left in it.
Run from the project root:  python tests/check_report_mock.py
"""
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]  # the project root (this file is in tests/)
sys.path.insert(0, str(ROOT))
import report  # noqa: E402

checks = {}


def money(unpaid, queued=(0, 0.0, 0.0), ledger=(0, 0.0, 0.0)):
    def one(count, sar, usd):
        by_cur = {k: v for k, v in (("SAR", sar), ("USD", usd)) if v}
        return {"count": count, "by_currency": by_cur, "total_sar": round(sar + usd * 3.75, 2)}
    return {"unpaid_orders": one(*unpaid), "queued_orders": one(*queued), "ledger_overstatement": one(*ledger)}


# A run record shaped like a real payment_drop act run (figures from the live run on 30 Oct).
RECORD = {
    "mode": "act", "started_at": "2026-10-30T09:18:05+00:00",
    "alert": "Health check: passing. 1 anomaly(ies), most urgent first:\n"
             "- [business] payment_gap: 21 of 35 orders were processed but have no payment.\n"
             "Business metrics: {\"orders_created\": 35}",
    "config_before": {"version": "v1.3", "payment_provider_url": "internal://payments-v2", "queue_max": 1000},
    "config_after": {"version": "v1.3", "payment_provider_url": "internal://payments-v1", "queue_max": 1000},
    "stats": {"seconds": 17.8, "steps": 5, "writes_rejected": [], "writes_blocked": [],
              "writes_applied": ['set_config {"key": "payment_provider_url", "value": "internal://payments-v1"}',
                                 'reprocess_orders {"order_ids": "all_unpaid"}']},
    "events": [{"kind": "tool_result", "tool": "read_logs", "text":
                "120 matching lines (of the last 5000). Most frequent messages:\n"
                "  51x [DEBUG] payment: provider call failed: payment provider unreachable at internal://payments-v2\n"
                "  60x [INFO] api: request\n"
                "  1x [INFO] config: config reloaded"}],
    "diagnosis": {"root_causes": ["bad URL"], "evidence": ["51 failures"], "summary": "s", "recommended_actions": [],
                  "confidence": 0.9, "recovered": True, "escalate": False,
                  "money_impact_before": money((21, 3755.27, 1366.71)),
                  "money_impact_peak": money((37, 5916.08, 2878.14)),
                  "money_recovered": {"orders_paid": {"count": 41, "by_currency": {"SAR": 6197.31, "USD": 3612.45},
                                                      "total_sar": 19744.0},
                                      "ledger_corrected": {"count": 0, "by_currency": {}, "total_sar": 0.0}},
                  "money_impact_after": money((0, 0.0, 0.0), queued=(1, 489.06, 0.0))},
}

GOOD = {
    "en": {"what_happened": "When the investigation started at {investigation_started_at}, orders were being processed "
                            "but not charged. At the peak {peak_unpaid_count} worth {peak_unpaid_sar} SAR were unpaid.",
           "root_cause": "payment_provider_url pointed at {cfg_payment_provider_url_before} instead of "
                         "{cfg_payment_provider_url_after}.",
           "evidence_note": "The provider calls failed quietly at DEBUG level while /health stayed green.",
           "impact_note": "Reprocessing charged {recovered_paid_count} ({recovered_paid_sar} SAR).",
           "follow_ups": ["Record every config edit in the deploy history.", "Alert on provider call failures."]},
    "ar": {"what_happened": "عند بدء التحقيق في {investigation_started_at} كانت الطلبات تُعالج دون تحصيل قيمتها، "
                            "وبلغ عددها في الذروة {peak_unpaid_count} بقيمة {peak_unpaid_sar} ريال.",
           "root_cause": "كان الإعداد payment_provider_url يشير إلى {cfg_payment_provider_url_before} "
                         "بدلًا من {cfg_payment_provider_url_after}.",
           "evidence_note": "فشلت طلبات مزوّد الدفع بصمت في سجلات DEBUG بينما بقي فحص /health سليمًا.",
           "impact_note": "حُصّلت بإعادة المعالجة قيمة {recovered_paid_count} بإجمالي {recovered_paid_sar} ريال.",
           "follow_ups": ["توثيق كل تعديل على الإعدادات في سجل النشر.", "تنبيه عند فشل طلبات مزوّد الدفع."]},
}


def fake_client(*inputs):
    """A client whose replies call write_report_prose with each input in turn (an Exception = raise)."""
    sent = []

    def create(**kw):
        sent.append({**kw, "messages": list(kw["messages"])})  # snapshot: the real list keeps growing
        item = inputs[len(sent) - 1]
        if isinstance(item, Exception):
            raise item
        block = NS(type="tool_use", name="write_report_prose", input=report.flatten(item), id=f"r{len(sent)}")
        return NS(content=[block], usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
                                            cache_creation_input_tokens=0))
    return NS(beta=NS(messages=NS(create=create))), sent


def inserted_values(record):
    """Every string the code inserts into the report."""
    values = list(report.facts(record).values())
    values += [msg for _, msg in report.monitor_lines(record)] + report.log_lines(record)
    values += [v for change in report.config_changes(record) for v in change]
    values += record["stats"]["writes_applied"] + record["stats"]["writes_rejected"] + record["stats"]["writes_blocked"]
    return values


def leftover_digits(text, record):
    """Digits left after removing every code-inserted value. A value is removed only where it is
    not part of a longer number, so the code value "0" can't hide the "0" of a made-up "40"."""
    for value in sorted(inserted_values(record), key=len, reverse=True):  # longest first: "16,709.10" before "16"
        # Not touching another digit, and not joined to one by "," or "." ("16,709" / "0.90").
        text = re.sub(rf"(?<!\d)(?<!\d[.,]){re.escape(value)}(?!\d)(?![.,]\d)", "", text)
    return report.DIGIT.findall(text)


# Code values the report must show, in both languages.
EXPECTED = ["21", "8,880.43", "37", "16,709.10", "41", "19,744.00", "489.06", "17.8", "5", "0.90", "3.75",
            "2026-10-30 12:18:05 (UTC+3)", "internal://payments-v2", "internal://payments-v1",
            "51x [DEBUG] payment: provider call failed"]

# ---- 1. good prose: used, every figure from code, nothing else
client, sent = fake_client(GOOD)
prose, stats = report.write_prose(RECORD, client)
versions = report.build(RECORD, prose)
checks["valid prose accepted first time"] = prose == GOOD and stats["attempts"] == 1
checks["no temperature, effort low, fallback beta"] = ("temperature" not in sent[0]
                                                      and sent[0]["output_config"] == {"effort": "low"}
                                                      and sent[0]["betas"] == [report.agent.FALLBACK_BETA])
for lang in ("en", "ar"):
    text = versions[lang]
    missing = [v for v in EXPECTED if v not in text]
    if missing:
        print(f"  {lang} missing: {missing}")
    checks[f"{lang}: every code figure present (counts, SAR, time, duration, URLs, log count)"] = not missing
    left = leftover_digits(text, RECORD)
    if left:
        print(f"  {lang} digits not from code: {left}")
    checks[f"{lang}: after removing code-inserted values, NO digit is left"] = not left
    checks[f"{lang}: every placeholder filled"] = not report.PLACEHOLDER.search(text)
checks["en: peak table row = code values"] = (
    "| Peak (just before the first fix) | 37 (16,709.10 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |" in versions["en"])
checks["ar: peak table row = code values, every count with its noun"] = (
    "| الذروة (قبل أول إصلاح مباشرة) | 37 طلبًا (16,709.10 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |"
    in versions["ar"])
checks["ar: other counts with grammar (one queued order, steps, recovered)"] = all(s in versions["ar"] for s in [
    "| طلب واحد (489.06 ريال) |", "| خطوات الوكيل | 5 خطوات |", "| 41 طلبًا (19,744.00 ريال) |", "| 0 قيد (0.00 ريال) |"])
checks["ar: no bare count left in any table cell"] = not re.search(r"\| \d+ \(", versions["ar"])


def has_value(text, value):
    """value appears as itself, not as part of a longer number (same boundary rule as leftover_digits)."""
    return bool(re.search(rf"(?<!\d)(?<!\d[.,]){re.escape(value)}(?!\d)(?![.,]\d)", text))


facts_now = report.facts(RECORD)
non_counts = [v for name, v in facts_now.items() if not report.count_noun(name)]
checks["en and ar show the SAME non-count values (counts are checked via their Arabic phrase)"] = all(
    has_value(versions["en"], v) == has_value(versions["ar"], v)
    for v in non_counts + [m for _, m in report.monitor_lines(RECORD)] + report.log_lines(RECORD))
LTR = '<span dir="ltr">'
checks["ar wrapped right-to-left; plain numbers in prose NOT wrapped"] = (
    versions["ar"].startswith('<div dir="rtl">')
    and "وبلغ عددها في الذروة 37 طلبًا بقيمة 16,709.10 ريال" in versions["ar"]
    and not re.search(r'<span dir="ltr">[\d,.]+</span>', versions["ar"]))
checks["ar: dates and URLs in prose still kept left-to-right"] = (
    f"{LTR}2026-10-30 12:18:05 (UTC+3)</span> كانت" in versions["ar"]
    and f"{LTR}internal://payments-v2</span>" in versions["ar"])
checks["label renamed: 'Investigation started' / 'بدء التحقيق'"] = (
    "| Investigation started | 2026-10-30 12:18:05 (UTC+3) |" in versions["en"]
    and f"| بدء التحقيق | {LTR}2026-10-30 12:18:05 (UTC+3)</span> |" in versions["ar"]
    and "| Started |" not in versions["en"] and "وقت البدء" not in versions["ar"])
checks["prompt says investigation_started_at is NOT when the fault began"] = (
    "investigation_started_at is when Nabd began investigating" in report.PROSE_PROMPT)
checks["ar: English/code snippets kept left-to-right (number stays at the start, arrow points right)"] = all(
    s in versions["ar"] for s in [
        f"{LTR}2026-10-30 12:18:05 (UTC+3)</span>",
        f"{LTR}`21 of 35 orders were processed but have no payment.`</span>",
        f"{LTR}`51x [DEBUG] payment: provider call failed",
        f"{LTR}`payment_provider_url`: `internal://payments-v2` → `internal://payments-v1`</span>",
        f'{LTR}`reprocess_orders {{"order_ids": "all_unpaid"}}`</span>'])
checks["en: no direction markup at all"] = "dir=" not in versions["en"]
checks["en prose filled with code values"] = "At the peak 37 orders worth 16,709.10 SAR were unpaid" in versions["en"]

# ---- 2. the model writes a figure itself -> rejected, retried
bad = {"en": {**GOOD["en"], "impact_note": "About 40 orders were unpaid."}, "ar": GOOD["ar"]}
client, sent = fake_client(bad, GOOD)
prose, stats = report.write_prose(RECORD, client)
checks["prose with a digit rejected, retry accepted"] = (prose == GOOD and stats["attempts"] == 2
                                                         and "digits" in " ".join(stats["rejected_because"]))
checks["rejection sent back as is_error tool_result"] = sent[1]["messages"][-1]["content"][0]["is_error"] is True
checks["Arabic-Indic digits rejected too"] = bool(report.problems_in(
    {"en": GOOD["en"], "ar": {**GOOD["ar"], "impact_note": "تأثر ٣٧ طلبًا."}}, report.facts(RECORD)))
checks["unknown placeholder rejected"] = any("unknown placeholder" in p for p in report.problems_in(
    {"en": {**GOOD["en"], "root_cause": "{made_up_total}"}, "ar": GOOD["ar"]}, report.facts(RECORD)))
half = report.unflatten({k: v for k, v in report.flatten(GOOD).items() if k.startswith("en_")})
checks["missing Arabic fields rejected"] = any(p.startswith("ar.") and "empty" in p
                                               for p in report.problems_in(half, report.facts(RECORD)))
as_string = {**report.flatten(GOOD), "ar_follow_ups": "- " + "\n- ".join(GOOD["ar"]["follow_ups"])}
checks["follow_ups sent as ONE bullet string -> read as the list (seen live)"] = (
    report.unflatten(as_string) == GOOD and not report.problems_in(report.unflatten(as_string), report.facts(RECORD)))
checks["follow_ups that is not a list -> one clear problem, not one per character"] = (
    report.problems_in({"en": {**GOOD["en"], "follow_ups": None}, "ar": GOOD["ar"]}, report.facts(RECORD))
    == ["en.follow_ups: must be a non-empty list of strings"])
doubled = {"en": {**GOOD["en"], "impact_note": "{recovered_paid_count} orders were charged."},
           "ar": {**GOOD["ar"], "impact_note": "دُفع {recovered_paid_count} طلبًا."}}
noun_problems = report.problems_in(doubled, report.facts(RECORD))
checks["noun written after a count placeholder rejected, in both languages"] = (
    sum("already includes its noun" in p for p in noun_problems) == 2)
checks["...but other words after a count are fine ('{x} من الطلبات', '{x} were unpaid')"] = not report.problems_in(
    {"en": {**GOOD["en"], "impact_note": "{recovered_paid_count} were charged."},
     "ar": {**GOOD["ar"], "impact_note": "دُفع {recovered_paid_count} من الطلبات."}}, report.facts(RECORD))

# ---- Arabic count grammar (CLDR plural categories: above 100 the last two digits decide)
AR_ORDERS = {0: "0 طلب", 1: "طلب واحد", 2: "طلبان", 3: "3 طلبات", 10: "10 طلبات", 11: "11 طلبًا",
             99: "99 طلبًا", 100: "100 طلب", 101: "101 طلب", 102: "102 طلب", 103: "103 طلبات",
             111: "111 طلبًا", 200: "200 طلب", 1000: "1000 طلب"}
wrong = {n: report.ar_count(n, "order") for n, want in AR_ORDERS.items() if report.ar_count(n, "order") != want}
if wrong:
    print(f"  wrong Arabic order counts: {wrong}")
checks["ar_count orders: 0, 1, 2, 3-10, 11-99, 100-102, 103, 111, 1000"] = not wrong
checks["ar_count feminine noun (step): واحدة, dual, plural, accusative"] = (
    [report.ar_count(n, "step") for n in (1, 2, 5, 12)] == ["خطوة واحدة", "خطوتان", "5 خطوات", "12 خطوةً"])
checks["ar_count ledger rows and actions"] = (
    [report.ar_count(n, "row") for n in (2, 4, 24)] == ["قيدان", "4 قيود", "24 قيدًا"]
    and [report.ar_count(n, "action") for n in (1, 3)] == ["إجراء واحد", "3 إجراءات"])
checks["en_count: 1 order, 0 / 2 orders, ledger rows"] = (
    [report.en_count(n, "order") for n in (0, 1, 2)] == ["0 orders", "1 order", "2 orders"]
    and report.en_count(1, "row") == "1 ledger row")
checks["which facts are counts, and of what"] = (
    [report.count_noun(n) for n in ("peak_unpaid_count", "end_ledger_count", "recovered_ledger_count",
                                    "steps", "fixes_applied", "peak_unpaid_sar", "investigation_started_at")]
    == ["order", "row", "row", "step", "action", None, None])
one_order = {**RECORD, "diagnosis": {**RECORD["diagnosis"], "money_impact_peak": money((1, 50.0, 0.0))}}
v1 = report.build(one_order, GOOD)
checks["a count of ONE in prose: 'طلب واحد' (Arabic) / '1 order' (English)"] = (
    "وبلغ عددها في الذروة طلب واحد بقيمة 50.00 ريال" in v1["ar"]
    and "At the peak 1 order worth 50.00 SAR" in v1["en"])

checks["tool schema is flat: every en_/ar_ field required"] = (
    set(report.PROSE_TOOL["input_schema"]["required"]) == set(report.flatten(GOOD))
    and report.unflatten(report.flatten(GOOD)) == GOOD)

# ---- 3. never valid / API down -> facts-only report, figures still complete
for name, replies in [("both attempts bad", (bad, bad)), ("API error", (RuntimeError("API down"),))]:
    client, _ = fake_client(*replies)
    prose, stats = report.write_prose(RECORD, client)
    versions = report.build(RECORD, prose)
    checks[f"{name} -> no model prose, figures still complete in both"] = (
        prose is None and all(report.LABELS[lang]["no_prose"] in versions[lang] for lang in ("en", "ar"))
        and all(v in versions[lang] for v in EXPECTED for lang in ("en", "ar"))
        and "About 40" not in versions["en"])

# ---- 4. save writes one Markdown file with both versions, records report stats
with tempfile.TemporaryDirectory() as tmp:
    client, _ = fake_client(GOOD)
    record = {**RECORD}
    path = report.save(record, Path(tmp) / "run1", client)
    text = path.read_text(encoding="utf-8")
    checks["save -> run1.report.md with English then Arabic, stats in record"] = (
        path.name == "run1.report.md" and text.index("# Incident report") < text.index("# تقرير الحادثة")
        and record["report"]["prose_used"] is True and "est_cost_usd" in record["report"])

# ---- 5. escalated, no fix applied (e.g. unknown fault): no peak row, says "no actions"
esc = {**RECORD, "stats": {**RECORD["stats"], "writes_applied": []},
       "config_after": RECORD["config_before"],
       "diagnosis": {**RECORD["diagnosis"], "recovered": False, "escalate": True, "money_impact_peak": None}}
versions = report.build(esc, None)
checks["escalated run: outcome escalated, no peak row, no actions, no config change"] = all(
    report.LABELS[lang]["escalated"] in versions[lang] and report.LABELS[lang]["peak"] not in versions[lang]
    and report.LABELS[lang]["no_actions"] in versions[lang] and report.LABELS[lang]["config"] not in versions[lang]
    for lang in ("en", "ar"))

for name, ok in checks.items():
    print(("PASS " if ok else "FAIL ") + name)
print(f"{sum(bool(v) for v in checks.values())}/{len(checks)} passed")
sys.exit(0 if all(checks.values()) else 1)
