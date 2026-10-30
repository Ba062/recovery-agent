# Incident report

| | |
|---|---|
| Investigation started | 2026-10-30 16:21:07 (UTC+3) |
| Outcome | Recovered (verified against the business invariants) |
| Duration | 33.3 s |
| Agent steps | 6 |
| Confidence | 0.90 |

## What happened

The monitor alerted that 13 orders were processed without payment, and Nabd began investigating at 2026-10-30 16:21:07 (UTC+3). The unpaid count peaked at 42 orders worth 29,651.30 SAR, while /health stayed green throughout.

## Root cause

payment_provider_url was changed outside the tracked change log from internal://payments-v1 to internal://payments-v2, which is unreachable. The worker kept processing orders even though every payment call failed.

## Evidence

- Monitor: `payment_gap`: `12 of 27 orders were processed but have no payment.`
- Config changed during the run: `payment_provider_url`: `internal://payments-v2` → `internal://payments-v1`
- Logs: `21x [DEBUG] payment: provider call failed: payment provider unreachable at internal://payments-v2`
- Logs: `1x [INFO] config: config reloaded`

The config log shows the reload that changed payment_provider_url, and the payment logs show repeated "provider unreachable" errors for the new address. The change does not appear in recent_changes, so its author is not yet known.

## Actions taken

- `set_config {"key": "payment_provider_url", "value": "internal://payments-v1"}` (applied)
- `reprocess_orders {"order_ids": "all_unpaid"}` (applied)

## Business impact

| | Unpaid orders | Queued orders | Ledger overstated |
|---|---|---|---|
| At alert | 13 (9,967.86 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |
| Peak (just before the first fix) | 42 (29,651.30 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |
| End of run | 0 (0.00 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |

| | |
|---|---|
| Recovered: orders paid by reprocessing | 44 (30,752.55 SAR) |
| Recovered: ledger overstatement corrected | 0 (0.00 SAR) |

At the peak, 29,651.30 SAR of orders had been processed without payment. After the fix, 44 orders worth 30,752.55 SAR were paid, and 0 orders remain unpaid.

## Follow-ups

- Find out who changed payment_provider_url and why, and record that edit in an audit trail.
- Add a payment provider reachability check to /health and alert on payment failures.
- Validate config values before they are applied.
- Stop the worker from processing orders when payment fails, and add a test for this case.

---

_All figures are inserted by code from the run record (USD converted at the peg of 3.75 SAR). The prose is written by Claude and contains no figures of its own._

---

<div dir="rtl">

# تقرير الحادثة

| | |
|---|---|
| بدء التحقيق | <span dir="ltr">2026-10-30 16:21:07 (UTC+3)</span> |
| النتيجة | تمت الاستعادة (وتم التحقق منها عبر مؤشرات الأعمال) |
| المدة | 33.3 ثانية |
| خطوات الوكيل | 6 خطوات |
| درجة الثقة | 0.90 |

## ما الذي حدث

نبّه المراقب إلى معالجة 13 طلبًا دون دفع، وبدأ Nabd التحقيق في <span dir="ltr">2026-10-30 16:21:07 (UTC+3)</span>. بلغ عدد الطلبات غير المدفوعة ذروته عند 42 طلبًا بقيمة 29,651.30 ريال، وبقي /health أخضر طوال الوقت.

## السبب الجذري

جرى تغيير payment_provider_url خارج سجل التغييرات المعتمد من <span dir="ltr">internal://payments-v1</span> إلى <span dir="ltr">internal://payments-v2</span> وهو عنوان لا يمكن الوصول إليه. واصل العامل (worker) معالجة الطلبات رغم فشل كل استدعاءات الدفع.

## الأدلة

- نظام المراقبة: طلبات عولجت دون دفع (<span dir="ltr">`payment_gap`</span>): <span dir="ltr">`12 of 27 orders were processed but have no payment.`</span>
- إعدادات تغيّرت أثناء المعالجة: <span dir="ltr">`payment_provider_url`: `internal://payments-v2` → `internal://payments-v1`</span>
- السجلات: <span dir="ltr">`21x [DEBUG] payment: provider call failed: payment provider unreachable at internal://payments-v2`</span>
- السجلات: <span dir="ltr">`1x [INFO] config: config reloaded`</span>

يُظهر سجل الإعدادات إعادة التحميل التي غيّرت payment_provider_url، وتُظهر سجلات الدفع أخطاء متكررة بأن مزوّد الدفع غير متاح على العنوان الجديد. لا يظهر التغيير في recent_changes، لذلك لم يُعرف بعد من أجراه.

## الإجراءات المتخذة

- <span dir="ltr">`set_config {"key": "payment_provider_url", "value": "internal://payments-v1"}`</span> (نُفِّذ)
- <span dir="ltr">`reprocess_orders {"order_ids": "all_unpaid"}`</span> (نُفِّذ)

## الأثر على الأعمال

| | طلبات بلا دفع | طلبات في الانتظار | مبالغ زائدة في دفتر الأستاذ |
|---|---|---|---|
| عند التنبيه | 13 طلبًا (9,967.86 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |
| الذروة (قبل أول إصلاح مباشرة) | 42 طلبًا (29,651.30 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |
| نهاية المعالجة | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |

| | |
|---|---|
| المسترد: طلبات دُفعت بإعادة المعالجة | 44 طلبًا (30,752.55 ريال) |
| المسترد: تصحيح المبالغ الزائدة في دفتر الأستاذ | 0 قيد (0.00 ريال) |

عند الذروة عولجت طلبات بقيمة 29,651.30 ريال دون دفع. بعد الإصلاح جرى سداد 44 طلبًا بقيمة 30,752.55 ريال، ولم يبقَ غير مدفوع سوى 0 طلب.

## إجراءات المتابعة

- معرفة من غيّر payment_provider_url وسبب ذلك، وتسجيل مثل هذه التعديلات في سجل تدقيق.
- إضافة فحص لإمكانية الوصول إلى مزوّد الدفع ضمن /health وتنبيه عند فشل الدفع.
- التحقق من صحة قيم الإعدادات قبل تطبيقها.
- منع العامل من معالجة الطلبات عند فشل الدفع، وإضافة اختبار لهذه الحالة.

---

_جميع الأرقام مُدرجة آليًا من سجل التشغيل (تحويل الدولار بسعر الربط 3.75 ريال). النص من صياغة Claude ولا يحتوي على أي أرقام من عنده._

</div>
