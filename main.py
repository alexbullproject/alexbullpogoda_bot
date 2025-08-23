# main.py — Telegram weather bot (aiogram v3, webhook, Open‑Meteo)

import os
import json
from typing import Optional, Dict, Any, List

import aiohttp
from aiohttp import web
import pytz

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv


# ========================== ENV ==========================
load_dotenv(encoding="utf-8")

BOT_TOKEN   = os.getenv("BOT_TOKEN")
CHANNEL_ID  = os.getenv("CHANNEL_USERNAME", "@alexbullpogoda")  # публичный @username канала
JOIN_URL    = f"https://t.me/{CHANNEL_ID.lstrip('@')}"
DATA_FILE   = "data.json"

# вебхук
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret123")
BASE_URL       = os.getenv("BASE_URL")  # например: https://alexbullpogoda.osc-fr1.scalingo.io
WEBHOOK_PATH   = f"/webhook/{WEBHOOK_SECRET}"

# общий планировщик (стартуем его в on_startup)
scheduler = AsyncIOScheduler()


# ====================== CONSTANTS/API ====================
GEOCODE_URL  = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HELP_TEXT = (
    "Пришлите название города (на русском или латиницей) — отвечу прогнозом на завтра.\n\n"
    "Команды:\n"
    "• /start — начать\n"
    "• /help — помощь\n"
    "• /repeat — повторить прогноз по последнему городу\n"
    "• /daily HH:MM — присылать прогноз каждый день (ваше местное время)\n"
    "• /stop — остановить ежедневную рассылку\n"
)


# ========================= STATE =========================
# В оперативке
LAST_CITY: Dict[int, str] = {}                       # user_id -> последний введённый город (строка)
PICK_OPTIONS: Dict[int, List[Dict[str, Any]]] = {}   # user_id -> варианты геокодинга для выбора

# На диске
STATE: Dict[str, Any] = {"users": {}}                # user_id(str) -> { city_label, lat, lon, tz, daily? }

def load_state() -> None:
    global STATE
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                STATE = json.load(f)
        except Exception:
            STATE = {"users": {}}

def save_state() -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

def ensure_user(user_id: int) -> Dict[str, Any]:
    users = STATE.setdefault("users", {})
    u = users.get(str(user_id))
    if not u:
        u = {}
        users[str(user_id)] = u
    return u


# ======================== HELPERS ========================
def wmo_to_emoji(wmo: Optional[int]) -> str:
    if wmo is None: return "🌤️"
    if wmo in (0,): return "☀️"
    if wmo in (1, 2): return "🌤️"
    if wmo in (3,): return "☁️"
    if 45 <= wmo <= 48: return "🌫️"
    if 51 <= wmo <= 67: return "🌦️"
    if 71 <= wmo <= 77: return "🌨️"
    if 80 <= wmo <= 82: return "🌧️"
    if 85 <= wmo <= 86: return "❄️"
    if 95 <= wmo <= 99: return "⛈️"
    return "🌤️"

def format_wind_dir_full(deg: Optional[float]) -> str:
    if deg is None:
        return "Нет данных"
    names = [
        "Север", "Северо‑северо‑восток", "Северо‑восток", "Восток‑северо‑восток",
        "Восток", "Восток‑юго‑восток", "Юго‑восток", "Юго‑юго‑восток",
        "Юг", "Юго‑юго‑запад", "Юго‑запад", "Запад‑юго‑запад",
        "Запад", "Запад‑северо‑запад", "Северо‑запад", "Северо‑северо‑запад"
    ]
    i = int((deg % 360) / 22.5 + 0.5) % 16
    return names[i]

def format_city_label(geo: Dict[str, Any]) -> str:
    name = geo.get("name", "")
    admin = geo.get("admin1") or ""
    country = geo.get("country_code") or ""
    label = f"{name}, {admin}, {country}".strip().strip(", ")
    while ", ," in label:
        label = label.replace(", ,", ",")
    return label

def format_forecast_text(city_label: str, tz: str, f: Dict[str, Any]) -> str:
    emoji = wmo_to_emoji(f["weathercode"])
    wind_dir = format_wind_dir_full(f["wind_dir"])
    precip = f["precip_mm"]
    precip_line = f"Осадки: {precip:.1f} мм" if precip is not None else "Осадки: —"
    prob = f["precip_prob"]
    prob_line = f"Вероятность осадков: {prob}%" if prob is not None else "Вероятность осадков: —"
    parts = [
        f"{emoji} Прогноз на завтра для *{city_label}* ({f['date']}).",
        f"Температура: от {round(f['tmin'])}° до {round(f['tmax'])}°C",
        f"Облачность: {f['clouds']}%" if f.get("clouds") is not None else "Облачность: —",
        precip_line,
        prob_line,
        f"Ветер: до {round(f['wind_max'])} м/с, направление: {wind_dir}" if f.get("wind_max") is not None else "Ветер: —",
        f"Восход: {f['sunrise']}  Закат: {f['sunset']}",
    ]
    return "\n".join(parts)


# ===================== OPEN‑METEO CALLS ==================
async def geocode_city(session: aiohttp.ClientSession, query: str, count: int = 5) -> List[Dict[str, Any]]:
    params = {"name": query, "count": count, "language": "ru", "format": "json"}
    async with session.get(GEOCODE_URL, params=params, timeout=15) as r:
        if r.status != 200:
            return []
        data = await r.json()
    return data.get("results") or []

async def fetch_tomorrow_forecast(session: aiohttp.ClientSession, lat: float, lon: float, tz: str) -> Optional[Dict[str, Any]]:
    params = {
        "latitude": lat, "longitude": lon, "timezone": tz,
        "daily": [
            "temperature_2m_max","temperature_2m_min",
            "precipitation_sum","precipitation_probability_max",
            "windspeed_10m_max","winddirection_10m_dominant",
            "weathercode","sunrise","sunset","cloudcover_mean",
        ],
    }
    async with session.get(FORECAST_URL, params=params, timeout=15) as r:
        if r.status != 200:
            return None
        data = await r.json()

    daily = data.get("daily") or {}
    dates = daily.get("time") or []
    if not dates:
        return None
    idx = 1 if len(dates) > 1 else 0

    def pick(key, default=None):
        arr = daily.get(key)
        return arr[idx] if isinstance(arr, list) and len(arr) > idx else default

    return {
        "date": dates[idx],
        "tmax": pick("temperature_2m_max"),
        "tmin": pick("temperature_2m_min"),
        "precip_mm": pick("precipitation_sum", 0),
        "precip_prob": pick("precipitation_probability_max"),
        "wind_max": pick("windspeed_10m_max"),
        "wind_dir": pick("winddirection_10m_dominant"),
        "weathercode": pick("weathercode"),
        "sunrise": pick("sunrise"),
        "sunset": pick("sunset"),
        "clouds": pick("cloudcover_mean"),
    }


# ===================== SUBSCRIPTION CHECK =================
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    except Exception:
        return False

async def require_subscription(message: types.Message, bot: Bot) -> bool:
    if await is_subscribed(bot, message.from_user.id):
        return True
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подписаться на канал", url=JOIN_URL)
    kb.button(text="🔄 Проверить подписку", callback_data="check_sub")
    kb.adjust(1)
    await message.answer(
        "Функция доступна только подписчикам канала.\n"
        "1) Подпишись на канал\n"
        "2) Нажми «Проверить подписку» 👇",
        reply_markup=kb.as_markup(),
    )
    return False


# ========================= ACTIONS ========================
async def send_tomorrow_forecast(bot: Bot, user_id: int):
    user = ensure_user(user_id)
    if not user.get("lat"):
        await bot.send_message(user_id, "У вас не выбран город. Напишите название города сообщением, например: «Гродно».")
        return
    lat = user["lat"]; lon = user["lon"]; tz = user["tz"]; label = user["city_label"]
    async with aiohttp.ClientSession() as session:
        fc = await fetch_tomorrow_forecast(session, lat, lon, tz)
    if not fc:
        await bot.send_message(user_id, "Не удалось получить прогноз. Попробуйте позже.")
        return
    text = format_forecast_text(label, tz, fc)
    await bot.send_message(user_id, text, parse_mode=ParseMode.MARKDOWN)

def schedule_daily(user_id: int, time_str: str, tz: str, bot: Bot):
    job_id = f"daily_{user_id}"
    job = scheduler.get_job(job_id)
    if job:
        job.remove()
    hour, minute = map(int, time_str.split(":"))
    trigger = CronTrigger(hour=hour, minute=minute, timezone=pytz.timezone(tz))
    scheduler.add_job(send_tomorrow_forecast, trigger, args=[bot, user_id], id=job_id, replace_existing=True)

def cancel_daily(user_id: int):
    job_id = f"daily_{user_id}"
    job = scheduler.get_job(job_id)
    if job:
        job.remove()

async def handle_city_query(message: types.Message, query: str):
    user_id = message.from_user.id
    LAST_CITY[user_id] = query
    async with aiohttp.ClientSession() as session:
        results = await geocode_city(session, query, count=5)
    if not results:
        await message.answer("Не нашёл такой город. Попробуйте ещё раз (можно добавить страну: «Гродно, BY»).")
        return
    if len(results) == 1:
        await apply_city_and_reply(message, results[0])
        return

    PICK_OPTIONS[user_id] = results
    kb = InlineKeyboardBuilder()
    for idx, geo in enumerate(results[:5]):
        label = format_city_label(geo)
        kb.button(text=label[:64], callback_data=f"pick:{idx}")
    kb.adjust(1)
    await message.answer("Уточните, пожалуйста, город:", reply_markup=kb.as_markup())

async def apply_city_and_reply(message: types.Message, geo: Dict[str, Any]):
    user_id = message.from_user.id
    label = format_city_label(geo)
    lat = float(geo["latitude"]); lon = float(geo["longitude"])
    tz = geo.get("timezone", "UTC")
    user = ensure_user(user_id)
    user.update({"city_label": label, "lat": lat, "lon": lon, "tz": tz})
    save_state()

    async with aiohttp.ClientSession() as session:
        fc = await fetch_tomorrow_forecast(session, lat, lon, tz)
    if not fc:
        await message.answer("Не получилось получить прогноз. Попробуйте позже.")
        return

    text = format_forecast_text(label, tz, fc)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Подписаться на ежедневный прогноз (08:00)", callback_data="daily:08:00")
    kb.adjust(1)
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb.as_markup())


# ===================== WEBHOOK SERVER ====================
async def on_startup(app: web.Application):
    # Запускаем планировщик, когда уже есть event loop
    scheduler.configure(timezone=pytz.UTC, event_loop=asyncio.get_running_loop())
    scheduler.start()

    # Ставим вебхук (если BASE_URL уже задан)
    bot: Bot = app["bot"]
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}{WEBHOOK_PATH}")

async def on_shutdown(app: web.Application):
    bot: Bot = app["bot"]
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

def run_webhook(bot: Bot, dp: Dispatcher):
    app = web.Application()
    app["bot"] = bot
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))


# =========================== MAIN =========================
import asyncio  # после определения on_startup (для get_running_loop)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN в переменных окружения")

    load_state()

    bot = Bot(BOT_TOKEN, parse_mode=ParseMode.MARKDOWN)
    dp = Dispatcher()

    @dp.message(CommandStart())
    async def start(m: types.Message):
        if not await require_subscription(m, bot):
            return
        await m.answer(
            "Привет! 👋 Напишите название города — пришлю прогноз на завтра.\n\n" + HELP_TEXT
        )

    @dp.message(Command("help"))
    async def help_cmd(m: types.Message):
        await m.answer(HELP_TEXT)

    @dp.message(Command("repeat"))
    async def repeat_cmd(m: types.Message):
        uid = m.from_user.id
        city = LAST_CITY.get(uid) or ensure_user(uid).get("city_label")
        if not city:
            await m.answer("Я ещё не знаю ваш город. Пришлите название города сообщением.")
            return
        user = ensure_user(uid)
        if user.get("lat"):
            class FakeMsg:
                from_user = m.from_user
                async def answer(self, text, **kwargs):
                    await m.answer(text, **kwargs)
            geo = {
                "latitude": user["lat"],
                "longitude": user["lon"],
                "timezone": user["tz"],
                "name": user.get("city_label"),
            }
            await apply_city_and_reply(FakeMsg(), geo)
        else:
            await handle_city_query(m, city)

    @dp.message(Command("daily"))
    async def daily_cmd(m: types.Message):
        if not await require_subscription(m, bot):
            return
        parts = m.text.strip().split()
        if len(parts) != 2 or ":" not in parts[1]:
            await m.answer("Использование: /daily HH:MM\nНапример: /daily 08:30")
            return
        time_str = parts[1]
        uid = m.from_user.id
        user = ensure_user(uid)
        if not user.get("lat"):
            await m.answer("Сначала выберите город: пришлите его название сообщением.")
            return
        user["daily"] = {"time": time_str}
        save_state()
        schedule_daily(uid, time_str, user["tz"], bot)
        await m.answer(f"Готово! Буду присылать прогноз каждый день в {time_str} по вашему времени ({user['tz']}).")

    @dp.message(Command("stop"))
    async def stop_cmd(m: types.Message):
        uid = m.from_user.id
        cancel_daily(uid)
        user = ensure_user(uid)
        user.pop("daily", None)
        save_state()
        await m.answer("Ежедневная рассылка отключена.")

    @dp.callback_query(F.data == "check_sub")
    async def check_sub(c: types.CallbackQuery):
        if await is_subscribed(bot, c.from_user.id):
            await c.message.answer("✅ Подписка подтверждена! Теперь отправьте название города.")
        else:
            await c.answer("Не вижу подписку. Подпишись и нажми снова.", show_alert=True)

    @dp.callback_query(F.data.startswith("pick:"))
    async def pick_city(c: types.CallbackQuery):
        uid = c.from_user.id
        opts = PICK_OPTIONS.get(uid) or []
        try:
            idx = int(c.data.split(":")[1])
        except Exception:
            await c.answer("Ошибка выбора.", show_alert=True)
            return
        if idx < 0 or idx >= len(opts):
            await c.answer("Слишком старый список — пришлите город ещё раз.", show_alert=True)
            return
        geo = opts[idx]
        PICK_OPTIONS.pop(uid, None)
        await c.message.edit_text(f"Вы выбрали: {format_city_label(geo)}")

        class FakeMsg:
            from_user = c.from_user
            async def answer(self, text, **kwargs):
                await c.message.answer(text, **kwargs)

        await apply_city_and_reply(FakeMsg(), geo)
        await c.answer()

    @dp.callback_query(F.data.startswith("daily:"))
    async def quick_daily(c: types.CallbackQuery):
        uid = c.from_user.id
        user = ensure_user(uid)
        if not user.get("lat"):
            await c.answer("Сначала выберите город.", show_alert=True)
            return
        _, t = c.data.split(":", 1)
        user["daily"] = {"time": t}
        save_state()
        schedule_daily(uid, t, user["tz"], bot)
        await c.message.answer(f"Подписал! Ежедневный прогноз в {t} по времени {user['tz']}.")
        await c.answer()

    @dp.message(F.text)
    async def any_text(m: types.Message):
        if not await require_subscription(m, bot):
            return
        await handle_city_query(m, m.text.strip())

    # запуск веб‑сервера (для PaaS Web Service)
    run_webhook(bot, dp)


if __name__ == "__main__":
    main()

