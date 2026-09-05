# ORION — CONTINUITY & RESUME LEDGER

**الإصدار:** 1.0-draft  
**الحالة:** DRAFT — قبل تثبيت الوثيقة داخل المستودع الجديد  
**المشروع:** ORION / ORION_NEXT  
**تاريخ الإنشاء:** 2026-09-05  

---

## 0. الغرض والسلطة

هذه الوثيقة هي **سجل الاستمرارية والاستئناف** للمشروع.
وظيفتها منع فقدان السياق بين جلسات التطوير، الحسابات/المستودعات، فترات التوقف، ونتائج CI.

هي لا تستبدل وثائق ORION المالكة للحالة أو المعمارية أو القرارات أو الـRoadmap.
بل تجمع **فهرسًا تشغيليًا قابلًا للتتبع** يجيب عن:

- أين وصل المشروع؟
- ما الذي تم إثباته فعليًا؟
- ما الذي لم يتم إثباته؟
- ما آخر حالة سليمة؟
- ما الـcommit/branch المرجعي لكل بوابة؟
- ما الـblocker الحالي؟
- ما الذي يُمنع تغييره؟
- من أين نستأنف؟
- ما الخطوة التالية المسموح بها؟

**قاعدة:** لا تُحوّل نتيجة غير قابلة للإثبات إلى `PASS` أو `APPROVED`.
الحالات المسموح بها تشمل: `PASS`, `VERIFIED`, `FAIL`, `ERROR`, `BLOCKED`, `CANCELLED`, `NOT RUN`, `NOT VERIFIED`, `PROPOSED`.

---

## 1. مصادر السلطة الحالية

المصدر الأعلى لقواعد تشغيل GPT هو `ORION_GPT_EXECUTION_RULES.md`.
والـ`CONTROL_INDEX` يحدد ملكية الوثائق.
أما `ORION_PROJECT_STATE.md` فهو المالك للحالة اللحظية، و`ORION_ROADMAP.md` لترتيب الانتقال، و`ORION_DECISIONS.md` للقرارات، و`ORION_CHANGELOG.md` للتاريخ التنفيذي، و`ORION_KNOWN_PROBLEMS.md` للمشاكل المؤكدة.

هذه الوثيقة **ليست مالكًا بديلًا** لهذه المعلومات؛ هي سجل استمرارية يربطها ببعضها.

---

## 2. حالة المستودع والترحيل

### المستودع الأصلي

`badeemorse-gif/ORION_NEXT`

### المستودع الجديد المستهدف

`badeemorse08-create/ORION_NEXT`

### حالة الترحيل

`COMPLETED — GIT HISTORY / BRANCHES`

تم نقل Git mirror إلى المستودع الجديد. التحقق المحلي أثبت وجود **225 فرعًا**، والتحقق عن بعد أثبت وجود `main` وجميع فروع المشروع المنقولة.

### ملاحظة مهمة عن Pull Requests

مراجع GitHub الداخلية من النوع `refs/pull/*` لا تُكتب عبر `git push --mirror`، ولذلك لم تُرحّل كـPR objects. هذا لا يعني فقدان Git commits أو الفروع.

### ملاحظة مهمة عن CI

GitHub Actions workflow runs وjob logs وartifacts وPR review history ليست جزءًا من Git history، ولذلك لا تُعتبر منقولة تلقائيًا مع الـmirror.

---

## 3. Git Canonical Anchors

### 3.1 Current main in migrated repository

`main` في المستودع الجديد يشير إلى:

`eeafd8c257825a8d24ea8158d57688663b7301ed`

رسالة الـcommit:

`docs: define paper execution timing telemetry boundary`

هذا هو **main canonical source state الذي نُقل من المستودع الأصلي** وقت الترحيل، وليس بالضرورة أحدث حالة موجودة على فروع التطوير اللاحقة.

### 3.2 D2 explosive-mover resume branch

الفرع:

`d2/explosive-mover-challenge-20260905`

آخر رأس منقول:

`4df818467ad91d28be92112e73c1813bea8aa0b4`

رسالة الـcommit:

`D2: add GitHub Actions infrastructure probe`

### 3.3 Canonical production baseline referenced by the development history

`c6126e0b94781608147f321f737742f87f48bf2f`

وهو commit مهم لسلامة Event Identity ومسار التحقق السابق. لا يُعامل وحده كـ`main` الحالي.

---

## 4. الحالة الرسمية المعلنة داخل PROJECT STATE

`PHASE 2 — CORE INTELLIGENCE COMPLETION`

الحالة المعلنة في `ORION_PROJECT_STATE.md`:

`IN PROGRESS`

ويظل `PROJECT STATE` هو المرجع الرسمي للحالة الحالية وفق نظام الحوكمة.

المسار التنفيذي المثبت:

```text
Provider
↓
MarketDataset
↓
VALIDATION
↓
STORE
↓
INDICATORS
↓
ANALYSIS
↓
PROFILE
↓
SCORE
↓
DECISION
↓
ExecutionPlan
↓
Execution
↓
Report
```

العقود التي توصف بأنها مستقرة/Canonical في PROJECT STATE تشمل `AnalysisResult`, `ProfileResult`, `ScoreResult`, `DecisionResult`, `ExecutionPlan`, `ExecutionResult`, و`ReportResult`.

---

## 5. آخر حالة Verification المعروفة من الوثائق الأساسية

الـ`PROJECT STATE` الحالي يوثق:

- `108 tests — OK`
- `VERIFICATION PASSED`
- `Python syntax compilation — PASSED`

كما يذكر تنظيف التحذيرات المستهدفة المرتبطة بدورة `asyncio` و`day-frequency` في `pandas-ta`.

**تنبيه استمرارية:** هذه هي النتيجة المعلنة داخل الوثيقة الحالية، بينما توجد نتائج تحقق لاحقة في تاريخ التطوير محفوظة في Git/CI context. لا يجوز استبدال سجل 108 tests بهذه النتائج أو العكس دون تحديث المالك الرسمي للحالة.

---

## 6. نقاط الاختبار والبوابات المهمة

### 6.1 Phase 1

`COMPLETED / VERIFIED` وفق PROJECT STATE.

تم تثبيت الأساس التعاقدي والمعماري، مع إغلاق Findings المرحلية المذكورة في STATE.

### 6.2 Event Identity blocker / correction

Commit:

`c6126e0b94781608147f321f737742f87f48bf2f`

الدلالة:

- إزالة workflow تشخيصي مؤقت من D2.
- هذا الـcommit يقع ضمن خط العمل الذي انتهى إلى تثبيت Event Identity contract.

**الحالة التشغيلية المسجلة في سجل المشروع:** تصحيح Event Identity أصبح جزءًا من baseline التطوير اللاحق، ولا يجوز إعادة فتحه دون سبب معماري مثبت.

### 6.3 D6 execution/capital line

Commit مرجعي تاريخي:

`2d3fc74dae0e7295b16e47b9e0ef1d85e46e1d22`

يشير تاريخ المشروع إلى تثبيت خط D6 النهائي المرتبط بإدارة رأس المال وتنفيذ الورق، مع اختبارات دورة رأس المال، journal، allocation، recovery، duplicate protection وغيرها.

### 6.4 D2 Campaign A — Real Historical Replay 7D

النطاق التاريخي:

`2026-08-18 → 2026-08-24`

النتيجة المسجلة:

- `40,320` processed events
- `0` orders
- `0` fills
- replay الأساسي اكتمل
- determinism gate اجتاز في التشغيل المؤهل

Dataset hash:

`9ed783f437152af77c0ac3e6694b6b1b47e3258c2b24f3dfcf0a81027dcd89e7`

Fixed universe:

20 symbols؛ لذلك لا تعتبر الحملة دليلًا على complete broad-market capture.

Artifact:

`d2-real-historical-replay-7d`

Digest:

`sha256:9b87cdf4ebdeafd83bea530dd3d700f03e6d069a63d731f340cbbd9eb4086cba`

Artifact ID:

`9926365613`

**Verdict:** `TECHNICALLY QUALIFIED WITHIN FIXED-20 SCOPE; NOT BROAD-MARKET QUALIFICATION`.

### 6.5 D2 explosive-mover challenge

النطاق:

22 symbols = fixed 20 + `TRUMPUSDT` + `STXUSDT`.

الهدف:

قياس قدرة النظام على discovery / decision / entry trace للأحجام الكبيرة عبر:

`+10%, +20%, +30%, +50%, +70%, +100%, +150%, +200%`

وعلى نوافذ:

`5m, 15m, 30m, 1h, 4h, 24h`

مع تتبع causal:

```text
movement
→ first observable
→ discovery
→ classification
→ score
→ entry state
→ decision
→ order
→ fill
→ position
→ exit
```

مع `no-entry reason` عند عدم الدخول.

### 6.6 D2 challenge evidence-layer defect

Commit:

`3b6f670de66eecb23be0960d9d9fa5b08dfad667`

النتيجة:

- إصلاح null-safety في evidence builder.
- replay اللاحق أكمل `44,352` processed events/decision cycles مع `0` orders و`0` fills.
- validation لم يكتمل؛ لذلك لم يتم إصدار checksum/artifact النهائي لتلك المحاولة.

### 6.7 D2 diagnostic commit

Commit:

`c9e6567e2e13545839f3e8e4ca026d85bf6c146a`

الغرض:

تحويل validation إلى diagnostic output لطباعة expected/actual/mismatches.

### 6.8 D2 Actions infrastructure probe

Commit:

`4df818467ad91d28be92112e73c1813bea8aa0b4`

الملف:

`.github/workflows/d2-actions-infra-probe.yml`

الـworkflow يستخدم:

`ubuntu-24.04`

ويحاول تنفيذ shell step، filesystem probe، checkout، ثم verify للـSHA.

**الحالة:** هذه هي آخر نقطة استئناف D2 في الفرع المرحّل.

---

## 7. Blockers المفتوحة

### BLOCKER-D2-ACTIONS-INIT

الوصف:

تعذر إكمال تشغيل D2 بسبب فشل تهيئة hosted runner / job initialization في بيئة GitHub Actions.

الـinfrastructure probe نفسها لم تصل إلى تنفيذ الـsteps في المحاولة المتأثرة.

### قيود القرار

لا يجوز بسبب هذا الـblocker:

- تخفيض Fast Recall standards.
- إدخال movers يدويًا إلى candidate set كحل لإجبار النجاح.
- تغيير thresholds بهدف تجاوز الفشل.
- اعتبار D2 qualified.
- بدء Campaign B.
- إعلان Production Merge.

### Resume Gate

قبل أي D2 replay كامل:

```text
FRESH MINIMAL ACTIONS PROBE
↓
real runner identity
runner_name != empty
steps > 0
first shell step executes
probe PASS
```

بعد اجتياز البوابة:

**تشغيل واحد فقط** للحملة D2 ذات 22 رمزًا، ثم تثبيت جميع الأدلة والـSHA256SUMS والـartifact قبل الحكم.

---

## 8. حالـة Complete Universe / Broad-Market Qualification

لا يوجد artifact موثوق يثبت complete point-in-time Binance Spot USDT universe لفترة Campaign A المطلوبة.

استخدام `exchangeInfo` الحالي للحصول على universe تاريخي سيكون future leakage وغير مقبول.

لذلك:

`universe_completeness = NOT_ESTABLISHED`

ولا يجوز اعتبار Campaign A أو D2 challenge الحالية اختبارًا شاملًا للسوق كله.

المطلوب مستقبلًا:

- `effective_timestamp_utc`
- `symbol`
- `baseAsset`
- `quoteAsset`
- `status`
- `spot_available / permission`
- complete point-in-time Spot universe
- immutable provenance
- hash

---

## 9. Performance evidence المهمة

تم تنفيذ profiling تاريخي لمدة 24 ساعة simulated:

- `5,760` events/cycles
- حوالي `849.98s`
- حوالي `6.7766 events/sec`
- حوالي `101.65x` speedup
- Peak RSS حوالي `2410.75 MB`
- zero observed memory growth
- cumulative scoring/evaluation كان المكوّن الأكبر في الزمن

الاستنتاج التشغيلي المسجل:

الـ60-minute timeout غير كافٍ لتشغيل Campaign A مع replay/determinism/downstream ضمن job واحد؛ تم اعتماد 240 دقيقة كتصحيح orchestration-only.

---

## 10. Paper-run evidence

ضمن التاريخ التشغيلي السابق:

### Real 1H paper run

- `9,942 events`
- start/end equity: `200 → 200`
- orders/fills: `0 / 0`
- max DD: `0`
- health: `true`
- paper-only: `true`
- reconnect: `6`
- replay/recovery/capital equality: `true`

### Real 8H paper run

- `61,453 events`
- start equity: `200`
- end equity: `200.11733072598386`
- `2 orders`
- `1 fill`
- open position at end: `DASHUSDT`
- reconnect: `60`
- max DD: `0.05866536`
- duplicate: `0`
- runtime failure: `null`
- health: `true`
- paper-only: `true`
- replay/recovery/capital equality: `true`

**Caveat:** لم يكن هذا run اعتمادًا نهائيًا بسبب ambiguity الخاصة بسياسة `CLOSE_AT_END`/end-of-run position handling.

---

## 11. Large-move / anti-missed-opportunity requirement

المشروع لديه خط حوكمة لاحق مخصص لـOpportunity Response وanti-missed-opportunity gates.

الـGit history يحتوي commits حديثة صريحة لهذه الحدود، منها:

- `11b8ea8373cf4eeb26417cc1de1ebcaa921bf199` — add opportunity response latency and anti-missed-opportunity gates
- `990602ddf1e86902e543aacbda4a4df5d840215d` — formalize opportunity response and position-management boundaries
- `aa825fb2e5944a9371cdccbc6d1a062a13b12b4c` — add opportunity-response and position-management acceptance gates
- `47bfa675f104b446ced084c9b8285c747df89472` — record opportunity response and position lifecycle decision
- `eeafd8c257825a8d24ea8158d57688663b7301ed` — define paper execution timing telemetry boundary

هذه الـcommits مهمة لأنها تعني أن شرط **عدم ترك الفرصة الكبيرة تمر بلا قياس** ليس مجرد طلب شفهي؛ هناك بالفعل خط توثيق/حوكمة في Git لهذا المفهوم.

لكن وجود هذه الحدود في Git **لا يساوي** إثبات أن النظام يحقق نسبة ربح يومية ثابتة 20% أو 30%، ولا يثبت التقاطًا كاملًا لحركة +150% أو +200%.

---

## 12. ما تم إثباته وما لم يتم إثباته

### مثبت

- البنية متعددة الطبقات وحدود الـpipeline الأساسية.
- Phase 1 completion وفق PROJECT STATE.
- canonical contracts الرئيسية المذكورة في STATE.
- وجود execution safety / capital lifecycle / paper controls في خط D6.
- Event identity correction ضمن التاريخ اللاحق.
- Real 7D historical replay مؤهل تقنيًا ضمن fixed-20 universe.
- deterministic replay gate للحملة المؤهلة.
- وجود targeted D2 challenge وتعريف evidence trace.
- وجود anti-missed-opportunity / opportunity response governance في Git.

### غير مثبت

- complete point-in-time Binance universe.
- broad-market qualification.
- capture of every explosive mover.
- guarantee of `20%` أو `30%` daily profit.
- guarantee of capturing `100%` of any `150%–200%` move.
- Production live-trading approval.
- Campaign B start.
- D2 explosive-mover final qualification.

---

## 13. Last Known Good State

لأغراض الاستئناف البرمجي:

**آخر حالة Git canonical مستقرة معروفة:** تعتمد على `main` وPROJECT STATE الحاليين، مع اعتبار نتائج D2 اللاحقة فروع تحقيق/تجربة غير مدمجة إلى main.

**آخر D2 investigation point:**

`d2/explosive-mover-challenge-20260905 @ 4df818467ad91d28be92112e73c1813bea8aa0b4`

ولا يجوز تخطي هذه النقطة والذهاب مباشرة إلى Campaign B.

---

## 14. Exact Resume Point

عند استعادة العمل:

```text
1. Verify migrated repository access.
2. Run fresh minimal GitHub Actions infrastructure probe.
3. Require actual runner execution.
4. If probe PASS:
   run exactly one 22-symbol D2 explosive-mover challenge.
5. Validate evidence schema.
6. Produce SHA256SUMS.
7. Upload final artifact.
8. Review broad-move traces and no-entry reasons.
9. Update PROJECT STATE / CHANGELOG / DECISIONS as required.
10. Only then decide whether D2 is qualified.
```

### Forbidden before D2 qualification

```text
Campaign B
Production Merge
Live orders / real credentials
Weakening discovery gates
Manual injection of movers
Changing thresholds solely to make the report pass
```

---

## 15. Migration reconciliation rules

بعد الترحيل إلى `badeemorse08-create/ORION_NEXT`:

1. Git history والفروع هي المرجع المنقول.
2. PR objects وreviews لا تعتبر منقولة.
3. Actions runs/logs/artifacts لا تعتبر منقولة.
4. أي evidence خارجي يجب أن يحصل على reference مستقل إذا أريد الحفاظ عليه كدليل دائم.
5. لا يُعاد كتابة التاريخ القديم لإخفاء حالات الفشل أو الـblockers.
6. أي run جديد على الحساب الجديد يجب أن يحمل commit SHA واضحًا.
7. عند أول تشغيل ناجح على الحساب الجديد، تُسجل هوية المستودع الجديد والـworkflow run والـartifact داخل هذا الـLedger وفي الوثائق المالكة عند الحاجة.

---

## 16. قاعدة تحديث هذا السجل

عند كل Gate مهم، تضاف entry جديدة تتضمن على الأقل:

```text
Date/Time UTC
Phase
Campaign
Commit SHA
Branch
Workflow Run ID
Job ID
Status
Evidence Artifact
Digest / SHA256
Observed Result
Blocker / Caveat
Decision
Next Authorized Action
```

لا يتم حذف entries التاريخية.

تصحيح البيانات يكون عبر **Correction Entry** جديدة مع الإشارة إلى entry السابقة.

---

## 17. الوضع الحالي عند إنشاء هذا السجل

**Repository migration:** `VERIFIED`  
**Git branches transferred:** `225`  
**Main present in destination:** `YES`  
**D2 current resume branch:** `d2/explosive-mover-challenge-20260905`  
**D2 current head:** `4df818467ad91d28be92112e73c1813bea8aa0b4`  
**D2 status:** `BLOCKED — ACTIONS HOSTED RUNNER / JOB INITIALIZATION`  
**Campaign A:** `TECHNICALLY QUALIFIED WITHIN FIXED-20 SCOPE`  
**Broad-market qualification:** `NOT ESTABLISHED`  
**Campaign B:** `BLOCKED`  
**Production approval:** `NOT APPROVED`

---

## 18. الوثائق التي يجب أن تبقى مالكة للمعلومات

- `ORION_GPT_EXECUTION_RULES.md`
- `ORION_CONTROL_INDEX.md`
- `ORION_PROJECT_STATE.md`
- `ORION_WORK_PROTOCOL.md`
- `ORION_PROJECT_CHARTER.md`
- `ORION_ARCHITECTURE.md`
- `ORION_ROADMAP.md`
- `ORION_FUTURE_ROADMAP.md`
- `ORION_ARCHITECTURE_FINDINGS.md`
- `ORION_DECISIONS.md`
- `ORION_CHANGELOG.md`
- `ORION_KNOWN_PROBLEMS.md`
- `ORION_RESTORE_ALL_BRANCH_SYNC.md`

هذا السجل يربط بينها ولا يلغي ملكيتها.

---

## 19. اعتماد الوثيقة

**هذه النسخة:** `DRAFT`  
**لا تعتبر Canonical داخل ORION قبل إدخالها إلى المستودع الجديد وتحديث المراجع المالكة عند الحاجة.**

عند تثبيتها رسميًا، يجب أن تصبح نقطة الاستمرارية التي يبدأ منها أي مراجعة شاملة بعد انقطاع أو نقل مستودع أو تغيير حساب.

---

**END OF ORION CONTINUITY & RESUME LEDGER**
