# Incident report

| | |
|---|---|
| Investigation started | 2026-10-30 18:43:32 (UTC+3) |
| Outcome | Recovered (verified against the business invariants) |
| Duration | 27.4 s |
| Agent steps | 5 |
| Confidence (model's self-assessment) | 93% |

## What happened

After deploy v1.4 went live, the monitor flagged ledger amounts that did not match their orders. Nabd began investigating at 2026-10-30 18:43:32 (UTC+3), rolled the deploy back to v1.3, and repaired the affected ledger rows in 5 steps.

## Root cause

The ledger sync refactor of currency handling in v1.4 scaled amounts up by a factor of one hundred, so ledger rows were written far above the real order amounts.

## Evidence

- Monitor: `ledger_mismatch`: `17 ledger entries don't match their order amount.`
- Config changed during the run: `version`: `v1.4` → `v1.3`
- Logs: `1x [DEBUG] sync: ledger synced`

The change log shows the refactor shipped just before the mismatches, and the sync log shows the affected rows were written by v1.4. A sample order's ledger amount was one hundred times its order amount. The invariant checks passed after the repair.

## Actions taken

- `rollback_deploy {}` (applied)
- `repair_ledger {"order_ids": "all_flagged"}` (applied)

## Business impact

| | Unpaid orders | Queued orders | Ledger overstated |
|---|---|---|---|
| At alert | 0 (0.00 SAR) | 0 (0.00 SAR) | 17 (474,942.60 SAR) |
| Peak (just before the first fix) | 0 (0.00 SAR) | 0 (0.00 SAR) | 17 (474,942.60 SAR) |
| End of run | 0 (0.00 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |

| | |
|---|---|
| Recovered: orders paid by reprocessing | 0 (0.00 SAR) |
| Recovered: ledger overstatement corrected | 17 (474,942.60 SAR) |

17 ledger rows had inflated ledger entries, overstating the ledger by 474,942.60 SAR. No orders were left unpaid or queued, and after the repair 0 ledger rows remain mismatched.

## Follow-ups

- Fix the amount scaling bug in the ledger sync before redeploying v1.4.
- Add an automated test that compares ledger amounts to order amounts.
- Add an alert for any ledger-versus-order mismatch so it is caught at the first bad row.
- Keep an audit trail of version and config changes.

---

_All figures are inserted by code from the run record (USD converted at the peg of 3.75 SAR). The prose is written by Claude and contains no figures of its own._

---

<div dir="rtl">

# تقرير الحادثة

| | |
|---|---|
| بدء التحقيق | <span dir="ltr">2026-10-30 18:43:32 (UTC+3)</span> |
| النتيجة | تمت الاستعادة (وتم التحقق منها عبر مؤشرات الأعمال) |
| المدة | 27.4 ثانية |
| خطوات الوكيل | 5 خطوات |
| درجة الثقة (تقييم النموذج الذاتي) | 93% |

## ما الذي حدث

بعد نشر الإصدار <span dir="ltr">v1.4</span> رصد المراقب مبالغ في دفتر الأستاذ لا تطابق مبالغ الطلبات. بدأ نبض التحقيق عند <span dir="ltr">2026-10-30 18:43:32 (UTC+3)</span>، ثم تراجع عن النشر إلى <span dir="ltr">v1.3</span>، وأصلح صفوف الدفتر المتأثرة خلال 5 خطوات.

## السبب الجذري

أدى تعديل معالجة العملات في مزامنة دفتر الأستاذ ضمن الإصدار <span dir="ltr">v1.4</span> إلى تضخيم المبالغ مئة ضعف، فكُتبت صفوف الدفتر بمبالغ أعلى بكثير من مبالغ الطلبات الفعلية.

## الأدلة

- نظام المراقبة: عدم تطابق دفتر الأستاذ (<span dir="ltr">`ledger_mismatch`</span>): <span dir="ltr">`17 ledger entries don't match their order amount.`</span>
- إعدادات تغيّرت أثناء المعالجة: <span dir="ltr">`version`: `v1.4` → `v1.3`</span>
- السجلات: <span dir="ltr">`1x [DEBUG] sync: ledger synced`</span>

يُظهر سجل التغييرات أن التعديل نُشر قبل ظهور حالات عدم التطابق مباشرة، ويُظهر سجل المزامنة أن الصفوف المتأثرة كتبها الإصدار <span dir="ltr">v1.4</span>. وكان مبلغ الدفتر في طلب العيّنة مئة ضعف مبلغ الطلب. ونجحت فحوص الثوابت بعد الإصلاح.

## الإجراءات المتخذة

- <span dir="ltr">`rollback_deploy {}`</span> (نُفِّذ)
- <span dir="ltr">`repair_ledger {"order_ids": "all_flagged"}`</span> (نُفِّذ)

## الأثر على الأعمال

| | طلبات بلا دفع | طلبات في الانتظار | مبالغ زائدة في دفتر الأستاذ |
|---|---|---|---|
| عند التنبيه | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 17 قيدًا (474,942.60 ريال) |
| الذروة (قبل أول إصلاح مباشرة) | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 17 قيدًا (474,942.60 ريال) |
| نهاية المعالجة | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |

| | |
|---|---|
| المسترد: طلبات دُفعت بإعادة المعالجة | 0 طلب (0.00 ريال) |
| المسترد: تصحيح المبالغ الزائدة في دفتر الأستاذ | 17 قيدًا (474,942.60 ريال) |

تأثر 17 قيدًا بقيود دفتر مضخّمة، مما رفع قيمة الدفتر بمقدار 474,942.60 ريال. لم تبقَ طلبات غير مدفوعة أو في الانتظار، وبعد الإصلاح بقي 0 قيد غير مطابق.

## إجراءات المتابعة

- إصلاح خطأ تحجيم المبالغ في مزامنة الدفتر قبل إعادة نشر <span dir="ltr">v1.4</span>.
- إضافة اختبار آلي يقارن مبالغ الدفتر بمبالغ الطلبات.
- إضافة تنبيه لأي عدم تطابق بين الدفتر والطلبات لاكتشافه عند أول صف خاطئ.
- الاحتفاظ بسجل تدقيق لتغييرات الإصدار والإعدادات.

---

_جميع الأرقام مُدرجة آليًا من سجل التشغيل (تحويل الدولار بسعر الربط 3.75 ريال). النص من صياغة Claude ولا يحتوي على أي أرقام من عنده._

</div>
