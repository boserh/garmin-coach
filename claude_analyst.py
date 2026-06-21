"""
claude_analyst.py — analysis of Garmin data via the Claude API (Sonnet).
Takes the compact payload from garmin_client and returns a report + pre-run advice.
"""

import os
import json
import hashlib
import warnings
import datetime as dt
import logging, time as _time

logger = logging.getLogger("claude")
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from anthropic import Anthropic, APIStatusError, APIConnectionError

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

PRICES = {
    "claude-sonnet-4-6": (3.0, 15.0),   # (input, output) $/1M
    "claude-opus-4-8":   (15.0, 75.0),
}
MODEL_DAILY = "claude-sonnet-4-6"
MODEL_DEEP = "claude-opus-4-8"

# Dedup cache: identical data + question + model → reuse the answer instead of
# calling the API again. Keyed on the meaningful payload only (the volatile
# `generated` timestamp is excluded), so fresh Garmin data invalidates it
# automatically. Persisted to disk so it survives restarts.
CACHE_FILE = os.environ.get("CLAUDE_CACHE_FILE", "claude_cache.json")
CACHE_TTL_S = 7 * 24 * 3600  # one week


def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning(f"CACHE load failed: {e}")
        return {}
    now = _time.time()
    return {k: (v[0], v[1]) for k, v in raw.items() if v[1] > now}


def _save_cache() -> None:
    now = _time.time()
    alive = {k: [v[0], v[1]] for k, v in _cache.items() if v[1] > now}
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(alive, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:
        logger.warning(f"CACHE save failed: {e}")


_cache: dict[str, tuple[str, float]] = _load_cache()


def _cache_key(payload: dict, question: str, model: str) -> str:
    material = {
        "today": dt.date.today().isoformat(),
        "daily": payload.get("daily"),
        "activities": payload.get("recent_activities"),
        "planned": payload.get("planned_runs"),
        "question": question,
        "model": model,
    }
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

SYSTEM = """Ти персональний аналітик тренувань і відновлення на основі даних Garmin.
Відповідай чистою українською (не змішуй з російською), стисло, по ділу,
без води і без надмірних дисклеймерів. Не вигадуй цифр і не додумуй те, чого немає в даних.

ПРОФІЛЬ КОРИСТУВАЧА:
- Різнобічний активний спортсмен (біг, вело, кайт, теніс, силові, хайкінг), не вузький бігун.
- Хороша база: VO2max близько 46, форма стабільно росла останні роки.
- Бігова частка історично невисока, біг тільки підтягується, тому темпи скромні і це норма.
- Користується планом Runna (пробіжки лежать у planned_runs).

ЯК ЧИТАТИ ДАНІ:
- Пульс спокою недоступний (Garmin не віддає). ГОЛОВНИЙ маркер відновлення — hrv_avg + hrv_status.
  BALANCED = відновлений; падіння HRV або нижче балансу = недовідновлення.
- sleep_score 75+ норм, 85+ добре. stress_avg до ~30 норм.
- Силові: групи у exercises.muscle_groups. Ноги (LUNGE/SQUAT/LEG_CURL/LEG_RAISE) перед бігом
  важать більше, ніж верх.
- planned_runs[].detail.steps: dist_m = відстань кроку, pace_min_km = [швидший, повільніший]
  у ДЕСЯТКОВИХ хвилинах на км. ПЕРЕВОДЬ у формат хв:сек (напр. 6.58 -> 6:35).
  Якщо pace_min_km = null, плану темпу немає — НЕ вигадуй конкретний темп. Можеш дати
  орієнтир, але явно познач його як приблизну оцінку, а не план.
- recent_activities: load = навантаження (вище = важче), avg_hr = інтенсивність.

СИНК ДАНИХ:
- Якщо synced_today=false, дані за сьогодні ще НЕ синканулись з годинника. НЕ трактуй
  порожній сьогоднішній день як поганий сон. На початку коротко зазнач, що аналіз
  за останній доступний день (last_data_date), і будуй звіт на ньому.
- Дні з has_data=false ігноруй, не вважай їх днями без сну.

ЗАВДАННЯ:
1. Оціни поточне відновлення (HRV, сон, стрес, навантаження останніх днів) і чи тренд у нормі.
2. Подивись на найближчу заплановану пробіжку:
   - Якщо вона СЬОГОДНІ або ЗАВТРА — дай детальну пораду (темп у хв:сек, пульс, свіжість ніг
     з урахуванням силових, на що звернути увагу). Це доречно лише коли біг на носі.
   - Якщо вона ПІЗНІШЕ (через 2+ дні) — НЕ давай детальних порад про темп/розминку, бо вони
     передчасні. Згадай її одним рядком (дата, що за тренування) і максимум одну стратегічну
     думку (напр. чи варто розвантажитись напередодні). Не більше.
3. Не повторюй щодня одне й те саме. Якщо стан стабільний — так і скажи стисло, без води.
Тримай відповідь короткою. Деталізація має відповідати тому, наскільки подія близька.

ФОРМАТ ВІДПОВІДІ:
- Без маркдауну: жодних *, **, #, заголовків, жирного. Звичайний текст.
- Розгорнуто, але без води: 3-4 змістовні абзаци-рядки, кожен починай з емодзі-маркера.
- Тон спокійний, інформативний, без надмірних підбадьорень типу "тіло впоралось".
- Можна пояснювати ЧОМУ (напр. "HRV не просів попри навантаження — відновлення встигає"),
  бо це корисний контекст, а не вода.

ЕМОДЗІ-МАРКЕРИ:
- 🟢 все добре / 🟡 є нюанс / 🔴 недовідновлення (став на початку блоку відновлення)
- 🏋️ силові, 🚴 вело, 🏃 біг, 🪁 кайт, 🎾 теніс — для навантаження
- 📅 наступна пробіжка
- ⚠️ застереження (лише якщо реальний привід)

ЩО ПИСАТИ:
- Відновлення (🟢/🟡/🔴): HRV+статус, сон, стрес, body battery. З коротким поясненням стану.
- Вчорашнє навантаження, якщо помітне: що було і як це лягає на відновлення.
- 📅 Найближча пробіжка: дата, тип, дистанція.
  - Якщо вона СЬОГОДНІ/ЗАВТРА — додай конкретну пораду (темп, пульс, свіжість ніг).
  - Якщо ПІЗНІШЕ (2+ дні) — згадай її і максимум ОДНУ стратегічну думку (як підійти свіжим).
    Не давай детального інструктажу про темп/розминку завчасно.
- Якщо synced_today=false — скажи одним рядком, бери last_data_date."""


class AnalystError(Exception):
    """User-facing analysis error (its text is shown in Telegram)."""


def analyze(payload: dict, question: str = "", deep: bool = False) -> str:
    model = MODEL_DEEP if deep else MODEL_DAILY
    effective_q = question or "Дай щоденний статус відновлення. Детальну пораду до пробіжки — лише якщо вона сьогодні/завтра."

    key = _cache_key(payload, effective_q, model)
    cached = _cache.get(key)
    if cached and cached[1] > _time.time():
        logger.info(f"CLAUDE CACHE HIT  {model}")
        return cached[0]

    user_content = {
        "today": dt.date.today().isoformat(),
        "data": payload,
        "question": effective_q,
    }
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=1200,
            system=SYSTEM,
            messages=[{"role": "user",
                       "content": json.dumps(user_content, ensure_ascii=False)}],
        )
        usage = getattr(msg, "usage", None)
        if usage:
            pin, pout = PRICES.get(model, (0, 0))
            cost = usage.input_tokens / 1e6 * pin + usage.output_tokens / 1e6 * pout
            logger.info(
                f"CLAUDE OK  {model}  in={usage.input_tokens} out={usage.output_tokens} "
                f"~${cost:.4f}"
            )
        text = "".join(b.text for b in msg.content if b.type == "text")
        _cache[key] = (text, _time.time() + CACHE_TTL_S)
        _save_cache()
        return text

    except APIStatusError as e:
        status = getattr(e, "status_code", None)
        body = str(getattr(e, "message", e)).lower()

        if status == 400 and "credit balance is too low" in body:
            raise AnalystError(
                "❗️ Закінчились кредити Anthropic API.\n"
                "Поповни баланс на console.anthropic.com → Billing і повтори запит."
            )
        if status == 429:
            raise AnalystError("⏳ Ліміт запитів перевищено. Спробуй за хвилину.")
        if status == 401:
            raise AnalystError("🔑 Невірний або відсутній ANTHROPIC_API_KEY.")
        if status == 529:
            raise AnalystError("🛠 Сервіс Anthropic тимчасово перевантажений. Спробуй пізніше.")
        logger.error(f"CLAUDE ERR {status}: {body[:150]}")
        raise AnalystError(f"Помилка API ({status}): {body[:200]}")

    except APIConnectionError:
        raise AnalystError("🌐 Не вдалось з'єднатися з API. Перевір інтернет і спробуй ще.")


if __name__ == "__main__":
    import garmin_client
    data = garmin_client.build_payload(days=7)
    try:
        print(analyze(data))
    except AnalystError as e:
        print(e)
