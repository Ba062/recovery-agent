# Incident report

| | |
|---|---|
| Investigation started | 2026-10-30 18:41:42 (UTC+3) |
| Outcome | Recovered (verified against the business invariants) |
| Duration | 36.0 s |
| Agent steps | 5 |
| Confidence (model's self-assessment) | 95% |

## What happened

After an alert, Nabd began investigating at 2026-10-30 18:41:42 (UTC+3). Orders were being marked as processed without a recorded payment: 11 orders were unpaid at alert time, rising to 42 orders at the peak. Nabd restored the previous configuration and reprocessed the unpaid orders, and none remain unpaid.

## Root cause

Ops changed payment_provider_url to internal://payments-v2 as part of a provider migration, but that endpoint was unreachable. The worker kept processing orders even though the payment call failed, so orders were completed without payment.

## Evidence

- Monitor: `payment_gap`: `11 of 27 orders were processed but have no payment.`
- Config changed during the run: `payment_provider_url`: `internal://payments-v2` → `internal://payments-v1`
- Logs: `33x [DEBUG] payment: provider call failed: payment provider unreachable at internal://payments-v2`

The config history shows the payment_provider_url change, and the worker log records the provider as unreachable, but only at DEBUG level. The payment gap check confirmed processed orders with no payment.

## Actions taken

- `set_config {"key": "payment_provider_url", "value": "internal://payments-v1"}` (applied)
- `reprocess_orders {"order_ids": "all_unpaid"}` (applied)

## Business impact

| | Unpaid orders | Queued orders | Ledger overstated |
|---|---|---|---|
| At alert | 11 (8,477.97 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |
| Peak (just before the first fix) | 42 (29,262.66 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |
| End of run | 0 (0.00 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |

| | |
|---|---|
| Recovered: orders paid by reprocessing | 45 (30,268.80 SAR) |
| Recovered: ledger overstatement corrected | 0 (0.00 SAR) |

At the peak, 42 orders worth 29,262.66 SAR were processed without payment. After the fix, 45 orders worth 30,268.80 SAR were recovered as paid, and 0 orders were unpaid at the end.

## Follow-ups

- Verify that the new payment provider endpoint is reachable and validated before retrying the migration.
- Add an alert on payment provider call failures, which are currently logged only at DEBUG.
- Add an audit trail and a pre-apply reachability check for payment_provider_url edits.
- Make the worker stop marking orders processed when the payment call fails.

---

_All figures are inserted by code from the run record (USD converted at the peg of 3.75 SAR). The prose is written by Claude and contains no figures of its own._

---

<div dir="rtl">

# تقرير الحادثة

| | |
|---|---|
| بدء التحقيق | <span dir="ltr">2026-10-30 18:41:42 (UTC+3)</span> |
| النتيجة | تمت الاستعادة (وتم التحقق منها عبر مؤشرات الأعمال) |
| المدة | 36.0 ثانية |
| خطوات الوكيل | 5 خطوات |
| درجة الثقة (تقييم النموذج الذاتي) | 95% |

## ما الذي حدث

بدأ Nabd التحقيق عند <span dir="ltr">2026-10-30 18:41:42 (UTC+3)</span> بعد وصول تنبيه. كانت الطلبات تُعلَّم كمعالجة دون تسجيل دفعة، إذ بلغ عدد الطلبات غير المدفوعة وقت التنبيه 11 طلبًا، وارتفع إلى 42 طلبًا في الذروة. أعاد Nabd الإعداد السابق وأعاد معالجة الطلبات غير المدفوعة، ولم يتبقَّ أي طلب غير مدفوع.

## السبب الجذري

غيّر فريق العمليات قيمة payment_provider_url إلى <span dir="ltr">internal://payments-v2</span> ضمن الانتقال إلى مزوّد دفع جديد، لكن هذا العنوان كان غير قابل للوصول. واصل العامل (worker) معالجة الطلبات رغم فشل استدعاء الدفع، فاكتملت الطلبات دون دفع.

## الأدلة

- نظام المراقبة: طلبات عولجت دون دفع (<span dir="ltr">`payment_gap`</span>): <span dir="ltr">`11 of 27 orders were processed but have no payment.`</span>
- إعدادات تغيّرت أثناء المعالجة: <span dir="ltr">`payment_provider_url`: `internal://payments-v2` → `internal://payments-v1`</span>
- السجلات: <span dir="ltr">`33x [DEBUG] payment: provider call failed: payment provider unreachable at internal://payments-v2`</span>

يُظهر سجل الإعدادات تغيير payment_provider_url، ويسجّل سجل العامل أن المزوّد غير قابل للوصول، لكن على مستوى DEBUG فقط. وأكد فحص فجوة الدفع وجود طلبات معالجة دون دفع.

## الإجراءات المتخذة

- <span dir="ltr">`set_config {"key": "payment_provider_url", "value": "internal://payments-v1"}`</span> (نُفِّذ)
- <span dir="ltr">`reprocess_orders {"order_ids": "all_unpaid"}`</span> (نُفِّذ)

## الأثر على الأعمال

| | طلبات بلا دفع | طلبات في الانتظار | مبالغ زائدة في دفتر الأستاذ |
|---|---|---|---|
| عند التنبيه | 11 طلبًا (8,477.97 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |
| الذروة (قبل أول إصلاح مباشرة) | 42 طلبًا (29,262.66 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |
| نهاية المعالجة | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |

| | |
|---|---|
| المسترد: طلبات دُفعت بإعادة المعالجة | 45 طلبًا (30,268.80 ريال) |
| المسترد: تصحيح المبالغ الزائدة في دفتر الأستاذ | 0 قيد (0.00 ريال) |

في الذروة عولجت 42 طلبًا بقيمة 29,262.66 ريال دون دفع. وبعد الإصلاح تم استرداد 45 طلبًا كمدفوعة بقيمة 30,268.80 ريال، وبلغ عدد غير المدفوعة في النهاية 0 طلب.

## إجراءات المتابعة

- التحقق من إمكانية الوصول إلى عنوان مزوّد الدفع الجديد واعتماده قبل إعادة محاولة الانتقال.
- إضافة تنبيه على فشل استدعاءات مزوّد الدفع، فهي تُسجَّل حاليًا على مستوى DEBUG فقط.
- إضافة سجل تدقيق وفحص إمكانية الوصول قبل تطبيق أي تعديل على payment_provider_url.
- جعل العامل يتوقف عن وسم الطلبات كمعالجة عند فشل استدعاء الدفع.

---

_جميع الأرقام مُدرجة آليًا من سجل التشغيل (تحويل الدولار بسعر الربط 3.75 ريال). النص من صياغة Claude ولا يحتوي على أي أرقام من عنده._

</div>
