# Incident report

| | |
|---|---|
| Investigation started | 2026-10-30 18:45:25 (UTC+3) |
| Outcome | Recovered (verified against the business invariants) |
| Duration | 37.5 s |
| Agent steps | 6 |
| Confidence | 0.85 |

## What happened

After an ops change to payment_provider_url, orders were processed without payment while /health stayed green. Nabd began investigating at 2026-10-30 18:45:25 (UTC+3), reverted the setting, restarted the worker and reprocessed the affected orders, taking 3 actions in 37.5 seconds.

## Root cause

The provider migration changed payment_provider_url from internal://payments-v1 to internal://payments-v2, which is unreachable, so payment calls failed. The worker also stalled in a wait state and the queue grew; the cause of the stall is not fully determined and may be a side effect of the provider failures.

## Evidence

- Monitor: `payment_gap`: `20 of 48 orders were processed but have no payment.`
- Config changed during the run: `payment_provider_url`: `internal://payments-v2` → `internal://payments-v1`
- Logs: `20x [DEBUG] payment: provider call failed: payment provider unreachable at internal://payments-v2`
- Logs: `1x [INFO] pipeline: pipeline started`
- Logs: `1x [INFO] config: config reloaded`

The config history shows the change, and the logs show repeated provider-unreachable failures at DEBUG level. The health check and error rate showed nothing wrong, so the fault was not visible there.

## Actions taken

- `set_config {"key": "payment_provider_url", "value": "internal://payments-v1"}` (applied)
- `restart_worker {}` (applied)
- `reprocess_orders {"order_ids": "all_unpaid"}` (applied)

## Business impact

| | Unpaid orders | Queued orders | Ledger overstated |
|---|---|---|---|
| At alert | 20 (14,346.75 SAR) | 12 (8,038.53 SAR) | 0 (0.00 SAR) |
| Peak (just before the first fix) | 20 (14,346.75 SAR) | 34 (20,418.35 SAR) | 0 (0.00 SAR) |
| End of run | 0 (0.00 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |

| | |
|---|---|
| Recovered: orders paid by reprocessing | 20 (14,346.75 SAR) |
| Recovered: ledger overstatement corrected | 0 (0.00 SAR) |

At peak, 20 orders were unpaid, worth 14,346.75 SAR, and 34 orders were queued, worth 20,418.35 SAR. After recovery, 20 orders were paid, worth 14,346.75 SAR, and 0 orders remain unpaid.

## Follow-ups

- <item>Keep an audit trail and require review for config edits such as payment_provider_url.</item><item>Alert on payment provider failures, which are currently logged only at DEBUG.</item><item>Confirm with ops that the new endpoint is reachable before retrying the migration.</item><item>Investigate why the worker enters a wait state when the provider fails, and add a test for it.</item>

---

_All figures are inserted by code from the run record (USD converted at the peg of 3.75 SAR). The prose is written by Claude and contains no figures of its own._

---

<div dir="rtl">

# تقرير الحادثة

| | |
|---|---|
| بدء التحقيق | <span dir="ltr">2026-10-30 18:45:25 (UTC+3)</span> |
| النتيجة | تمت الاستعادة (وتم التحقق منها عبر مؤشرات الأعمال) |
| المدة | 37.5 ثانية |
| خطوات الوكيل | 6 خطوات |
| درجة الثقة | 0.85 |

## ما الذي حدث

بعد تغيير أجراه فريق العمليات على payment_provider_url، جرت معالجة طلبات دون دفع بينما ظل /health سليمًا. بدأ نبض التحقيق عند <span dir="ltr">2026-10-30 18:45:25 (UTC+3)</span>، وأعاد الإعداد إلى قيمته السابقة، وأعاد تشغيل العامل، وأعاد معالجة الطلبات المتأثرة، وذلك عبر 3 إجراءات خلال 37.5 ثانية.

## السبب الجذري

غيّر ترحيل مزود الدفع قيمة payment_provider_url من <span dir="ltr">internal://payments-v1</span> إلى <span dir="ltr">internal://payments-v2</span> وهو عنوان لا يمكن الوصول إليه، فأخفقت مكالمات الدفع. كما توقف العامل في حالة انتظار وتراكمت الطلبات في الطابور؛ ولم يتحدد سبب التوقف بالكامل، وقد يكون أثرًا جانبيًا لإخفاق المزود.

## الأدلة

- نظام المراقبة: طلبات عولجت دون دفع (<span dir="ltr">`payment_gap`</span>): <span dir="ltr">`20 of 48 orders were processed but have no payment.`</span>
- إعدادات تغيّرت أثناء المعالجة: <span dir="ltr">`payment_provider_url`: `internal://payments-v2` → `internal://payments-v1`</span>
- السجلات: <span dir="ltr">`20x [DEBUG] payment: provider call failed: payment provider unreachable at internal://payments-v2`</span>
- السجلات: <span dir="ltr">`1x [INFO] pipeline: pipeline started`</span>
- السجلات: <span dir="ltr">`1x [INFO] config: config reloaded`</span>

يُظهر سجل الإعدادات التغيير، وتُظهر السجلات إخفاقًا متكررًا في الوصول إلى المزود بمستوى DEBUG. لم يُظهر فحص الصحة ولا معدل الأخطاء أي مشكلة، لذلك لم يكن العطل ظاهرًا فيهما.

## الإجراءات المتخذة

- <span dir="ltr">`set_config {"key": "payment_provider_url", "value": "internal://payments-v1"}`</span> (نُفِّذ)
- <span dir="ltr">`restart_worker {}`</span> (نُفِّذ)
- <span dir="ltr">`reprocess_orders {"order_ids": "all_unpaid"}`</span> (نُفِّذ)

## الأثر على الأعمال

| | طلبات بلا دفع | طلبات في الانتظار | مبالغ زائدة في دفتر الأستاذ |
|---|---|---|---|
| عند التنبيه | 20 طلبًا (14,346.75 ريال) | 12 طلبًا (8,038.53 ريال) | 0 قيد (0.00 ريال) |
| الذروة (قبل أول إصلاح مباشرة) | 20 طلبًا (14,346.75 ريال) | 34 طلبًا (20,418.35 ريال) | 0 قيد (0.00 ريال) |
| نهاية المعالجة | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |

| | |
|---|---|
| المسترد: طلبات دُفعت بإعادة المعالجة | 20 طلبًا (14,346.75 ريال) |
| المسترد: تصحيح المبالغ الزائدة في دفتر الأستاذ | 0 قيد (0.00 ريال) |

عند الذروة كان 20 طلبًا غير مدفوعة بقيمة 14,346.75 ريال، و34 طلبًا في الطابور بقيمة 20,418.35 ريال. بعد الاستعادة جرى دفع 20 طلبًا بقيمة 14,346.75 ريال، ولم يتبقَّ غير مدفوع سوى 0 طلب.

## إجراءات المتابعة

- <item>الاحتفاظ بسجل تدقيق لتعديلات الإعدادات مثل payment_provider_url واشتراط مراجعتها.</item><item>إضافة تنبيه على إخفاقات مزود الدفع التي تُسجَّل حاليًا بمستوى DEBUG فقط.</item><item>التأكد مع فريق العمليات من إمكانية الوصول إلى العنوان الجديد قبل إعادة محاولة الترحيل.</item><item>فحص سبب دخول العامل في حالة انتظار عند إخفاق المزود، وإضافة اختبار لذلك.</item>

---

_جميع الأرقام مُدرجة آليًا من سجل التشغيل (تحويل الدولار بسعر الربط 3.75 ريال). النص من صياغة Claude ولا يحتوي على أي أرقام من عنده._

</div>
