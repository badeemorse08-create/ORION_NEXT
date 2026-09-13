# ORION — Historical Paper Replay Contract

الحالة: PROPOSED — DESIGN GATE ONLY
Baseline SHA: `f7c7341e5f3cf13ed05cde35342122badb85185c`
Branch: `verification/historical-paper-replay-design`

==================================================
1. الغرض
==================================================

هذه الوثيقة تعرف نموذج **Historical Paper Replay** المقترح لـ ORION.

الهدف ليس بناء Backtest تقليدي يعطي الاستراتيجية كامل التاريخ ثم يحسب
النتيجة النهائية، وليس إعادة تصميم استراتيجية ORION لخدمة الاختبار.

الهدف هو تشغيل ORION كأنه موجود في السوق الحي، مع استبدال مصدر الزمن/السوق
الحي ببيانات تاريخية محملة مسبقًا، مع تقدم زمني تدريجي يمنع رؤية المستقبل.

النتيجة المطلوبة هي **تجربة تشغيلية تاريخية** تعيش فيها القرارات بالتتابع
الزمني نفسه الذي كان سيواجهه النظام لو كان ذلك الشهر يحدث فعليًا.

==================================================
2. المبدأ الأعلى
==================================================

**نُسرّع مرور الزمن، ولا نُسرّع المعرفة.**

يجب أن تكون البيانات التاريخية محملة ومفهرسة مسبقًا لتقليل زمن الاختبار،
لكن ORION لا يحصل في أي لحظة محاكاة إلا على المعلومات التي كانت ستكون متاحة
له عند `simulation_clock` الحالي.

ممنوع:

- look-ahead.
- استخدام أي حدث مستقبلي في قرار حالي.
- إعطاء المؤشرات بيانات خارج نافذة التاريخ المتاحة حتى اللحظة الحالية.
- حساب نتيجة الصفقة اعتمادًا على المستقبل ثم إرجاعها للبوت كأنها Fill حي.
- إعادة ترتيب الأحداث بما يغيّر السببية التشغيلية.

==================================================
3. الفرق عن Backtest التقليدي
==================================================

الـReplay ليس:

Historical Data
↓
Batch Indicators
↓
Batch Signals
↓
Final P&L

بل:

Historical Data Store
↓
Simulation Clock
↓
Progressive Event Release
↓
Canonical ORION Pipeline
↓
Decision
↓
Paper Order / Position Lifecycle
↓
Next Historical Event
↓
...حتى نهاية الفترة

التاريخ الكامل موجود في القرص، لكن المعرفة المتاحة للـORION محدودة بالزمن
المحاكى.

==================================================
4. العلاقة مع ORION Architecture
==================================================

Historical Replay هو **Adapter للبيانات والزمن**، وليس محرك أعمال بديلًا.

المبدأ:

Historical Event Source
↓
Market Event Adapter
↓
نفس عقود ORION القانونية
↓
نفس Discovery / Opportunity / Decision
↓
نفس Paper Execution / Ledger
↓
نفس Audit / Recovery / Reporting

لا يسمح بإعادة كتابة Business Logic داخل Replay Adapter.

ويجب أن يبقى المسار الحي والمسار التاريخي مستهلكين لنفس العقود التنفيذية قدر
الإمكان.

==================================================
5. Historical Data Preload
==================================================

قبل بدء المحاكاة يتم تحميل كل البيانات المطلوبة للفترة المستهدفة وتحققها.

يشمل ذلك — بحسب عقد المكونات المستهلكة:

- Market trades / ticker events عندما تكون مطلوبة.
- Candle updates / closes.
- Multi-timeframe candles المطلوبة للقرار.
- بيانات universe / exchange metadata اللازمة للتشغيل.
- أي auxiliary market context مطلوب قانونيًا.

يجب إنشاء manifest يثبت:

- الفترة التاريخية.
- الرموز.
- نوع البيانات.
- Timeframes.
- مصدر البيانات.
- حجم البيانات.
- ordering.
- integrity hashes.
- timezone normalization.

بعد بدء المحاكاة لا يسمح للـReplay بإعادة جلب بيانات مستقبلية من مصدر خارجي
لإكمال التاريخ؛ التاريخ الاختباري يجب أن يكون مصدرًا ثابتًا وقابلًا لإعادة
التشغيل.

==================================================
6. Simulation Clock
==================================================

الساعة الرسمية أثناء Replay هي:

`simulation_clock`

وليس ساعة الجهاز.

كل قرار أو صلاحية زمنية أو expiry أو lifecycle timestamp يجب أن يعتمد على
الساعة المحاكاة حيث ينطبق ذلك على العقد.

يجب الاحتفاظ بتوقيتين منفصلين عند الحاجة:

- `simulation_timestamp` — وقت الحدث داخل السوق التاريخي.
- `wall_clock_timestamp` — وقت تنفيذه فعليًا على الجهاز.

لا يجوز استخدام `wall_clock_timestamp` لإعطاء ORION معرفة مستقبلية.

==================================================
7. Progressive Event Release
==================================================

الأحداث تطلق بالترتيب الزمني السببي.

القاعدة الأساسية:

`event.timestamp <= simulation_clock`

وفي كل خطوة لا يصبح الحدث متاحًا قبل نقطة وصوله في الزمن المحاكى.

عند وجود عدة أحداث في نفس اللحظة يجب اعتماد ordering حتمي ثابت، ويجب تسجيله
في evidence. لا يجوز استخدام ترتيب غير حتمي ناتج عن thread scheduling أو file
iteration order.

==================================================
8. Causal Market State
==================================================

في كل لحظة، state السوق الظاهر لـORION يجب أن يكون ناتجًا فقط من الأحداث التي
تم إطلاقها حتى تلك اللحظة.

أي indicator أو feature أو score أو classification أو decision يجب أن يعتمد
على state تاريخي سببي، وليس على snapshot نهائي للفترة.

مثال:

عند `2026-08-12 14:32:05` لا يجوز أن يرى ORION سعرًا أو حجمًا أو candle أو
feature مشتقًا من `14:32:06` أو ما بعدها، حتى لو كانت كل البيانات موجودة مسبقًا.

==================================================
9. Order / Fill Semantics
==================================================

الأمر الورقي الذي ينشأ عند زمن محاكاة معين لا يحصل على Fill لمجرد أن البيانات
المستقبلية تثبت أنه كان يمكن أن يُملأ.

يجب أن يمر عبر Paper Execution lifecycle وفق الأحداث اللاحقة التي أصبحت
مرئية زمنيًا.

بالتالي يجب تسجيل:

- order creation simulation timestamp.
- order state transitions.
- market events التي سببت أو رفضت الـfill.
- fill simulation timestamp.
- price / quantity.
- capital reservation / release.
- position transition.

أي shortcut ينظر إلى المستقبل لتحديد Fill يعتبر خرقًا للـReplay Contract.

==================================================
10. Position Lifecycle عند نهاية الفترة
==================================================

يجب تعريف سلوك موحد وصريح عندما تصل المحاكاة إلى نهاية الفترة ويوجد مركز
مفتوح.

الخيارات المقبولة تصميميًا قبل التنفيذ:

1. `CLOSE_AT_END` — إغلاق ورقي قسري بسعر/قاعدة نهاية الفترة المحددة.
2. `MARK_AND_CARRY` — إنهاء الفترة مع مركز مفتوح واحتساب mark-to-market،
   مع عدم الادعاء بأن الربح تحقق فعليًا.
3. سياسة تعاقدية أخرى موثقة صراحة.

لا يجوز أن يختلف هذا السلوك حسب adapter أو المصادفة.

==================================================
11. Determinism
==================================================

نفس:

- source SHA.
- dataset manifest.
- replay configuration.
- event ordering.
- simulation semantics.

يجب أن ينتج نفس:

- event sequence.
- decisions.
- orders / fills.
- positions.
- ledger state.
- summary metrics.

وأي اختلاف بين تشغيلين متطابقين يجب أن ينتج finding واضحًا، وليس أن يمر كفرق
طبيعي.

==================================================
12. Time Acceleration
==================================================

التسريع مسموح فقط في **الزمن المنفذ**، وليس في التسلسل المنطقي للسوق.

مثال:

30 يومًا من السوق يمكن أن تنفذ خلال دقائق أو ساعات فعلية حسب كثافة البيانات،
لكن ORION لا يرى إلا الأحداث بالتدرج الزمني التاريخي.

يجب تسجيل:

- `simulated_duration`.
- `wall_duration`.
- `speedup_ratio`.

حتى تكون مدة الاختبار قابلة للمراجعة.

==================================================
13. Universe Semantics
==================================================

إذا كان التشغيل يستخدم dynamic universe، فيجب محاكاة معلومات الـuniverse
المتاحة عند كل نقطة زمنية.

لا يجوز استخدام universe النهائي للشهر كاملًا عند بداية المحاكاة إذا كان ذلك
يمنح البوت معرفة مستقبلية عن الرموز.

وأي universe simplification يجب أن يكون معلنًا في configuration/evidence.

==================================================
14. Recovery / Failure Simulation
==================================================

Replay الأساسي لا يفترض أن كل شيء مثالي.

يجب أن يدعم مستقبلًا سيناريوهات موثقة مثل:

- reconnect.
- event gap.
- delayed event.
- duplicated event.
- malformed event.
- provider interruption.

لكن لا يحق للـReplay اختراع أعطال غير مثبتة في dataset إلا في **Synthetic Stress
Mode** منفصل ومسمى بوضوح.

==================================================
15. Evidence
==================================================

كل Replay يجب أن ينتج evidence قابلًا للمراجعة، على الأقل:

- source SHA.
- dataset manifest/hash.
- configuration.
- start/end simulation timestamps.
- wall-clock start/end.
- event count.
- event ordering proof.
- decisions.
- orders.
- fills.
- closed/open positions.
- realized/unrealized P&L.
- fees/slippage.
- equity curve.
- max drawdown.
- runtime failures.
- recovery counts.
- determinism/replay equality.
- final disposition of open positions.

==================================================
16. Required Metrics
==================================================

الحد الأدنى:

- total events.
- total signals.
- accepted entries.
- rejected entries by reason.
- orders.
- fills.
- closed trades.
- open positions at end.
- realized P&L.
- unrealized P&L.
- ending equity.
- max drawdown.
- fees.
- slippage.
- runtime failures.
- recovery events.
- duplicate events.
- missed-opportunity audit.

==================================================
17. Missed Opportunity Audit
==================================================

بسبب متطلبات المشروع الخاصة بالحركات المفاجئة والفرص الانفجارية، يجب أن يسمح
Replay لاحقًا بتدقيق:

Market movement
↓
Discovery
↓
Classification
↓
Entry state
↓
Entry decision
↓
Order
↓
Fill
↓
Position management
↓
Exit

إذا لم يدخل ORION فرصة مهمة، يجب أن يكون سبب الرفض قابلًا للاستخراج من الـaudit
بدون إعادة تفسير النتائج بعد انتهاء المحاكاة.

==================================================
18. Campaign Levels
==================================================

بعد اكتمال التنفيذ والتحقق من صحة Replay، المستويات المستهدفة:

### Level 1 — 7D

صحة التسلسل + smoke + determinism.

### Level 2 — 30D

اختبار تشغيلي طويل المدى للتنوع السوقي.

### Level 3 — 90D

تنوع أكبر للأنظمة السوقية والتحمل.

### Level 4 — 365D

Long-horizon robustness.

نجاح مستوى لا يعني نجاح المستوى التالي.

==================================================
19. Safety Boundary
==================================================

Historical Replay:

- Paper only.
- No exchange orders.
- No live credentials.
- No live execution.
- No mutation of production trading state.
- No silent modification of canonical strategy behavior.

ويجب أن تكون أي artifact ناتجة عن Replay معزولة عن التشغيل الحي.

==================================================
20. Performance Boundary
==================================================

يحق لمحرك Replay استخدام preloading، indexing، batching داخلي أو memory-mapped
storage لتحسين الزمن، بشرط ألا يغير event semantics أو causal visibility.

الأداء يقاس منفصلًا عن صحة النتيجة.

لا يجوز قبول Replay أسرع لكنه يرى المستقبل.

==================================================
21. Reproducibility
==================================================

يجب أن يكون الـReplay قابلًا لإعادة التشغيل على نفس dataset/config/source SHA
وإنتاج نتائج متكافئة حتميًا.

عند تغير source أو dataset أو config، يجب أن يتغير replay identity والمخرجات
الموقعة/المهندسة الخاصة به.

==================================================
22. Definition of Done — Design Gate
==================================================

لا تعتبر هذه الوثيقة معتمدة للتنفيذ إلا بعد أن يثبت المراجع:

- [ ] أن الفرق بين Replay وBacktest التقليدي واضح ومقبول.
- [ ] No Future Knowledge invariant محدد بوضوح.
- [ ] Simulation Clock contract محدد.
- [ ] Event ordering contract محدد.
- [ ] Fill causality محددة.
- [ ] Dynamic-universe semantics محددة.
- [ ] End-of-run open-position policy محددة.
- [ ] Determinism requirements محددة.
- [ ] Evidence schema محددة.
- [ ] Paper-only boundary محفوظة.
- [ ] لا يتطلب التصميم تعديل production path الحالي دون مراجعة معمارية منفصلة.

==================================================
23. Definition of Done — Implementation Gate
==================================================

بعد اعتماد التصميم فقط:

- [ ] 7D replay ينجح.
- [ ] repeated 7D replay متطابق.
- [ ] 30D replay ينجح.
- [ ] repeated 30D replay متطابق.
- [ ] لا توجد future-data violations.
- [ ] event ordering deterministic.
- [ ] fill causality verified.
- [ ] recovery/reconnect semantics verified where applicable.
- [ ] full evidence artifact محفوظ.
- [ ] source SHA exact.
- [ ] working tree clean.
- [ ] لا توجد تغييرات خارج scope.

90D و365D يظلان حملة تحقق لاحقة ولا يتم اعتبارهما بديلًا عن صحة التنفيذ
الأساسية.

==================================================
24. Explicit Non-Scope
==================================================

هذه الوثيقة لا تنفذ:

- أي code.
- أي change في main.
- أي live trading.
- أي تعديل على Strategy/Decision semantics.
- أي تغيير في Paper Ledger semantics قبل قرار مستقل.
- أي GUI.
- أي credentials integration.

==================================================
25. Current Status
==================================================

`PROPOSED — DESIGN ONLY`

Branch created from exact baseline `f7c7341e5f3cf13ed05cde35342122badb85185c`.

No merge to `main`.
No production code changed by this design package.
