import os
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime
import json
import pytz

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, Command
from aiogram.enums.parse_mode import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

# Загружаем .env (на будущее) и задаём токен явно
from dotenv import load_dotenv
load_dotenv(encoding="utf-8")
BOT_TOKEN = os.getenv("BOT_TOKEN")  # ← вместо жестко прописанного токена


# === Проверка подписки на канал ===
from aiogram.enums import ChatMemberStatus

CHANNEL_ID = "@alexbullpogoda"                # публичный канал
JOIN_URL   = "https://t.me/alexbullpogoda"    # ссылка на канал

async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
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
        reply_markup=kb.as_markup()
    )
    return False
# === конец блока проверки подписки ===

DATA_FILE = "data.json"

# Runtime memory
LAST_CITY: Dict[int, str] = {}              # user_id -> last query text
PICK_OPTIONS: Dict[int, List[Dict[str, Any]]] = {}

# Persistent user settings (храним в data.json)
STATE: Dict[str, Any] = {"users": {}}

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

HELP_TEXT = (
    "Пришлите название города (на русском или латиницей) — отвечу прогнозом на завтра.\n\n"
    "Команды:\n"
    "• /start — начать\n"
    "• /help — помощь\n"
    "• /repeat — повторить прогноз по последнему городу\n"
    "• /daily HH:MM — присылать прогноз каждый день в указанное время (вашего города)\n"
    "• /stop — остановить ежедневную рассылку\n"
    "• /menu — показать клавиатуру\n"
)

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

def main_menu() -> ReplyKeyboardMarkup:
    # Клавиатура с удобными кнопками
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/daily 08:00"), KeyboardButton(text="/stop")],
            [KeyboardButton(text="🌆 Сменить город"), KeyboardButton(text="/help")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Напишите город (например, Минск)..."
    )

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

def format_wind_dir(deg: Optional[float]) -> str:
    # 16 румбов — полные русские названия
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
    wind_dir = format_wind_dir(f["wind_dir"])
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

def ensure_user(user_id: int) -> Dict[str, Any]:
    users = STATE.setdefault("users", {})
    u = users.get(str(user_id))
    if not u:
        u = {}
        users[str(user_id)] = u
    return u

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

def schedule_daily(scheduler: AsyncIOScheduler, bot: Bot, user_id: int, time_str: str, tz: str):
    job_id = f"daily_{user_id}"
    job = scheduler.get_job(job_id)
    if job:
        job.remove()
    hour, minute = map(int, time_str.split(":"))
    trigger = CronTrigger(hour=hour, minute=minute, timezone=pytz.timezone(tz))
    scheduler.add_job(send_tomorrow_forecast, trigger, args=[bot, user_id], id=job_id, replace_existing=True)

def cancel_daily(scheduler: AsyncIOScheduler, user_id: int):
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
        geo = results[0]
        await apply_city_and_reply(message, geo)
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
    tz = geo.get("timezone", "auto")

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

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN в .env")

    load_state()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    # Планировщик
    scheduler = AsyncIOScheduler(timezone=pytz.UTC)
    scheduler.start()

    @dp.message(CommandStart())
    async def start(m: types.Message):
        if not await require_subscription(m, bot):
            return
        await m.answer(
            "Привет! 👋 Напишите название города (на русском тоже можно) — пришлю прогноз на завтра.\n\n" + HELP_TEXT,
            reply_markup=main_menu()
        )

    @dp.message(Command("help"))
    async def help_cmd(m: types.Message):
        if not await require_subscription(m, bot):
            return
        await m.answer(HELP_TEXT, reply_markup=main_menu())

    @dp.message(Command("menu"))
    async def menu_cmd(m: types.Message):
        if not await require_subscription(m, bot):
            return
        await m.answer("Меню открыто. Выберите действие или напишите город:", reply_markup=main_menu())

    @dp.message(Command("repeat"))
    async def repeat_cmd(m: types.Message):
        if not await require_subscription(m, bot):
            return
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
            geo = {"latitude": user["lat"], "longitude": user["lon"], "timezone": user["tz"],
                   "name": user.get("city_label")}
            await apply_city_and_reply(FakeMsg(), geo)
        else:
            await handle_city_query(m, city)

    @dp.message(Command("daily"))
    async def daily_cmd(m: types.Message):
        if not await require_subscription(m, bot):
            return
        parts = m.text.strip().split()
        if len(parts) != 2 or ":" not in parts[1]:
            await m.answer("Использование: /daily HH:MM\nНапример: /daily 08:30", reply_markup=main_menu())
            return
        time_str = parts[1]
        uid = m.from_user.id
        user = ensure_user(uid)
        if not user.get("lat"):
            await m.answer("Сначала выберите город: пришлите его название сообщением.", reply_markup=main_menu())
            return
        user["daily"] = {"time": time_str}
        save_state()
        schedule_daily(scheduler, bot, uid, time_str, user["tz"])
        await m.answer(f"Готово! Буду присылать прогноз каждый день в {time_str} по вашему времени ({user['tz']}).", reply_markup=main_menu())

    @dp.message(Command("stop"))
    async def stop_cmd(m: types.Message):
        if not await require_subscription(m, bot):
            return
        uid = m.from_user.id
        cancel_daily(scheduler, uid)
        user = ensure_user(uid)
        user.pop("daily", None)
        save_state()
        await m.answer("Ежедневная рассылка отключена.", reply_markup=main_menu())

    @dp.callback_query(F.data == "check_sub")
    async def cb_check_sub(c: types.CallbackQuery):
        if await is_subscribed(bot, c.from_user.id):
            await c.message.answer("✅ Подписка подтверждена! Можешь пользоваться ботом.", reply_markup=main_menu())
        else:
            await c.answer("Кажется, ты ещё не подписан 🤔", show_alert=True)

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
        schedule_daily(scheduler, bot, uid, t, user["tz"])
        await c.message.answer(f"Подписал! Ежедневный прогноз в {t} по времени {user['tz']}.", reply_markup=main_menu())
        await c.answer()

    @dp.message(F.text)
    async def any_text(m: types.Message):
        if not await require_subscription(m, bot):
            return
        text = m.text.strip()
        if text == "🌆 Сменить город":
            await m.answer("Ок, пришлите новый город сообщением (например, «Минск»).", reply_markup=main_menu())
            return
        await handle_city_query(m, text)

    asyncio.run(dp.start_polling(bot))

if __name__ == "__main__":
    main()
