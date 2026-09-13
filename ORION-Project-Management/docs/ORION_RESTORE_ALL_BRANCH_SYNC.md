# ORION Restore — ALL Branch Synchronization Contract

> **ARCHIVED / HISTORICAL RECORD — NOT AN ACTIVE OPERATIONAL PROCEDURE**
>
> This document records the former Restore/MAIN/ALL synchronization architecture. The legacy synchronization and restore tools described below have been retired. Current development uses GitHub as the source of truth and the current repository/integration verification workflow. Do not use the commands, paths, or tool list in this historical document as an active operating procedure.

الإصدار: 2.3
الحالة: ARCHIVED — HISTORICAL RECORD

## الهدف

أداة ORION Restore تحتوي الآن على مسارين مستقلين:

- `MAIN` = نسخة مطابقة رسميًا لـ `origin/main` داخل المشروع الرئيسي.
- `ALL` = نسخ معزولة لجميع الفروع داخل `ORION_NEXT_ALL_BRANCHES`.

لا يوجد أي تداخل بين المسارين.

## MAIN — exact mirror

`MAIN` هو المسار الوحيد المسموح له بالكتابة داخل:

```text
C:\Users\badee\Desktop\ORION_NEXT
```

المصدر:

```text
GitHub / origin/main
```

النتيجة المطلوبة هي **Exact Mirror**:

> كل مسار موجود في `origin/main` يجب أن يكون موجودًا محليًا بالمحتوى نفسه، وكل مسار غير موجود في `origin/main` يجب ألا يبقى محليًا، باستثناء `.git` لأنه بيانات Git الداخلية الخاصة بالمستودع.

يستخدم MAIN:

1. `git fetch --prune origin main` دون checkout أو تغيير الفرع الحالي.
2. `git archive origin/main` لبناء snapshot رسمي مستقل عن working tree.
3. manifest محليًا مع استثناء `.git` فقط.
4. SHA-256 لمحتوى الملفات.
5. نقل الملفات الجديدة وتحديث المتغيرة فقط.
6. حذف كل المسارات الزائدة محليًا.
7. معالجة collisions بين file/directory/symlink.
8. verification كامل بعد materialization.

لا يظهر `MAIN SUCCESS` إلا إذا كان:

```text
LOCAL_MANIFEST (excluding .git)
        ==
ORIGIN_MAIN_ARCHIVE_MANIFEST
```

أي اختلاف يمنع النجاح.

## Independent parity verification

أضيفت أداة مستقلة للغرض الوحيد التالي:

```text
tools/orion_main_sync_verify.py
```

هذه الأداة **Read-Only** ولا تعدّل المشروع. تقوم بـ:

1. `git fetch --prune origin main`.
2. تحديد commit الهدف من `origin/main`.
3. بناء archive snapshot من `git archive origin/main`.
4. حساب manifest محلي مستقل مع استثناء `.git` فقط.
5. مقارنة كل المسارات والبصمات SHA-256.
6. إظهار `RESULT: EXACT MATCH` فقط عند التطابق الكامل.
7. إظهار `FAILED` مع المسارات الناقصة أو الزائدة أو المختلفة عند وجود أي خلل.

وبذلك أصبح لدينا مستويان مستقلان من الحماية:

```text
Sync MAIN
    ↓
Materialize
    ↓
Internal exact verification
    ↓
MAIN SUCCESS

ثم عند الحاجة للتحقق المستقل:

orion_main_sync_verify.py
    ↓
Fresh fetch
    ↓
Fresh archive
    ↓
Fresh local manifest
    ↓
EXACT MATCH / FAILED
```

هذا يمنع اعتمادنا على عداد الملفات أو commit hash وحدهما، ويجعل السؤال "هل النقل تم طبق الأصل؟" قابلًا للإجابة آليًا ببصمة محتوى كاملة.

## ALL — التصميم المعتمد

تم تثبيت تصميم ALL الحالي بعد نجاح اختبارات المزامنة.

التصميم **لا يستخدم `git worktree` نهائيًا**.

بدلًا من ذلك:

1. `git fetch --prune origin +refs/heads/*:refs/remotes/origin/*` يتم مرة واحدة لجلب جميع الفروع.
2. لكل فرع يتم استخدام `git archive origin/<branch>` لاستخراج snapshot كامل للفرع.
3. يتم وضع المحتوى مباشرة داخل مجلد مستقل للفرع تحت:

```text
C:\Users\badee\Desktop\ORION_NEXT_ALL_BRANCHES\
    main\
    future__opportunity-evaluation-contract__...
    phase2__core-intelligence-hardening__...
    ops__...
```

4. لا يتم تبديل `PROJECT_ROOT` إلى أي فرع أثناء ALL.
5. لا يتم تنفيذ `reset --hard` أو `clean -fdx` أو `worktree add` داخل المشروع الرئيسي.

## ALL — المزامنة الفعلية

لكل فرع يتم:

- قراءة snapshot الفعلي من GitHub عبر `git archive`.
- مقارنة المحتوى الموجود محليًا مع snapshot الهدف باستخدام SHA-256.
- نقل الملفات الجديدة فعليًا.
- تحديث الملفات التي تغير محتواها فعليًا.
- حذف الملفات التي لم تعد موجودة في الفرع الهدف.
- الإبقاء على الملفات المطابقة دون إعادة كتابتها لتقليل الزمن والـI/O.

وبالتالي فإن كل مجلد فرع يصبح **Exact Mirror** لمحتوى GitHub لذلك الفرع.

## ALL — الحماية والعزل

كل فرع له مجلد مستقل.

لا يجوز أن يكتب فرع في مجلد فرع آخر، ولا أن يكتب ALL فوق:

```text
C:\Users\badee\Desktop\ORION_NEXT
```

## MAIN وALL — الفصل النهائي

```text
MAIN:
GitHub/main → PROJECT_ROOT

ALL:
GitHub/all branches → ALL_ROOT/<branch>
```

القواعد:

- MAIN لا يستخدم `ALL_ROOT`.
- ALL لا يستخدم `PROJECT_ROOT` كوجهة.
- MAIN لا يغير أو يعيد تصميم محرك ALL.
- ALL لا يكتب فوق المشروع الرئيسي.
- لا يوجد `git worktree` في أي من المسارين.

## إحصائيات الشاشة

واجهة الأداة تعرض:

- `BRANCHES`
- `FILES`
- `ADDED`
- `UPDATED`
- `REMOVED`

في MAIN:

```text
BRANCHES = 1
FILES    = عدد ملفات origin/main
ADDED    = الملفات التي أضيفت محليًا
UPDATED  = الملفات التي اختلف محتواها وتم تحديثها
REMOVED  = الملفات الزائدة محليًا التي أزيلت لتحقيق التطابق
```

## الأدوات

التصميم النهائي للأداة:

```text
tools/
    orion_restore_gui.pyw       # محرك ALL المجمد
    orion_restore_main_gui.pyw  # واجهة MAIN + ALL
    orion_main_sync_verify.py   # مستقل: exact parity verification
    orion_restore_gui.vbs       # Windows launcher
```

## النتيجة المعتمدة

`MAIN` =

**Fetch origin/main → Archive snapshot → Compare SHA-256 → Add new files → Update changed files → Remove all extra local paths → Verify exact manifest → SUCCESS**

والتحقق المستقل =

**Fresh fetch → Fresh archive snapshot → Fresh local manifest → EXACT MATCH / FAILED**

و`ALL` =

**Discover all branches → Batch Fetch → Archive each branch → Compare SHA-256 → Transfer only required files → Remove obsolete files → Resolve Windows path-type collisions → Show per-branch statistics**.

يظل نجاح ALL baseline محميًا، ويظل MAIN هو المسار الوحيد الذي يجوز له تحديث `PROJECT_ROOT`.
