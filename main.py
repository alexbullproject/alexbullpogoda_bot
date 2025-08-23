import os
import json
from typing import Optional, Dict, Any, List
import aiohttp
from aiohttp import web
import pytz
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

# Environment variables
load_dotenv(encoding="utf-8")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_USERNAME", "@alexbullpogoda")
JOIN_URL = f"https://t.me/{CHANNEL_ID.lstrip('@')}"
DATA_FILE = "data.json"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "secret123")
BASE_URL = os.getenv("BASE_URL")
WEBHOOK_PATH = f"/webhook/{WEBHOOK_SECRET}"

# Weather API URLs
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Runtime state
LAST_CITY = {}
PICK_OPTIONS = {}
STATE = {"users": {}}
scheduler = AsyncIOScheduler()

def load_state():
    global STATE
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                STATE = json.load(f)
        except Exception:
            STATE = {"users": {}}

def save_state():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(STATE, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

def ensure_user(user_id):
    users = STATE.setdefault("users", {})
    u = users.get(str(user_id))
    if not u:
        u = {}
        users[str(user_id)] = u
    return u

async def is_subscribed(bot, user_id):
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    except Exception:
        return False

async def require_subscription(message, bot):
    if await is_subscribed(bot, message.from_user.id):
        return True
    kb = InlineKeyboardBuilder()
    kb.button(text=" Подписаться на канал", url=JOIN_URL)
    kb.button(text=" Проверить подписку", callback_data="check_sub")
    kb.adjust(1)
    await message.answer(
        "Функция доступна только подписчикам канала.\n"
        "1) Подпишись на канал\n"
        "2) Нажми Проверить подписку ",
        reply_markup=kb.as_markup(),
    )
    return False

async def geocode_city(session, query, count=5):
    params = {"name": query, "count": count, "language": "ru", "format": "json"}
    async with session.get(GEOCODE_URL, params=params, timeout=15) as r:
        if r.status != 200:
            return []
        data = await r.json()
    return data.get("results") or []

async def fetch_tomorrow_forecast(session, lat, lon, tz):
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

def format_city_label(geo):
    name = geo.get("name", "")
    admin = geo.get("admin1") or ""
    country = geo.get("country_code") or ""
    label = f"{name}, {admin}, {country}".strip().strip(", ")
    while ", ," in label:
        label = label.replace(", ,", ",")
    return label

def format_forecast_text(city_label, tz, f):
    emoji = ""
    wmo = f.get("weathercode")
    if wmo == 0:
        emoji = ""
    elif wmo in (1, 2):
        emoji = ""
    elif wmo == 3:
        emoji = ""
    elif 51 <= wmo <= 67:
        emoji = ""
    elif 80 <= wmo <= 82:
        emoji = ""
    
    parts = [
        f"{emoji} Прогноз на завтра для {city_label} ({f['date']})",
        f"Температура: от {round(f['tmin'])} до {round(f['tmax'])}C",
        f"Восход: {f['sunrise']}  Закат: {f['sunset']}",
    ]
    return "\n".join(parts)

async def handle_city_query(message, query):
    user_id = message.from_user.id
    LAST_CITY[user_id] = query
    async with aiohttp.ClientSession() as session:
        results = await geocode_city(session, query)
    if not results:
        await message.answer("Не нашёл такой город. Попробуйте ещё раз.")
        return
    
    geo = results[0]
    label = format_city_label(geo)
    lat = float(geo["latitude"])
    lon = float(geo["longitude"])
    tz = geo.get("timezone", "UTC")
    
    user = ensure_user(user_id)
    user.update({"city_label": label, "lat": lat, "lon": lon, "tz": tz})
    save_state()
    
    async with aiohttp.ClientSession() as session:
        fc = await fetch_tomorrow_forecast(session, lat, lon, tz)
    if not fc:
        await message.answer("Не получилось получить прогноз.")
        return
    
    text = format_forecast_text(label, tz, fc)
    await message.answer(text)

async def on_startup(app):
    scheduler.configure(timezone=pytz.UTC, event_loop=asyncio.get_running_loop())
    scheduler.start()
    bot = app["bot"]
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}{WEBHOOK_PATH}")

async def on_shutdown(app):
    bot = app["bot"]
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN not set")
    
    load_state()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    
    @dp.message(CommandStart())
    async def start(m):
        if not await require_subscription(m, bot):
            return
        await m.answer(" Привет! Пришлите название города для прогноза погоды.")
    
    @dp.message(Command("help"))
    async def help_cmd(m):
        await m.answer("Пришлите название города  получите прогноз на завтра.")
    
    @dp.callback_query(F.data == "check_sub")
    async def check_sub(c):
        if await is_subscribed(bot, c.from_user.id):
            await c.message.answer(" Подписка подтверждена! Отправьте название города.")
        else:
            await c.answer("Не вижу подписку. Подпишись и нажми снова.", show_alert=True)
    
    @dp.message(F.text)
    async def any_text(m):
        if not await require_subscription(m, bot):
            return
        await handle_city_query(m, m.text.strip())
    
    app = web.Application()
    app["bot"] = bot
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", "10000")))

if __name__ == "__main__":
    main()
