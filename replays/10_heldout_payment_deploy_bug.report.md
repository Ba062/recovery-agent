# Incident report

| | |
|---|---|
| Investigation started | 2026-10-30 19:29:16 (UTC+3) |
| Outcome | Recovered (verified against the business invariants) |
| Duration | 32.1 s |
| Agent steps | 5 |
| Confidence | 0.95 |

## What happened

After deploy v1.6, orders were marked processed without being paid. The monitor alerted at 12 orders unpaid, and Nabd began investigating at 2026-10-30 19:29:16 (UTC+3). Unpaid orders peaked at 28 orders worth 19,968.01 SAR.

## Root cause

The v1.6 payments client upgraded its HTTP library, and the new version could not serialize Decimal amounts. Every call to the payment provider therefore failed, yet the orders were still marked processed.

## Evidence

- Monitor: `payment_gap`: `12 of 26 orders were processed but have no payment.`
- Config changed during the run: `version`: `v1.6` → `v1.3`
- Logs: `21x [DEBUG] payment: provider call failed: Object of type Decimal is not JSON serializable`

Payment logs repeatedly show "Object of type Decimal is not JSON serializable", and the change log lists the v1.6 client upgrade as the latest change. The payment gap check confirmed processed orders with no payment.

## Actions taken

- `rollback_deploy {}` (applied)
- `reprocess_orders {"order_ids": "all_unpaid"}` (applied)

## Business impact

| | Unpaid orders | Queued orders | Ledger overstated |
|---|---|---|---|
| At alert | 12 (9,499.00 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |
| Peak (just before the first fix) | 28 (19,968.01 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |
| End of run | 0 (0.00 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |

| | |
|---|---|
| Recovered: orders paid by reprocessing | 35 (25,603.33 SAR) |
| Recovered: ledger overstatement corrected | 0 (0.00 SAR) |

At the peak, 28 orders were unpaid, worth 19,968.01 SAR. After the rollback to v1.3, 35 orders were reprocessed and paid, worth 25,603.33 SAR, and 0 orders remain unpaid.

## Follow-ups

- Fix Decimal serialization in the v1.6 payments client and add a test covering it before redeploying.
- Alert on payment provider call failures, which are currently logged only at DEBUG.
- Do not mark an order processed unless the payment call succeeded.

---

_All figures are inserted by code from the run record (USD converted at the peg of 3.75 SAR). The prose is written by Claude and contains no figures of its own._

---

<div dir="rtl">

# تقرير الحادثة

| | |
|---|---|
| بدء التحقيق | <span dir="ltr">2026-10-30 19:29:16 (UTC+3)</span> |
| النتيجة | تمت الاستعادة (وتم التحقق منها عبر مؤشرات الأعمال) |
| المدة | 32.1 ثانية |
| خطوات الوكيل | 5 خطوات |
| درجة الثقة | 0.95 |

## ما الذي حدث

بعد نشر الإصدار <span dir="ltr">v1.6</span> أصبحت الطلبات تُسجَّل كمعالَجة دون أن يتم دفعها. أطلق المراقب تنبيهًا عند 12 طلبًا غير مدفوعة، وبدأ نبض التحقيق في <span dir="ltr">2026-10-30 19:29:16 (UTC+3)</span>. بلغ عدد الطلبات غير المدفوعة ذروته 28 طلبًا بقيمة 19,968.01 ريال.

## السبب الجذري

قامت نسخة <span dir="ltr">v1.6</span> من عميل المدفوعات بترقية مكتبة HTTP، ولم تعد النسخة الجديدة قادرة على تحويل مبالغ Decimal إلى JSON. لذلك فشل كل استدعاء لمزوّد الدفع، ومع ذلك سُجّلت الطلبات كمعالَجة.

## الأدلة

- نظام المراقبة: طلبات عولجت دون دفع (<span dir="ltr">`payment_gap`</span>): <span dir="ltr">`12 of 26 orders were processed but have no payment.`</span>
- إعدادات تغيّرت أثناء المعالجة: <span dir="ltr">`version`: `v1.6` → `v1.3`</span>
- السجلات: <span dir="ltr">`21x [DEBUG] payment: provider call failed: Object of type Decimal is not JSON serializable`</span>

تُظهر سجلات الدفع تكرار الرسالة "Object of type Decimal is not JSON serializable"، ويُظهر سجل التغييرات أن ترقية عميل <span dir="ltr">v1.6</span> هي آخر تغيير. وأكد فحص فجوة الدفع وجود طلبات معالَجة دون دفع.

## الإجراءات المتخذة

- <span dir="ltr">`rollback_deploy {}`</span> (نُفِّذ)
- <span dir="ltr">`reprocess_orders {"order_ids": "all_unpaid"}`</span> (نُفِّذ)

## الأثر على الأعمال

| | طلبات بلا دفع | طلبات في الانتظار | مبالغ زائدة في دفتر الأستاذ |
|---|---|---|---|
| عند التنبيه | 12 طلبًا (9,499.00 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |
| الذروة (قبل أول إصلاح مباشرة) | 28 طلبًا (19,968.01 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |
| نهاية المعالجة | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |

| | |
|---|---|
| المسترد: طلبات دُفعت بإعادة المعالجة | 35 طلبًا (25,603.33 ريال) |
| المسترد: تصحيح المبالغ الزائدة في دفتر الأستاذ | 0 قيد (0.00 ريال) |

عند الذروة كانت 28 طلبًا غير مدفوعة بقيمة 19,968.01 ريال. وبعد التراجع إلى <span dir="ltr">v1.3</span> أُعيدت معالجة 35 طلبًا ودُفعت بقيمة 25,603.33 ريال، ولم يتبقَّ شيء غير مدفوع (0 طلب).

## إجراءات المتابعة

- إصلاح تحويل Decimal في عميل المدفوعات <span dir="ltr">v1.6</span> وإضافة اختبار يغطيه قبل إعادة النشر.
- إضافة تنبيه عند فشل استدعاءات مزوّد الدفع، فهي تُسجَّل حاليًا بمستوى DEBUG فقط.
- عدم تسجيل الطلب كمعالَج ما لم ينجح استدعاء الدفع.

---

_جميع الأرقام مُدرجة آليًا من سجل التشغيل (تحويل الدولار بسعر الربط 3.75 ريال). النص من صياغة Claude ولا يحتوي على أي أرقام من عنده._

</div>
