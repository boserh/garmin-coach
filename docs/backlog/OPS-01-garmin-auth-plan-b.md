# OPS-01 · Garmin auth: підготувати і перевірити «план Б» (python-garminconnect)

**Тип:** операційна стійкість · **Оцінка:** M–L · **Пріоритет:** P0 як
**підготовка**, не негайна міграція — станом на 2026-07-06 повний логін через
пінований garth 0.4.47 працює (ANALYSIS.md §1.5) · **Залежності:** зливається з
PERF-05 (rate limit живе в тому самому шарі клієнта); поглинає адаптацію ST-06
(MFA-міст)

## Проблема

garth офіційно deprecated з 2026-03-28 — Garmin увімкнув Cloudflare
TLS-fingerprinting (ANALYSIS.md §0). Збережені OAuth1-токени живуть ~1 рік від
видачі (user 1: виданий ≈ 2026-06-19 → оцінка смерті ≈ 2027-06). **Але**
перевірка 2026-07-06 показала: OAuth2-обмін через OAuth1 працює, і навіть повний
email+password логін через garth 0.4.47 проходить (~4с, без MFA) — пін зі старим
mobile-флоу поки проходить повз Cloudflare (deprecation найбільше вдарив по
0.8.x SSO-флоу). Реальний ризик — не таймер токена, а **раптове** закручування
Cloudflare. Тому формат тікета: план Б готовий «у шухляді» + моніторинг, а сама
міграція — за фактом поломки, без паніки зараз.

## Acceptance criteria

- [x] Розвідка (ANALYSIS.md §4.4.2): скрипт проганяє `python-garminconnect`
      (curl_cffi TLS-імперсонація) проти власного акаунта — логін, MFA, всі наші
      endpoint-и (`trainingreadiness`, `hrvService`, `/details`-серії,
      calendar/workouts). Результат зафіксувати в цьому тікеті (що працює/що ні).
      → **Прогони 2–3 на Pi (2026-07-07): 0 FAIL** — логін+MFA, token resume
      без MFA і всі endpoint-и включно з `/details`-серією працюють (таблиці
      нижче). Не ганявся лише опційний write-раундтріп (`--write-test`) —
      зробити при самій міграції, перед свапом провайдера.
- [x] Задокументований план міграції: `_UserGarthProvider` → новий провайдер
      (endpoint-и connectapi ті самі; `client.py` писався провайдер-агностично,
      майже не чіпається); адаптація MFA-моста (`app/garmin/mfa.py`) до нового
      login-флоу; token refresh — нова бібліотека вміє автооновлення DI-токенів,
      «раз на рік логін» може взагалі зникнути. → **див. «План міграції» нижче.**
- [x] Моніторинг: падіння логіну/token-обміну помітне одразу (ERROR-лог з
      розпізнаваним маркером або health-мітка) — це тригер запуску міграції.
      → **`GARMIN AUTH FAIL` (ERROR) + `GARMIN AUTH: stored token resume failed`
      (WARNING), див. «Моніторинг» нижче.**
- [x] Відомі дати expiry OAuth1-токенів усіх юзерів (read-only декодування з БД).
      → **`python -m app.cli token-expiry`; знімок 2026-07-07 нижче.**
- [x] `gconn`-провайдер збережено (див. CODE-03) — `_GConnProvider` у
      `app/garmin/providers.py` живий; CODE-03 явно виключає його з видалення.

## Моніторинг (зроблено 2026-07-07)

Обидва маркери grep-стабільні, пишуться логером `garmin` у `bot.log` + stdout:

- **`GARMIN AUTH FAIL`** (ERROR) — свіжий email+password логін не пройшов.
  Живе в `app/garmin/mfa.py` (фоновий потік `start_login` — єдина точка, через
  яку йдуть усі свіжі логіни: і `_UserGarthProvider`, і `/settings`-конект).
  **Це і є тригер міграції**: `grep "GARMIN AUTH FAIL" bot.log`.
- **`GARMIN AUTH: stored token resume failed`** (WARNING) —
  `_UserGarthProvider` не зміг відновити збережений токен і падає у свіжий
  логін. Якщо такі рядки з'являються для токенів, яким не ~рік — Garmin,
  імовірно, зламав OAuth2-обмін: дивитись, чи не йде слідом `GARMIN AUTH FAIL`.

Помилки *фетчів* (включно з падінням OAuth2-обміну всередині garth під час
запиту) і далі видно як `GARMIN ERR <label>` (`client._safe`) — але вони
повертають `{"_error": ...}` і не валять процес, тому головний сигнал — саме
логін-маркери.

## Дати expiry OAuth1-токенів (знімок 2026-07-07)

`./venv/bin/python -m app.cli token-expiry` (read-only: сирий SELECT трьох
колонок + розшифровка, працює навіть на напівмігрованій БД):

| user | oauth1 виданий | помирає ≈ |
| --- | --- | --- |
| 1 · sergiwez@gmail.com | 2026-07-06 (свіжий — повний логін з перевірки §1.5) | **2027-07-06** |
| 2 · sergiwez+1@gmail.com | немає збереженого токена | — |

Оцінка «виданий» = `iat` OAuth2-JWT зі збереженого блоба (ми персистимо блоб
лише одразу після свіжого логіну, тож `iat` == момент видачі OAuth1);
життя OAuth1 ≈ 365 днів (`app/garmin/token_info.py`).

## Розвідка: скрипт і як його ганяти

`scripts/ops01_recon_gconn.py` — standalone (нуль імпортів з `app`), ганяти
**на Pi** (не на маку): Cloudflare-репутація прив'язана до IP/регіону проду,
архітектура aarch64 має власні wheels для `curl_cffi`, Python — той, що на
проді. В **окремому одноразовому venv** з найновішим `python-garminconnect`;
скрипт standalone, тож досить `scp` самого файлу, якщо гілка не запулена:

```bash
# на Pi:
python3 --version   # garminconnect 0.3.6 вимагає >= 3.12 — див. нижче
python3 -m venv /tmp/ops01-venv
/tmp/ops01-venv/bin/pip install --upgrade garminconnect
GARMIN_EMAIL=... GARMIN_PASSWORD=... \
    /tmp/ops01-venv/bin/python scripts/ops01_recon_gconn.py [--write-test]
```

Чому не в проєктному venv: там пін `garth==0.4.47` і `garminconnect==0.2.8`,
а **0.2.8 логіниться через той самий старий garth** — прогін у ньому нічого не
каже про Cloudflare-еру auth-движок.

Вже відомо без живого прогону (перевірка метаданих 2026-07-07): актуальний
`garminconnect` — **0.3.6**, тягне `curl_cffi` (нативний движок підтверджено)
і має **`Requires-Python >= 3.12`**. Проєктний venv — Python 3.9 (мак-дев);
**на Pi — Python 3.13.5 (перевірено 2026-07-07)**, тобто апгрейд Python для
міграції не потрібен — лишається тільки звірити, що для `curl_cffi` є
aarch64-wheel (якщо pip почне компілювати — вписати сюди).

Скрипт: логін (resume → fresh, MFA через stdin, сумісний зі старим і новим API
бібліотеки), усі наші endpoint-и (sleep/HRV/stress/body battery/readiness/
user summary/VO2max/race predictions/endurance/daily events/activities/
`/details`-серії/exerciseSets/calendar/workouts), опційний write-раундтріп
workout-а (`--write-test`: create → schedule → delete). Токени — в ізольований
`./.ops01_tokens` (у .gitignore); `~/.garth` і БД не чіпає. Свіжий логін мінтить
новий OAuth1, але старі лишаються чинними (перевірено 2026-07-06 — прод
працював далі після паралельного повного логіну).

### Результати прогону

**Прогін 1 — Pi, 2026-07-07 08:42, python 3.13.5, garminconnect 0.3.6** (частковий —
скрипт ще розраховував на старий API бібліотеки):

- ✅ **Свіжий email+password логін з MFA пройшов** — головне питання розвідки
  закрите: curl_cffi-обхід Cloudflare працює з IP проду. Движок нативний
  (garth не встановлено взагалі).
- ⚠️ По дорозі mobile-флоу двічі зловили **429 «IP rate limited by Garmin»**,
  перш ніж спрацював робочий флоу — живе підтвердження, що PERF-05
  (rate limit/backoff) мусить жити в цьому ж шарі.
- ❌ `token save` / `profile` впали з `AttributeError: 'Garmin' object has no
  attribute 'garth'` — **0.3.6 позбувся внутрішнього garth-клієнта** (у 0.2.x
  токени/профіль жили на `api.garth`). Скрипт виправлено: профіль — через
  `/userprofile-service/socialProfile`, збереження токенів — адаптивне
  (пробує відомі API, інакше друкує кандидатів). Наслідок для плану міграції:
  формат/API збереження сесії у 0.3.6 інший — звірити з прогону 2, конвертація
  збережених у БД garth-блобів може не бути тривіальною.
- Endpoint-и ще не перевірені (скрипт обірвався на profile) — прогін 2.

**Прогін 2 — Pi, 2026-07-07 08:49, python 3.13.5, garminconnect 0.3.6** —
**19 перевірок, 0 FAIL**:

| перевірка | статус | нотатка |
| --- | --- | --- |
| login: fresh email+password | PASS | MFA prompted |
| login: token save | PASS | ./.ops01_tokens via **api.client.dump** |
| profile: userName/displayName | PASS | via socialProfile endpoint |
| sleep | PASS | dict[24] |
| hrv (hrvService) | PASS | dict[11] |
| stress | PASS | dict[14] |
| body battery | PASS | list[1] |
| training readiness | PASS | list[3] |
| user summary | PASS | dict[94] |
| vo2max (maxmet) | EMPTY | немає даних за день — endpoint живий |
| race predictions | PASS | dict[8] |
| endurance score | PASS | dict[16] |
| daily events | PASS | list[2] |
| activities list | PASS | list[10] |
| activity details/series | SKIP | серед останніх 10 активностей не було бігу |
| exerciseSets | PASS | dict[2] |
| calendar month | PASS | dict[6] |
| workouts list | PASS | list[10] |
| workout full | PASS | dict[33] |

Висновки прогону 2:

- **План Б підтверджено практично повністю**: логін+MFA, всі щоденні
  endpoint-и, activities, exerciseSets, calendar, workouts — працюють через
  нативний движок з IP проду.
- **Токен-store garth-сумісний**: нативний клієнт 0.3.6 живе на `api.client`
  з `dump`/`load` у стилі garth — міст для MFA і конвертація збережених у БД
  блобів виглядають простіше, ніж закладалось (звірити формат каталогу
  `.ops01_tokens` з garth-дампом при міграції).
- Залишались дві неперевірені гілки — закриті прогоном 3 (нижче), крім
  опційного write-раундтріпа.

**Прогін 3 — Pi, 2026-07-07 08:59, resume-прогін** — **18 перевірок, 0 FAIL**:

- ✅ **`login: token resume` PASS без MFA** — збережена у прогоні 2 сесія
  (`api.client.dump` → `Garmin().login(token_dir)`) відновилась; «раз на рік
  логін» у плані Б справді може зникнути.
- ✅ **`/details`-серія PASS** (activity 23365713848, dict[10]) — з лімітом 50
  біг знайшовся; останній read-endpoint закритий.
- Решта endpoint-ів — ті самі PASS, що у прогоні 2 (vo2max так само EMPTY —
  нема даних за день, endpoint живий).
- Не ганявся: **write-раундтріп** (`--write-test`: create → schedule → delete
  workout) — свідомо відкладений до самої міграції, щоб не писати в календар
  без потреби.

**Підсумок розвідки: план Б робочий.** Нативний `garminconnect` 0.3.6
(curl_cffi) з IP проду: логін + MFA + resume + всі наші read-endpoint-и — 0 FAIL.

## План міграції (виконувати ПІСЛЯ фактичної поломки garth, не зараз)

Поточна архітектура вже провайдер-агностична — міграція локалізована:

1. **Залежності**: розпінити `garminconnect` → найновіший (curl_cffi
   TLS-імперсонація), прибрати пін `garth==0.4.47` (новий garminconnect
   приведе свою сумісну версію як транзитивну залежність). Свіжий
   garminconnect (0.3.6) вимагає **Python ≥ 3.12** — на Pi вже 3.13.5
   (перевірено 2026-07-07), апгрейдити треба лише дев-venv на маку (3.9).
2. **Провайдер**: у `app/garmin/providers.py` додати `_UserGConnProvider`
   (аналог `_UserGarthProvider`): `Garmin(email=..., password=..., prompt_mfa=...)`
   з ізольованим станом на юзера; `connectapi(path, **kwargs)` — той самий
   інтерфейс, `client.py` не чіпається. Токени: `api.garth.dumps()`-еквівалент
   (нова бібліотека так само тримає garth-стан усередині — resume зі
   збереженого блоба в БД, `new_token` після свіжого логіну; формат блоба
   звіряти при прогоні, за потреби — конвертація в `loads`).
3. **MFA-міст** (`app/garmin/mfa.py`): міняється лише виклик у `_run`-потоці —
   `client.login(email, password, prompt_mfa=...)` → конструктор/`login()`
   нової бібліотеки з тим самим parked-callback `prompt_mfa`. Якщо новий API
   віддає `("needs_mfa", state)` + `resume_login` — міст навіть спрощується
   (без фонового потоку), але це окремий крок, спершу — мінімальний діф.
   Веб-флоу `/settings` і обробники `MFARequired` не змінюються.
4. **Token refresh**: нова бібліотека автооновлює DI-токени — «раз на рік
   логін» може зникнути. Перевірити, чи оновлений стан треба реперсистити
   (розширити `runtime.user_runtime`, який уже вміє зберігати `new_token`).
5. **Rate limit (PERF-05)**: робити в цьому ж шарі при міграції — після
   Cloudflare-ери агресивні патерни → ризик бана акаунта, не лише 429.
6. **Верифікація**: `pytest` (провайдер мокається, тести не залежать від
   бібліотеки) + живий прогін recon-скрипта + один повний цикл
   `/report` → morning job → `push-plan --dry-run`.
7. **Відкат**: пін `garth==0.4.47` лишається в git-історії; стан «до» —
   один `pip install -e .` від відновлення.

## Підводні камені

- Обхід Cloudflare — гонка озброєнь: Garmin може закрутити далі. Це постійний
  операційний податок неофіційного API (ANALYSIS.md §2.4), прийнятий свідомо.
- Не чіпати робочий garth-шлях, поки він працює; пін версії 0.4.47 важливий.
- Rate limit (PERF-05) робити в тому самому шарі: після Cloudflare-ери агресивні
  патерни запитів — ризик бана акаунта, не лише 429.

Деталі: ANALYSIS.md §0, §1.5, §4.4.
