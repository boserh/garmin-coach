# EP-02 · Адаптивний план (замикання петлі)

**Тип:** епік · **Оцінка:** L · **Пріоритет:** стратегічне ядро продукту ·
**Залежності:** **EP-01** (compliance-дані — обов'язково)

## Сторя

> Як бігун, я хочу, щоб план сам пропонував корекцію наступних 1–2 тижнів, коли
> я пропускаю тренування, перевантажуюсь (ACWR) або погано відновлююсь (HRV,
> readiness) — щоб план залишався реалістичним без ручного `/plan <текст>`.

## Контекст

Це і є відмінність від Runna: план, що живе на даних відновлення. Уся механіка
застосування змін уже написана: `PlanEdit`-ops (add/move/modify/skip + risky/alt),
confirm-flow у боті (`pending_plan`, `plan_apply`/`plan_apply_alt`/`plan_cancel`),
`apply_plan_ops` повертає зачеплені сесії, `resync_workouts` доносить їх на
годинник. Епік — про **тригери і промпт**, не про механіку.

## Acceptance criteria

- [ ] **Тижневий перегляд** (неділя ввечері): Claude отримує наступні 7–14 днів плану,
      compliance минулого тижня (EP-01), recovery-тренд і ACWR; якщо корекція потрібна —
      у чат приходить пропозиція з поясненням «чому» і кнопками ✅ прийняти /
      🛡 safer-варіант / ❌ відхилити. Без підтвердження план НЕ змінюється.
- [ ] **Ранковий тригер**: readiness/HRV нижче порога І сьогодні tempo/intervals/long →
      разова пропозиція полегшити/перенести саме сьогоднішню сесію.
- [ ] «План ок» → жодного повідомлення (не спамити «все добре»).
- [ ] Прийнята корекція синкається на годинник лише для зачеплених сесій
      (`resync_workouts`), як існуючий чат-едіт.
- [ ] Per-user вимикач адаптації в `/settings`; глобальний дефолт у config.
- [ ] Кожен виклик логується (`ReportLog`, kind="adapt"); непідтверджені пропозиції
      протухають (нова пропозиція заміщає стару).
- [ ] Ранковий тригер спрацьовує не частіше 1 разу на день (guard у `bot_state`).

## План по файлах

- `app/analysis/prompts.py` — новий `SYSTEM_PLAN_ADAPT`: вхід — вікно плану,
  compliance, `fitness`-знімок (ACWR, recovery time, readiness, HRV vs baseline);
  вихід — **точно той самий JSON-формат, що `SYSTEM_PLAN_EDIT`** (`PlanEdit`:
  summary, operations, risky, alt_summary/alt_operations) + правило: «якщо
  корекція не потрібна — порожній operations». Правила: пропущений тиждень → не
  нарощувати обсяг; ACWR високий → deload; переносити, а не видаляти, де можливо.
- `app/analysis/service.py` — `run_plan_adaptation(session, user_id, api_key, ...)`
  за зразком `run_plan_edit`: зібрати контекст (repository: вікно плану через
  `list_workouts`, compliance з EP-01, `get_recent_extra`), Sonnet (`MODEL_PLAN` —
  задача механічна, як едіти), парсинг у `PlanEdit`, `ReportLog(kind="adapt")`.
- `bot/jobs.py` —
  - `plan_adapt_job` (тижневий, `JobQueue.run_daily` у неділю, `PLAN_ADAPT_HOUR`,
    Europe/Warsaw — патерн `plan_sync_job`): по користувачах з активним планом і
    увімкненою адаптацією → `run_plan_adaptation` → якщо є operations, покласти
    pending і надіслати повідомлення з кнопками;
  - ранковий тригер: у циклі morning_job після побудови payload — перевірка
    readiness-порога + типу сьогоднішньої сесії; guard `bot_state`
    (`adapt_suggested:<date>`).
- `bot/handlers.py` — переиспользовать `plan_callback`: у callback data додати
  джерело (`adapt` vs `edit`), pending-структура та сама; врахувати, що pending
  від джоба живе в `context.application`-скоупі, а не `user_data` конкретного
  апдейта — перевірити, як `context.user_data` доступний із job context
  (можливо, зберігати pending ops у `bot_state` за user_id замість user_data).
- `app/db/models.py` — `User.plan_adapt_enabled: bool = True` (**міграція**).
- `app/routers/settings.py` + `settings.html` — чекбокс адаптації.
- `app/core/config.py` — `PLAN_ADAPT_HOUR`, `PLAN_ADAPT_READINESS_MIN`,
  `PLAN_ADAPT_WEEKLY_DOW`.
- `tests/` — «план ок» → без пропозиції; пропуски → ops зменшують обсяг
  (mock Claude); ранковий guard раз/день; вимикач.

## Підводні камені

- **Pending ops із джоба**: існуючий флоу тримає їх у `context.user_data`, що
  прив'язано до чату; для джоба надійніше зберігати pending у `bot_state`
  (JSON) і читати в `plan_callback` — інакше рестарт бота губить пропозицію.
- Не давати моделі «переписати весь план» — обмежити вікно ops 14 днями у
  промпті І валідацією в `run_plan_adaptation` (відкинути ops поза вікном).
- Дедуп-кеш: контекст містить дату/compliance — ключ природно змінюється;
  переконатися, що повторний тік того ж дня — кеш-хіт, а не повторна оплата.
- UX-тон: пропозиція має пояснювати причину одним рядком («HRV третій день
  нижче базової смуги») — це те, за що користувач довірятиме системі.
