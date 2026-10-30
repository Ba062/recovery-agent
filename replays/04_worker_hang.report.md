# Incident report

| | |
|---|---|
| Investigation started | 2026-10-30 18:44:25 (UTC+3) |
| Outcome | Recovered (verified against the business invariants) |
| Duration | 32.3 s |
| Agent steps | 4 |
| Confidence | 0.60 |

## What happened

After the monitor's alert, Nabd began investigating at 2026-10-30 18:44:25 (UTC+3). The order worker had stopped consuming the queue, so 25 orders worth 17,828.58 SAR were waiting at alert time. Nabd restarted the worker and the backlog drained.

## Root cause

The order worker thread got stuck in a wait state and stopped processing the queue. Its heartbeat stayed fresh, so the stall was not visible from health checks. The deeper reason it entered the wait state is not yet known.

## Evidence

- Monitor: `queue_backlog`: `Queue depth 25 and growing (was 12).`
- Logs: `1x [INFO] pipeline: pipeline started`
- Logs: `1x [DEBUG] worker: worker entered wait state`
- Logs: `1x [DEBUG] sync: ledger synced`

Queue depth grew while the processed count stayed flat, and the worker log shows it entering a wait state with no later activity. The configuration was unchanged (v1.3) and looked sane. After the restart the queue returned to empty.

## Actions taken

- `restart_worker {}` (applied)

## Business impact

| | Unpaid orders | Queued orders | Ledger overstated |
|---|---|---|---|
| At alert | 0 (0.00 SAR) | 25 (17,828.58 SAR) | 0 (0.00 SAR) |
| Peak (just before the first fix) | 0 (0.00 SAR) | 48 (32,006.30 SAR) | 0 (0.00 SAR) |
| End of run | 0 (0.00 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |

| | |
|---|---|
| Recovered: orders paid by reprocessing | 0 (0.00 SAR) |
| Recovered: ledger overstatement corrected | 0 (0.00 SAR) |

At peak, 48 orders worth 32,006.30 SAR were delayed in the queue. Nothing was left unpaid (0.00 SAR) and nothing reached the ledger without payment (0.00 SAR). At the end, the queue held 0.00 SAR.

## Follow-ups

- Investigate why the worker enters the wait state and does not wake up.
- Add an alert on a flat processed counter or a growing queue, since the heartbeat stayed fresh during the stall.
- Add a watchdog that restarts the worker automatically when it stops consuming the queue.

---

_All figures are inserted by code from the run record (USD converted at the peg of 3.75 SAR). The prose is written by Claude and contains no figures of its own._

---

<div dir="rtl">

# تقرير الحادثة

| | |
|---|---|
| بدء التحقيق | <span dir="ltr">2026-10-30 18:44:25 (UTC+3)</span> |
| النتيجة | تمت الاستعادة (وتم التحقق منها عبر مؤشرات الأعمال) |
| المدة | 32.3 ثانية |
| خطوات الوكيل | 4 خطوات |
| درجة الثقة | 0.60 |

## ما الذي حدث

بعد تنبيه المراقبة، بدأ نبض التحقيق في <span dir="ltr">2026-10-30 18:44:25 (UTC+3)</span>. كان عامل الطلبات قد توقف عن استهلاك الطابور، فكان هناك 25 طلبًا بقيمة 17,828.58 ريال في الانتظار وقت التنبيه. أعاد نبض تشغيل العامل فتم تصريف الطابور المتراكم.

## السبب الجذري

علق مسار عامل الطلبات في حالة انتظار وتوقف عن معالجة الطابور. بقي نبض الحياة (heartbeat) حديثًا، لذلك لم يظهر التوقف في فحوصات السلامة. والسبب الأعمق لدخوله حالة الانتظار غير معروف حتى الآن.

## الأدلة

- نظام المراقبة: تراكم في الطابور (<span dir="ltr">`queue_backlog`</span>): <span dir="ltr">`Queue depth 25 and growing (was 12).`</span>
- السجلات: <span dir="ltr">`1x [INFO] pipeline: pipeline started`</span>
- السجلات: <span dir="ltr">`1x [DEBUG] worker: worker entered wait state`</span>
- السجلات: <span dir="ltr">`1x [DEBUG] sync: ledger synced`</span>

ازداد عمق الطابور بينما بقي عدد الطلبات المعالجة ثابتًا، ويُظهر سجل العامل دخوله حالة انتظار دون نشاط لاحق. كانت الإعدادات دون تغيير (<span dir="ltr">v1.3</span>) وسليمة. وبعد إعادة التشغيل عاد الطابور فارغًا.

## الإجراءات المتخذة

- <span dir="ltr">`restart_worker {}`</span> (نُفِّذ)

## الأثر على الأعمال

| | طلبات بلا دفع | طلبات في الانتظار | مبالغ زائدة في دفتر الأستاذ |
|---|---|---|---|
| عند التنبيه | 0 طلب (0.00 ريال) | 25 طلبًا (17,828.58 ريال) | 0 قيد (0.00 ريال) |
| الذروة (قبل أول إصلاح مباشرة) | 0 طلب (0.00 ريال) | 48 طلبًا (32,006.30 ريال) | 0 قيد (0.00 ريال) |
| نهاية المعالجة | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |

| | |
|---|---|
| المسترد: طلبات دُفعت بإعادة المعالجة | 0 طلب (0.00 ريال) |
| المسترد: تصحيح المبالغ الزائدة في دفتر الأستاذ | 0 قيد (0.00 ريال) |

عند الذروة تأخر 48 طلبًا بقيمة 32,006.30 ريال في الطابور. لم يبق شيء غير مدفوع (0.00 ريال)، ولم يصل شيء إلى السجل دون دفع (0.00 ريال). وفي النهاية كان الطابور بقيمة 0.00 ريال.

## إجراءات المتابعة

- التحقيق في سبب دخول العامل حالة الانتظار وعدم استيقاظه منها.
- إضافة تنبيه عند ثبات عدّاد الطلبات المعالجة أو نمو الطابور، لأن نبض الحياة بقي حديثًا أثناء التوقف.
- إضافة مراقب (watchdog) يعيد تشغيل العامل تلقائيًا عند توقفه عن استهلاك الطابور.

---

_جميع الأرقام مُدرجة آليًا من سجل التشغيل (تحويل الدولار بسعر الربط 3.75 ريال). النص من صياغة Claude ولا يحتوي على أي أرقام من عنده._

</div>
