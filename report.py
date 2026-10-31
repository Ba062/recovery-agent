"""Bilingual incident report (English + Arabic), written after each run, saved as Markdown.

The rule: every figure (counts, SAR amounts, times, versions, URLs) is inserted by CODE from the run
record. The model writes only the prose, and uses {placeholders} wherever a figure goes. Prose that
contains any digit or an unknown placeholder is rejected (one retry), so a number the model made up
can never reach the report. If the prose still fails, the report has the code-built facts only.

Usage:  report.save(record, Path("replays/run1"))  ->  replays/run1.report.md
"""

import html
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent
from service.db import RATES_TO_SAR

RIYADH = timezone(timedelta(hours=3))  # Saudi Arabia has no daylight saving time
DIGIT = re.compile(r"[0-9٠-٩۰-۹]")  # Western, Arabic-Indic and Persian digits
PLACEHOLDER = re.compile(r"\{(\w+)\}")
SECTIONS = ["what_happened", "root_cause", "evidence_note", "impact_note"]  # plus follow_ups (a list)
MAX_ATTEMPTS = 2
LOG_LINE = re.compile(r"^\s+(\d+)x \[(\w+)\] (\w+): (.*)$")  # read_logs summary: "  27x [DEBUG] payment: ..."


# ---------------------------------------------------------------- facts (all from code)
def riyadh_time(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(RIYADH).strftime("%Y-%m-%d %H:%M:%S (UTC+3)")


def sar(amount: float) -> str:
    return f"{amount:,.2f}"


# ---------------------------------------------------------------- counts with their noun (grammar in code)
# Arabic number-noun agreement depends on the count. Categories follow the Unicode CLDR plural rules
# for Arabic, which look at the LAST TWO digits above 100 (103 -> "103 طلبات", 111 -> "111 طلبًا").
#   zero: 0 طلب | one: طلب واحد | two: طلبان | few (3-10): 3 طلبات | many (11-99): 11 طلبًا | other (100, 101, 102...): 100 طلب
# "one" and "two" are written as words, the natural Arabic form. Dual in the nominative ("طلبان").
AR_NOUNS = {
    "order": {"zero": "{n} طلب", "one": "طلب واحد", "two": "طلبان", "few": "{n} طلبات",
              "many": "{n} طلبًا", "other": "{n} طلب"},
    "row": {"zero": "{n} قيد", "one": "قيد واحد", "two": "قيدان", "few": "{n} قيود",
            "many": "{n} قيدًا", "other": "{n} قيد"},
    "step": {"zero": "{n} خطوة", "one": "خطوة واحدة", "two": "خطوتان", "few": "{n} خطوات",
             "many": "{n} خطوةً", "other": "{n} خطوة"},
    "action": {"zero": "{n} إجراء", "one": "إجراء واحد", "two": "إجراءان", "few": "{n} إجراءات",
               "many": "{n} إجراءً", "other": "{n} إجراء"},
}
EN_NOUNS = {"order": ("order", "orders"), "row": ("ledger row", "ledger rows"),
            "step": ("step", "steps"), "action": ("action", "actions")}


def ar_plural_category(n: int) -> str:
    if n == 0:
        return "zero"
    if n == 1:
        return "one"
    if n == 2:
        return "two"
    if 3 <= n % 100 <= 10:
        return "few"
    if 11 <= n % 100 <= 99:
        return "many"
    return "other"  # 100-102, 200-202, 1000, ...


def ar_count(n: int, noun: str) -> str:
    """e.g. ar_count(38, "order") -> "38 طلبًا", ar_count(1, "order") -> "طلب واحد"."""
    return AR_NOUNS[noun][ar_plural_category(n)].format(n=n)


def en_count(n: int, noun: str) -> str:
    singular, plural = EN_NOUNS[noun]
    return f"{n} {singular if n == 1 else plural}"


def count_noun(fact_name: str) -> str | None:
    """Which noun a count fact is about (None = not a count)."""
    if fact_name == "steps":
        return "step"
    if fact_name == "fixes_applied":
        return "action"
    if fact_name.endswith("_count"):
        return "row" if "_ledger_" in fact_name else "order"
    return None


def facts(record: dict) -> dict[str, str]:
    """Every figure the report may show, by placeholder name. Missing data = no placeholder."""
    d, s = record["diagnosis"], record["stats"]
    f = {"investigation_started_at": riyadh_time(record["started_at"]), "duration_s": f"{s['seconds']:.1f}",
         "steps": str(s["steps"]), "fixes_applied": str(len(s["writes_applied"])),
         "confidence": f"{d['confidence']:.2f}", "usd_to_sar": f"{RATES_TO_SAR['USD']:.2f}"}
    for stage, money in [("alert", d.get("money_impact_before")), ("peak", d.get("money_impact_peak")),
                         ("end", d.get("money_impact_after"))]:
        if money is None:
            continue
        for key, short in [("unpaid_orders", "unpaid"), ("queued_orders", "queued"), ("ledger_overstatement", "ledger")]:
            f[f"{stage}_{short}_count"] = str(money[key]["count"])
            f[f"{stage}_{short}_sar"] = sar(money[key]["total_sar"])
    recovered = d.get("money_recovered")
    if recovered:
        for key, short in [("orders_paid", "paid"), ("ledger_corrected", "ledger")]:
            f[f"recovered_{short}_count"] = str(recovered[key]["count"])
            f[f"recovered_{short}_sar"] = sar(recovered[key]["total_sar"])
    for when in ("before", "after"):
        for key, value in (record.get(f"config_{when}") or {}).items():
            f[f"cfg_{key}_{when}"] = str(value)
    return f


def config_changes(record: dict) -> list[tuple[str, str, str]]:
    """(key, before, after) for every config value that changed during the run."""
    before, after = record.get("config_before") or {}, record.get("config_after") or {}
    return [(k, str(before[k]), str(after[k])) for k in before if k in after and before[k] != after[k]]


def monitor_lines(record: dict) -> list[tuple[str, str]]:
    """(rule, message) for each anomaly in the alert the monitor raised."""
    found = re.findall(r"^- \[\w+\] (\w+): (.*)$", record["alert"], flags=re.MULTILINE)
    return [(rule, msg) for rule, msg in found]


def log_lines(record: dict, limit: int = 3) -> list[str]:
    """The most frequent non-request log messages in the logs the agent actually read."""
    counts: dict[str, int] = {}
    for event in record["events"]:
        if event["kind"] != "tool_result" or event.get("tool") != "read_logs":
            continue
        for line in event["text"].splitlines():
            m = LOG_LINE.match(line)
            if m and m.group(3) != "api":
                text = f"[{m.group(2)}] {m.group(3)}: {m.group(4)[:140]}"
                counts[text] = max(counts.get(text, 0), int(m.group(1)))
    top = sorted(counts.items(), key=lambda item: -item[1])[:limit]
    return [f"{n}x {text}" for text, n in top]


# ---------------------------------------------------------------- fixed labels (no digits in here)
RULES_AR = {"payment_gap": "طلبات عولجت دون دفع", "messages_dropped": "طلبات فُقدت من الطابور",
            "ledger_mismatch": "عدم تطابق دفتر الأستاذ", "high_error_rate": "معدل أخطاء مرتفع",
            "worker_stalled": "توقف العامل", "queue_backlog": "تراكم في الطابور",
            "high_latency": "بطء في الاستجابة", "service_down": "الخدمة لا تستجيب"}

LABELS = {
    "en": {
        "title": "Incident report", "started": "Investigation started", "outcome": "Outcome", "duration": "Duration",
        "seconds": "s", "steps": "Agent steps", "confidence": "Confidence",
        "recovered": "Recovered (verified against the business invariants)",
        "escalated": "Escalated to a human", "not_recovered": "Not recovered",
        "recommend": "Diagnosis only (recommend mode, nothing was changed)",
        "what_happened": "What happened", "root_cause": "Root cause", "evidence": "Evidence",
        "monitor": "Monitor", "config": "Config changed during the run", "logs": "Logs",
        "actions": "Actions taken", "approved": "applied", "rejected": "rejected by the operator",
        "blocked": "blocked (same fix already failed twice)", "no_actions": "None.",
        "impact": "Business impact", "unpaid": "Unpaid orders", "queued": "Queued orders",
        "ledger": "Ledger overstated", "at_alert": "At alert", "peak": "Peak (just before the first fix)",
        "end": "End of run", "recovered_paid": "Recovered: orders paid by reprocessing",
        "recovered_ledger": "Recovered: ledger overstatement corrected", "currency": "SAR",
        "follow_ups": "Follow-ups",
        "no_prose": "(Prose unavailable: the model's text failed validation. The figures above are complete.)",
        "prose_skipped": "(Prose unavailable: none was requested for this run; eval runs skip it to save cost. "
                         "The figures above are complete.)",
        "footer": "All figures are inserted by code from the run record (USD converted at the peg of "
                  "{rate} SAR). The prose is written by Claude and contains no figures of its own.",
    },
    "ar": {
        "title": "تقرير الحادثة", "started": "بدء التحقيق", "outcome": "النتيجة", "duration": "المدة",
        "seconds": "ثانية", "steps": "خطوات الوكيل", "confidence": "درجة الثقة",
        "recovered": "تمت الاستعادة (وتم التحقق منها عبر مؤشرات الأعمال)",
        "escalated": "تم التصعيد إلى مهندس بشري", "not_recovered": "لم تتم الاستعادة",
        "recommend": "تشخيص فقط (وضع التوصية، لم يُغيَّر أي شيء)",
        "what_happened": "ما الذي حدث", "root_cause": "السبب الجذري", "evidence": "الأدلة",
        "monitor": "نظام المراقبة", "config": "إعدادات تغيّرت أثناء المعالجة", "logs": "السجلات",
        "actions": "الإجراءات المتخذة", "approved": "نُفِّذ", "rejected": "رفضه المشغّل",
        "blocked": "مُنع (فشل الإصلاح نفسه مرتين)", "no_actions": "لا يوجد.",
        "impact": "الأثر على الأعمال", "unpaid": "طلبات بلا دفع", "queued": "طلبات في الانتظار",
        "ledger": "مبالغ زائدة في دفتر الأستاذ", "at_alert": "عند التنبيه",
        "peak": "الذروة (قبل أول إصلاح مباشرة)", "end": "نهاية المعالجة",
        "recovered_paid": "المسترد: طلبات دُفعت بإعادة المعالجة",
        "recovered_ledger": "المسترد: تصحيح المبالغ الزائدة في دفتر الأستاذ", "currency": "ريال",
        "follow_ups": "إجراءات المتابعة",
        "no_prose": "(النص غير متاح: لم يجتز نص النموذج التحقق. الأرقام أعلاه مكتملة.)",
        "prose_skipped": "(النص غير متاح: لم يُطلب نص من النموذج لهذا التشغيل، إذ تتخطاه تشغيلات التقييم توفيرًا "
                         "للتكلفة. الأرقام أعلاه مكتملة.)",
        "footer": "جميع الأرقام مُدرجة آليًا من سجل التشغيل (تحويل الدولار بسعر الربط {rate} ريال). "
                  "النص من صياغة Claude ولا يحتوي على أي أرقام من عنده.",
    },
}


# ---------------------------------------------------------------- rendering (code puts in every figure)
def ltr(text: str, lang: str) -> str:
    """In the Arabic (right-to-left) version, keep a left-to-right snippet (English, code, a date, a URL)
    in one piece. Without this, the bidi algorithm pulls a leading number away from its sentence:
    "19 of 35 orders ... payment." shows the 19 at the far end, and "a → b" points the wrong way."""
    return f'<span dir="ltr">{text}</span>' if lang == "ar" else text


PLAIN_NUMBER = re.compile(r"^\d[\d,]*(\.\d+)?$")  # 38, 14,914.21, 0.95: display fine in Arabic as they are


def show(name: str, f: dict[str, str], lang: str) -> str:
    """One fact as it appears in the text: counts with their noun (Arabic grammar in code), plain
    numbers as they are, anything else (dates, URLs, versions) kept left-to-right in Arabic."""
    noun = count_noun(name)
    if noun:
        return ar_count(int(f[name]), noun) if lang == "ar" else en_count(int(f[name]), noun)
    return f[name] if PLAIN_NUMBER.match(f[name]) else ltr(f[name], lang)


def fill(text: str, f: dict[str, str], lang: str) -> str:
    return PLACEHOLDER.sub(lambda m: show(m.group(1), f, lang), text)


def render(record: dict, prose: dict | None, lang: str) -> str:
    L, f, d, s = LABELS[lang], facts(record), record["diagnosis"], record["stats"]
    if record.get("mode") == "recommend":
        outcome = L["recommend"]
    else:
        outcome = L["recovered"] if d["recovered"] else L["escalated"] if d["escalate"] else L["not_recovered"]
    p = prose[lang] if prose else None
    # No prose: asked for but unusable (save() records the attempt in record["report"]), or never asked for (eval runs).
    no_prose = L["no_prose"] if record.get("report") else L["prose_skipped"]

    def section(key: str) -> list[str]:
        return [fill(plain(p[key]), f, lang) if p else no_prose, ""]

    def count(name: str) -> str:
        """A count in a table: with its noun in Arabic ("38 طلبًا"), bare in English (the header says what)."""
        return show(name, f, lang) if lang == "ar" else f[name]

    out = [f"# {L['title']}", "", "| | |", "|---|---|",
           f"| {L['started']} | {ltr(f['investigation_started_at'], lang)} |", f"| {L['outcome']} | {outcome} |",
           f"| {L['duration']} | {f['duration_s']} {L['seconds']} |", f"| {L['steps']} | {count('steps')} |",
           f"| {L['confidence']} | {f['confidence']} |", "",
           f"## {L['what_happened']}", "", *section("what_happened"),
           f"## {L['root_cause']}", "", *section("root_cause"),
           f"## {L['evidence']}", ""]
    for rule, msg in monitor_lines(record):
        name = RULES_AR.get(rule, rule) + f" ({ltr(f'`{rule}`', lang)})" if lang == "ar" else f"`{rule}`"
        out.append(f"- {L['monitor']}: {name}: {ltr(f'`{msg}`', lang)}")
    for key, before, after in config_changes(record):
        out.append(f"- {L['config']}: " + ltr(f"`{key}`: `{before}` → `{after}`", lang))
    for line in log_lines(record):
        out.append(f"- {L['logs']}: {ltr(f'`{line}`', lang)}")
    out += ["", *section("evidence_note"), f"## {L['actions']}", ""]
    actions = ([f"{ltr(f'`{a}`', lang)} ({L['approved']})" for a in s["writes_applied"]]
               + [f"{ltr(f'`{a}`', lang)} ({L['rejected']})" for a in s["writes_rejected"]]
               + [f"{ltr(f'`{a}`', lang)} ({L['blocked']})" for a in s["writes_blocked"]])
    out += [f"- {a}" for a in actions] or [L["no_actions"]]
    cur = L["currency"]
    out += ["", f"## {L['impact']}", "", f"| | {L['unpaid']} | {L['queued']} | {L['ledger']} |", "|---|---|---|---|"]
    for stage, label in [("alert", L["at_alert"]), ("peak", L["peak"]), ("end", L["end"])]:
        if f"{stage}_unpaid_count" in f:
            cells = [f"{count(f'{stage}_{k}_count')} ({f[f'{stage}_{k}_sar']} {cur})" for k in ("unpaid", "queued", "ledger")]
            out.append(f"| {label} | " + " | ".join(cells) + " |")
    if "recovered_paid_count" in f:
        out += ["", "| | |", "|---|---|",
                f"| {L['recovered_paid']} | {count('recovered_paid_count')} ({f['recovered_paid_sar']} {cur}) |",
                f"| {L['recovered_ledger']} | {count('recovered_ledger_count')} ({f['recovered_ledger_sar']} {cur}) |"]
    out += ["", *section("impact_note"), f"## {L['follow_ups']}", ""]
    out += [f"- {fill(plain(item), f, lang)}" for item in as_items(p["follow_ups"])] if p else [no_prose]
    out += ["", "---", "", f"_{L['footer'].format(rate=f['usd_to_sar'])}_"]
    text = "\n".join(out)
    # Blank lines around the div so Markdown inside it still renders (GitHub, VS Code).
    return f'<div dir="rtl">\n\n{text}\n\n</div>' if lang == "ar" else text


# ---------------------------------------------------------------- prose (the model's only job)
# Flat fields (en_what_happened, ar_what_happened, ...): with nested {"en": {...}, "ar": {...}} objects
# the model sometimes sent a whole language as one JSON string, so that version was "missing".
FIELDS = SECTIONS + ["follow_ups"]
PROSE_TOOL = {"name": "write_report_prose",
              "description": "Submit the prose for the incident report, in English (en_*) and Arabic (ar_*).",
              "input_schema": {"type": "object",
                               "properties": {f"{lang}_{k}": ({"type": "array", "items": {"type": "string"}}
                                                              if k == "follow_ups" else {"type": "string"})
                                              for lang in ("en", "ar") for k in FIELDS},
                               "required": [f"{lang}_{k}" for lang in ("en", "ar") for k in FIELDS]}}


def plain(text: str) -> str:
    """Model prose is plain text. Escape <, > and & so it can never become HTML: the dashboard renders the
    report with HTML allowed (for the code's own rtl/ltr tags), so a tag written by the model would be live."""
    return html.escape(text, quote=False)


ITEM_TAG = re.compile(r"<item>(.*?)</item>", re.S)
CDATA = re.compile(r"<!\[CDATA\[(.*)\]\]>", re.S)


def split_items(text: str) -> list[str] | None:
    """Several follow-ups packed into one string, in formats the model has really sent:
    <item>...</item><item>...</item>, or a JSON list (possibly inside <![CDATA[...]]>). None = one plain item."""
    text = text.strip()
    wrapped = CDATA.fullmatch(text)
    if wrapped:
        text = wrapped.group(1).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(x).strip() for x in parsed if str(x).strip()]
    tagged = ITEM_TAG.findall(text)
    if tagged:
        return [t.strip() for t in tagged if t.strip()]
    return [text] if wrapped else None


def as_items(follow_ups):
    """Follow-ups as a clean list of strings. Anything else is returned as it is, for problems_in to reject."""
    if isinstance(follow_ups, str):
        split = split_items(follow_ups)
        if split is not None:
            return split
        lines = [line.strip().lstrip("-•*").strip() for line in follow_ups.splitlines()]  # one bullet string
        return [line for line in lines if line]
    if not isinstance(follow_ups, list):
        return follow_ups
    items = []
    for item in follow_ups:
        split = split_items(item) if isinstance(item, str) else None
        items += split if split is not None else [item]
    return items


def unflatten(tool_input: dict) -> dict:
    """{"en_root_cause": ...} -> {"en": {"root_cause": ...}, "ar": {...}}"""
    prose = {lang: {k: tool_input.get(f"{lang}_{k}") for k in FIELDS} for lang in ("en", "ar")}
    for part in prose.values():
        part["follow_ups"] = as_items(part["follow_ups"])
    return prose


def flatten(prose: dict) -> dict:
    """The reverse of unflatten (used by the tests to fake the model's tool call)."""
    return {f"{lang}_{k}": prose[lang][k] for lang in prose for k in prose[lang]}
PROSE_PROMPT = """You write the prose of a short incident postmortem for an order-processing system,
in English (en_* fields) and in Arabic (ar_* fields). Call write_report_prose with both versions.

Hard rule: write NO digits at all (no 0-9, no Arabic-Indic digits), not even in versions or URLs.
Wherever a figure, time, version or URL belongs, write its {placeholder} from the FACTS list,
e.g. "{peak_unpaid_count} orders worth {peak_unpaid_sar} SAR". Code inserts the real values.
Use only placeholder names from FACTS. Prose with a digit or an unknown placeholder is rejected.
Count placeholders (names ending in _count, plus steps and fixes_applied) already include their noun,
with correct grammar in each language ("38 orders" / "38 طلبًا", "طلب واحد"). Never write a noun right
after them: write "{peak_unpaid_count} were unpaid", not "{peak_unpaid_count} orders were unpaid".
investigation_started_at is when Nabd began investigating, after the monitor's alert. It is NOT when the
fault began (that was earlier and is not known here), so never present it as the start of the incident.

Sections, each in both languages (1-3 sentences each): what_happened, root_cause, evidence_note (what the evidence shows;
the evidence itself is listed by code), impact_note (business meaning of the impact table).
follow_ups: 2-4 short items to prevent a repeat (e.g. audit trail for config edits, an alert, a test).
Write as a calm engineer: plain, factual, no blame, no marketing.

Arabic: clear professional Modern Standard Arabic for Saudi readers, written naturally (not a word-for-
word translation). Keep code identifiers (tool names, config keys like payment_provider_url) in Latin
letters. Use "ريال" for SAR."""


# A noun written right after a count placeholder would be doubled ("38 طلبًا طلبًا", "38 orders orders").
NOUN_AFTER_COUNT = {"en": re.compile(r"\s*(orders?|ledger rows?|rows?|steps?|actions?)\b", re.IGNORECASE),
                    "ar": re.compile(r"\s*(طلب|قيد|قيود|خطو|إجراء)")}


def problems_in(prose: dict, f: dict[str, str]) -> list[str]:
    """Why prose is not acceptable (empty list = acceptable)."""
    problems = []
    for lang in ("en", "ar"):
        part = prose.get(lang)
        if not isinstance(part, dict):
            problems.append(f"{lang}: missing")
            continue
        follow_ups = part.get("follow_ups")
        if not isinstance(follow_ups, list) or not follow_ups:
            problems.append(f"{lang}.follow_ups: must be a non-empty list of strings")
            follow_ups = []
        texts = [(k, part.get(k)) for k in SECTIONS] + [(f"follow_ups[{i}]", t) for i, t in enumerate(follow_ups)]
        for name, text in texts:
            if not isinstance(text, str) or not text.strip():
                problems.append(f"{lang}.{name}: empty")
                continue
            unknown = set(PLACEHOLDER.findall(text)) - set(f)
            if unknown:
                problems.append(f"{lang}.{name}: unknown placeholder(s) {sorted(unknown)}")
            digits = DIGIT.findall(PLACEHOLDER.sub("", text))
            if digits:
                problems.append(f"{lang}.{name}: contains digits {digits} - use placeholders instead")
            for m in PLACEHOLDER.finditer(text):
                if count_noun(m.group(1)) and NOUN_AFTER_COUNT[lang].match(text[m.end():]):
                    problems.append(f"{lang}.{name}: {{{m.group(1)}}} already includes its noun - "
                                    f"don't write one after it")
    return problems


def write_prose(record: dict, client=None) -> tuple[dict | None, dict]:
    """Ask the model for the prose. Returns (prose or None, token stats). Never raises."""
    client = client or agent.client
    f, d = facts(record), record["diagnosis"]
    context = {"diagnosis": {k: d.get(k) for k in ("root_causes", "evidence", "summary", "recommended_actions",
                                                   "recovered", "escalate")},
               "actions": record["stats"]["writes_applied"], "rejected": record["stats"]["writes_rejected"],
               "config_changes": config_changes(record), "FACTS": f}
    messages = [{"role": "user", "content": json.dumps(context, ensure_ascii=False, indent=1)}]
    stats = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "attempts": 0}
    try:
        for _ in range(MAX_ATTEMPTS):
            stats["attempts"] += 1
            resp = client.beta.messages.create(
                model=agent.MODEL, max_tokens=16000, system=PROSE_PROMPT, tools=[PROSE_TOOL], messages=messages,
                output_config={"effort": "low"}, betas=[agent.FALLBACK_BETA], fallbacks="default")
            stats["input_tokens"] += resp.usage.input_tokens
            stats["output_tokens"] += resp.usage.output_tokens
            stats["cache_read_tokens"] += resp.usage.cache_read_input_tokens or 0
            stats["cache_write_tokens"] += resp.usage.cache_creation_input_tokens or 0
            messages.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if b.type == "tool_use"]
            if not calls:
                messages.append({"role": "user", "content": "Call write_report_prose now."})
                continue
            prose = unflatten(calls[0].input)
            problems = problems_in(prose, f)
            if not problems:
                return prose, stats
            stats["rejected_because"] = problems
            rejection = "Rejected, fix and call write_report_prose again:\n" + "\n".join(problems)
            messages.append({"role": "user", "content": [  # every tool_use needs a tool_result
                {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                 "content": rejection if b is calls[0] else "Ignored."} for b in calls]})
    except Exception as e:  # a failed report must never break saving the run
        stats["error"] = repr(e)
    return None, stats


# ---------------------------------------------------------------- save
def build(record: dict, prose: dict | None) -> dict:
    return {"en": render(record, prose, "en"), "ar": render(record, prose, "ar")}


def save(record: dict, stem: Path, client=None) -> Path:
    """Write <stem>.report.md (English, then Arabic). Records whether the model's prose was used."""
    prose, stats = write_prose(record, client)
    stats["est_cost_usd"] = agent.estimate_cost(stats)
    record["report"] = {"prose_used": prose is not None, "prose": prose, **stats}  # prose kept: re-render offline
    versions = build(record, prose)
    path = Path(f"{stem}.report.md")
    path.write_text(versions["en"] + "\n\n---\n\n" + versions["ar"] + "\n", encoding="utf-8")
    return path
