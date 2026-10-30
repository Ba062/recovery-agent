# Incident report

| | |
|---|---|
| Investigation started | 2026-10-30 18:46:25 (UTC+3) |
| Outcome | Escalated to a human |
| Duration | 11.8 s |
| Agent steps | 3 |
| Confidence | 0.70 |

## What happened

Nabd began investigating at 2026-10-30 18:46:25 (UTC+3) after the monitor raised an alert for failing requests to /orders. Requests to POST /orders were failing with server errors and the ledger sync was failing, while /health stayed green. Nabd took 3 steps and made 0 actions, then escalated to a human.

## Root cause

The database is rejecting writes with "attempt to write a readonly database", which breaks both order creation and the ledger sync. Why it became read-only is not known, and none of Nabd's tools can restore write access.

## Evidence

- Monitor: `high_error_rate`: `50% of recent /orders requests failed with 5xx.`
- Logs: `1x [INFO] pipeline: pipeline started`
- Logs: `1x [ERROR] sync: ledger sync failed: attempt to write a readonly database`

The logs show repeated read-only write errors on /orders and one from the ledger sync. The configuration is unchanged (v1.3) and its last change does not match when the errors began, so a config fault is unlikely. Business checks show accepted orders and payments still match. Confidence in this diagnosis is 0.70.

## Actions taken

None.

## Business impact

| | Unpaid orders | Queued orders | Ledger overstated |
|---|---|---|---|
| At alert | 0 (0.00 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |
| End of run | 0 (0.00 SAR) | 0 (0.00 SAR) | 0 (0.00 SAR) |

| | |
|---|---|
| Recovered: orders paid by reprocessing | 0 (0.00 SAR) |
| Recovered: ledger overstatement corrected | 0 (0.00 SAR) |

No accepted orders are unpaid, queued or missing from the ledger (0 orders unpaid, 0 ledger rows missing from the ledger). The impact is on new orders, which are rejected until write access is restored. Clients will need to retry them.

## Follow-ups

- Check the database file or volume for permissions, a read-only remount, a full disk or a storage fault, and restore write access.
- After the fix, re-run check_invariants and check_data_integrity, and confirm the ledger sync recovers.
- Add an alert for read-only database write errors so it fires before the error rate climbs.
- Ask clients to retry the orders that were rejected.

---

_All figures are inserted by code from the run record (USD converted at the peg of 3.75 SAR). The prose is written by Claude and contains no figures of its own._

---

<div dir="rtl">

# تقرير الحادثة

| | |
|---|---|
| بدء التحقيق | <span dir="ltr">2026-10-30 18:46:25 (UTC+3)</span> |
| النتيجة | تم التصعيد إلى مهندس بشري |
| المدة | 11.8 ثانية |
| خطوات الوكيل | 3 خطوات |
| درجة الثقة | 0.70 |

## ما الذي حدث

بدأ نبض التحقيق في <span dir="ltr">2026-10-30 18:46:25 (UTC+3)</span> بعد تنبيه من المراقب بشأن فشل الطلبات على /orders. كانت طلبات POST /orders تفشل بأخطاء خادم ومزامنة السجل المالي تفشل، بينما بقي /health سليمًا. نفّذ نبض 3 خطوات ولم يُجرِ أي تغيير (0 إجراء)، ثم صعّد الأمر إلى مختص.

## السبب الجذري

قاعدة البيانات ترفض عمليات الكتابة برسالة "attempt to write a readonly database"، مما يعطّل إنشاء الطلبات ومزامنة السجل المالي. سبب تحوّلها إلى وضع القراءة فقط غير معروف، ولا توجد لدى نبض أداة تعيد صلاحية الكتابة.

## الأدلة

- نظام المراقبة: معدل أخطاء مرتفع (<span dir="ltr">`high_error_rate`</span>): <span dir="ltr">`50% of recent /orders requests failed with 5xx.`</span>
- السجلات: <span dir="ltr">`1x [INFO] pipeline: pipeline started`</span>
- السجلات: <span dir="ltr">`1x [ERROR] sync: ledger sync failed: attempt to write a readonly database`</span>

تُظهر السجلات تكرار أخطاء الكتابة على قاعدة للقراءة فقط عند /orders، وخطأً واحدًا في مزامنة السجل المالي. الإعدادات لم تتغير (<span dir="ltr">v1.3</span>) وآخر تغيير فيها لا يتطابق مع وقت بدء الأخطاء، فخلل الإعدادات غير مرجح. وتُظهر الفحوص أن الطلبات المقبولة ومدفوعاتها لا تزال متطابقة. درجة الثقة في هذا التشخيص 0.70.

## الإجراءات المتخذة

لا يوجد.

## الأثر على الأعمال

| | طلبات بلا دفع | طلبات في الانتظار | مبالغ زائدة في دفتر الأستاذ |
|---|---|---|---|
| عند التنبيه | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |
| نهاية المعالجة | 0 طلب (0.00 ريال) | 0 طلب (0.00 ريال) | 0 قيد (0.00 ريال) |

| | |
|---|---|
| المسترد: طلبات دُفعت بإعادة المعالجة | 0 طلب (0.00 ريال) |
| المسترد: تصحيح المبالغ الزائدة في دفتر الأستاذ | 0 قيد (0.00 ريال) |

لا توجد طلبات مقبولة غير مدفوعة أو معلّقة أو مفقودة من السجل المالي (0 طلب غير مدفوعة، 0 قيد مفقودة من السجل). يقع الأثر على الطلبات الجديدة التي تُرفض إلى أن تُستعاد صلاحية الكتابة، وسيحتاج العملاء إلى إعادة إرسالها.

## إجراءات المتابعة

- فحص ملف قاعدة البيانات أو وحدة التخزين من حيث الصلاحيات، أو إعادة التركيب للقراءة فقط، أو امتلاء القرص، أو عطل التخزين، ثم استعادة صلاحية الكتابة.
- بعد الإصلاح، إعادة تشغيل check_invariants وcheck_data_integrity والتأكد من تعافي مزامنة السجل المالي.
- إضافة تنبيه لأخطاء الكتابة على قاعدة بيانات للقراءة فقط ليعمل قبل ارتفاع نسبة الأخطاء.
- طلب إعادة إرسال الطلبات المرفوضة من العملاء.

---

_جميع الأرقام مُدرجة آليًا من سجل التشغيل (تحويل الدولار بسعر الربط 3.75 ريال). النص من صياغة Claude ولا يحتوي على أي أرقام من عنده._

</div>
