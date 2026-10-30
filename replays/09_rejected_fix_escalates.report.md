# Incident report

| | |
|---|---|
| Investigation started | 2026-10-30 18:08:15 (UTC+3) |
| Outcome | Escalated to a human |
| Duration | 14.3 s |
| Agent steps | 3 |
| Confidence | 0.90 |

## What happened

Nabd began investigating at 2026-10-30 18:08:15 (UTC+3) after the monitor alerted that 10 orders were unpaid. Orders were being marked processed without a payment being recorded, while /health stayed green. The incident was not recovered: Nabd applied 0 actions and escalated to a human.

## Root cause

Ops changed payment_provider_url to internal://payments-v2 during a provider migration, and that provider is unreachable. The worker logs the failed call but still marks the order processed.

## Evidence

- Monitor: `payment_gap`: `10 of 26 orders were processed but have no payment.`
- Logs: `41x [DEBUG] payment: provider call failed: payment provider unreachable at internal://payments-v2`

The change log shows the payment_provider_url edit by ops, and the worker log repeats "payment provider unreachable" errors. The payment gap check confirms processed orders with no payment, with error_rate at zero, which is why health looked normal. Confidence is 0.90.

## Actions taken

- `set_config {"key": "payment_provider_url", "value": "internal://payments-v1"}` (rejected by the operator)

## Business impact

| | Unpaid orders | Queued orders | Ledger overstated |
|---|---|---|---|
| At alert | 10 (8,009.11 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |
| End of run | 72 (42,670.57 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |

| | |
|---|---|
| Recovered: orders paid by reprocessing | 0 (0.00 SAR) |
| Recovered: ledger overstatement corrected | 0 (0.00 SAR) |

Unpaid orders grew from 10 orders worth 8,009.11 SAR at the alert to 72 orders worth 42,670.57 SAR at the end. No orders have been recovered yet (0 orders paid), so this revenue is still outstanding.

## Follow-ups

- Revert payment_provider_url to the previous provider, or restore connectivity to the new one, then reprocess all unpaid orders and run check_invariants.
- Do not restart the worker or reprocess orders until the provider works.
- Make the worker leave an order unprocessed when the payment call fails, instead of marking it processed.
- Add an alert on processed orders without a payment, and an audit trail with review for config edits.

---

_All figures are inserted by code from the run record (USD converted at the peg of 3.75 SAR). The prose is written by Claude and contains no figures of its own._

---

<div dir="rtl">

# تقرير الحادثة

| | |
|---|---|
| بدء التحقيق | <span dir="ltr">2026-10-30 18:08:15 (UTC+3)</span> |
| النتيجة | تم التصعيد إلى مهندس بشري |
| المدة | 14.3 ثانية |
| خطوات الوكيل | 3 خطوات |
| درجة الثقة | 0.90 |

## ما الذي حدث

بدأ نبض التحقيق في <span dir="ltr">2026-10-30 18:08:15 (UTC+3)</span> بعد تنبيه المراقبة بأن 10 طلبات غير مدفوعة. كانت الطلبات تُوسَم بأنها معالجة دون تسجيل أي دفعة، بينما بقي /health سليمًا. لم يتم التعافي: طبّق نبض 0 إجراء، وصعّد الحالة إلى مختص بشري.

## السبب الجذري

غيّر فريق العمليات قيمة payment_provider_url إلى <span dir="ltr">internal://payments-v2</span> أثناء ترحيل مزوّد الدفع، وهذا المزوّد لا يمكن الوصول إليه. يسجّل العامل فشل الاتصال لكنه يستمر في وسم الطلب بأنه معالج.

## الأدلة

- نظام المراقبة: طلبات عولجت دون دفع (<span dir="ltr">`payment_gap`</span>): <span dir="ltr">`10 of 26 orders were processed but have no payment.`</span>
- السجلات: <span dir="ltr">`41x [DEBUG] payment: provider call failed: payment provider unreachable at internal://payments-v2`</span>

يُظهر سجل التغييرات تعديل payment_provider_url من فريق العمليات، ويكرر سجل العامل أخطاء عدم إمكانية الوصول إلى مزوّد الدفع. ويؤكد فحص فجوة الدفع وجود طلبات معالجة بلا دفعة، مع بقاء error_rate عند الصفر، ولهذا بدت الصحة طبيعية. مستوى الثقة 0.90.

## الإجراءات المتخذة

- <span dir="ltr">`set_config {"key": "payment_provider_url", "value": "internal://payments-v1"}`</span> (رفضه المشغّل)

## الأثر على الأعمال

| | طلبات بلا دفع | طلبات في الانتظار | مبالغ زائدة في دفتر الأستاذ |
|---|---|---|---|
| عند التنبيه | 10 طلبات (8,009.11 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |
| نهاية المعالجة | 72 طلبًا (42,670.57 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |

| | |
|---|---|
| المسترد: طلبات دُفعت بإعادة المعالجة | 0 طلب (0.00 ريال) |
| المسترد: تصحيح المبالغ الزائدة في دفتر الأستاذ | 0 قيد (0.00 ريال) |

ارتفعت الطلبات غير المدفوعة من 10 طلبات بقيمة 8,009.11 ريال عند التنبيه إلى 72 طلبًا بقيمة 42,670.57 ريال في النهاية. لم يُسترد أي طلب بعد (0 طلب مدفوعة)، فهذه الإيرادات ما زالت معلّقة.

## إجراءات المتابعة

- إعادة payment_provider_url إلى المزوّد السابق أو إصلاح الاتصال بالمزوّد الجديد، ثم إعادة معالجة كل الطلبات غير المدفوعة وتشغيل check_invariants.
- عدم إعادة تشغيل العامل أو إعادة معالجة الطلبات قبل أن يعمل المزوّد.
- جعل العامل يترك الطلب غير معالج عند فشل الاتصال بالدفع بدل وسمه كمعالج.
- إضافة تنبيه للطلبات المعالجة بلا دفعة، وسجل تدقيق ومراجعة لتعديلات الإعدادات.

---

_جميع الأرقام مُدرجة آليًا من سجل التشغيل (تحويل الدولار بسعر الربط 3.75 ريال). النص من صياغة Claude ولا يحتوي على أي أرقام من عنده._

</div>
