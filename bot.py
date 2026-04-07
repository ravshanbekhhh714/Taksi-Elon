"""
Elon Bot - Telegram bot for Surxandaryo-Toshkent transport announcements.
Built with aiogram 3.x + PostgreSQL.
"""

import asyncio
import logging
import sys
from os import getenv

from dotenv import load_dotenv
import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ContentType,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

load_dotenv()

# ─── Configuration ───────────────────────────────────────────────────────────

BOT_TOKEN = getenv("BOT_TOKEN")
CHANNEL_ID = getenv("CHANNEL_ID")
DATABASE_URL = getenv("DATABASE_URL")
DB_HOST = getenv("DB_HOST", "localhost")
DB_PORT = int(getenv("DB_PORT", "5432"))
DB_USER = getenv("DB_USER", "postgres")
DB_PASSWORD = getenv("DB_PASSWORD", "1234")
DB_NAME = getenv("DB_NAME", "elon_bot")

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)

# ─── FSM States ──────────────────────────────────────────────────────────────


class UserStates(StatesGroup):
    waiting_for_phone = State()        # Registration: phone number
    waiting_for_direction = State()    # "Toshkent↔Surxandaryo" flow
    waiting_for_pochta = State()       # "Pochta bor" flow
    waiting_for_driver_elon = State()  # Driver announcement flow


# ─── Keyboards ───────────────────────────────────────────────────────────────


def phone_kb() -> ReplyKeyboardMarkup:
    """Contact share button for phone registration."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Рақамни юбориш", request_contact=True)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_menu_kb() -> ReplyKeyboardMarkup:
    """Main 4-button menu shown after registration."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🚙 Тошкентдан - Сурхондарёга"),
                KeyboardButton(text="🚙 Сурхондарёдан - Тошкентга"),
            ],
            [KeyboardButton(text="📦 Почта бор")],
            [KeyboardButton(text="⚙️ Созламалар")],
        ],
        resize_keyboard=True,
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Бекор қилиш")]],
        resize_keyboard=True,
    )


def sozlamalar_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Клиент"), KeyboardButton(text="🚕 Шофёр")],
            [KeyboardButton(text="❌ Бекор қилиш")],
        ],
        resize_keyboard=True,
    )


def role_select_kb() -> ReplyKeyboardMarkup:
    """Menu for initially picking a role after registration."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Клиент"), KeyboardButton(text="🚕 Шофёр")],
        ],
        resize_keyboard=True,
    )


def driver_menu_kb() -> ReplyKeyboardMarkup:
    """Menu for drivers: post announcement or settings."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📢 Элон бериш")],
            [KeyboardButton(text="⚙️ Созламалар")],
        ],
        resize_keyboard=True,
    )


# ─── Database ────────────────────────────────────────────────────────────────

pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    if DATABASE_URL:
        # For Railway environments natively using a formatted connection string
        return await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        
    return await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        min_size=2,
        max_size=10,
    )


async def init_db(db_pool: asyncpg.Pool) -> None:
    """Create tables if they don't exist."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                full_name TEXT,
                phone TEXT,
                role TEXT DEFAULT 'client',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
        
        # Ensure the phone column exists for users created before this update
        await conn.execute(
            """
            ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT;
            """
        )
        
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS announcements (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                category TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
        logger.info("Database tables initialized.")


async def get_user(db_pool: asyncpg.Pool, telegram_id: int):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1;",
            telegram_id,
        )


async def register_user(
    db_pool: asyncpg.Pool,
    telegram_id: int,
    username: str | None,
    full_name: str,
    phone: str,
) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO users (telegram_id, username, full_name, phone)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (telegram_id) DO UPDATE
            SET username = $2, full_name = $3, phone = $4;
            """,
            telegram_id,
            username,
            full_name,
            phone,
        )


async def set_user_role(db_pool: asyncpg.Pool, telegram_id: int, role: str) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET role = $1 WHERE telegram_id = $2;",
            role,
            telegram_id,
        )


async def save_announcement(db_pool: asyncpg.Pool, user_id: int, category: str, text: str) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO announcements (user_id, category, text)
            VALUES ($1, $2, $3)
            RETURNING id;
            """,
            user_id,
            category,
            text,
        )
        return row["id"]


async def get_all_drivers(db_pool: asyncpg.Pool) -> list[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT telegram_id FROM users WHERE role = 'driver';"
        )


async def get_all_clients(db_pool: asyncpg.Pool) -> list[asyncpg.Record]:
    async with db_pool.acquire() as conn:
        return await conn.fetch(
            "SELECT telegram_id FROM users WHERE role = 'client';"
        )


# ─── Router / Handlers ──────────────────────────────────────────────────────

router = Router()
# Ensure all message handlers only work in private chat (ignore group messages)
router.message.filter(F.chat.type == "private")


# ── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()

    # Check if user already registered with phone
    user = await get_user(pool, message.from_user.id)
    if user and dict(user).get("phone"):
        # Already registered — go straight to main menu
        kb = driver_menu_kb() if dict(user).get("role") == "driver" else main_menu_kb()
        await message.answer(
            f"Ассалому алайкум {message.from_user.full_name}\n\n"
            "Қуйидаги тугмалардан бирини танланг!",
            reply_markup=kb,
        )
    else:
        # Ask for phone number
        await message.answer(
            "👋 Ассалому алайкум!\n\n"
            "Рўйхатдан ўтиш учун телефон рақамингизни юборинг:",
            reply_markup=phone_kb(),
        )
        await state.set_state(UserStates.waiting_for_phone)


# ── Phone registration ──────────────────────────────────────────────────────

@router.message(UserStates.waiting_for_phone, F.contact)
async def receive_phone_contact(message: Message, state: FSMContext) -> None:
    """User shared contact."""
    phone = message.contact.phone_number
    await register_user(
        pool,
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        phone,
    )
    await state.clear()
    await message.answer(
        f"Рўйхатдан муваффақиятли ўтдингиз! {message.from_user.full_name}\n\n"
        "Танланг:",
        reply_markup=role_select_kb(),
    )


@router.message(UserStates.waiting_for_phone, F.text)
async def receive_phone_text(message: Message, state: FSMContext) -> None:
    """User typed phone number manually."""
    phone = message.text.strip()
    # Basic validation
    digits = phone.replace("+", "").replace(" ", "").replace("-", "")
    if not digits.isdigit() or len(digits) < 9:
        await message.answer(
            "⚠️ Илтимос, тўғри телефон рақам юборинг!\n"
            "Ёки қуйидаги тугмани босинг:",
            reply_markup=phone_kb(),
        )
        return

    await register_user(
        pool,
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
        phone,
    )
    await state.clear()
    await message.answer(
        f"Рўйхатдан муваффақиятли ўтдингиз! {message.from_user.full_name}\n\n"
        "Танланг:",
        reply_markup=role_select_kb(),
    )


# ── Бекор қилиш (cancel) — works from ANY state ─────────────────────────────

@router.message(F.text == "❌ Бекор қилиш")
async def handle_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await get_user(pool, message.from_user.id)
    kb = driver_menu_kb() if user and dict(user).get("role") == "driver" else main_menu_kb()
    await message.answer(
        "❌ Бекор қилинди. Асосий менюга қайтдингиз.",
        reply_markup=kb,
    )


# ── Direction: Тошкентдан - Сурхондарёга ────────────────────────────────────

DIRECTION_MSG = (
    "Илтимос буюртма хақида бироз малумот беринг!\n\n"
    "Мисол учун: Соат 19:00 да Яккасаройдан Сурхондарёга "
    "чиқиб кетишим керак 1 та катта сумкам бор"
)


@router.message(F.text.in_({"🚙 Тошкентдан - Сурхондарёга", "🚙 Сурхондарёдан - Тошкентга"}))
async def handle_direction(message: Message, state: FSMContext) -> None:
    await state.update_data(direction=message.text)
    await message.answer(DIRECTION_MSG, reply_markup=cancel_kb())
    await state.set_state(UserStates.waiting_for_direction)


@router.message(UserStates.waiting_for_direction)
async def receive_direction_text(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.text:
        await message.answer("⚠️ Илтимос, матн юборинг!")
        return

    data = await state.get_data()
    direction = data.get("direction", "Номаълум")

    # Get user phone for the broadcast
    user = await get_user(pool, message.from_user.id)
    phone = user["phone"] if user else "—"

    ann_id = await save_announcement(pool, message.from_user.id, f"direction:{direction}", message.text)

    await message.answer(
        f"✅ Буюртмангиз қабул қилинди! (ID: {ann_id})\n"
        "Барча шофёрларга юборилди.",
        reply_markup=main_menu_kb(),
    )

    await broadcast_to_drivers(
        bot,
        f"📢 Янги буюртма\n"
        f"📍 {direction}\n\n"
        f"👤 {message.from_user.full_name}\n"
        f"📞 {phone}\n\n"
        f"{message.text}",
    )
    await send_order_to_channel(bot, message.from_user.id, f"Yangi zakaz ({direction})", phone, message.text)
    await state.clear()


# ── Почта бор ────────────────────────────────────────────────────────────────

POCHTA_MSG = (
    "Илтимос буюртма хақида бироз малумот беринг!\n\n"
    "Мисол учун: Сурхондарёдан Яккасаройга Битта сумкада кийимлар бор, "
    "Велосипедни олиб кетиш керак, Илтимос фақат томида багажи борлар "
    "алоқага чиқсин"
)


@router.message(F.text == "📦 Почта бор")
async def handle_pochta(message: Message, state: FSMContext) -> None:
    await message.answer(POCHTA_MSG, reply_markup=cancel_kb())
    await state.set_state(UserStates.waiting_for_pochta)


@router.message(UserStates.waiting_for_pochta)
async def receive_pochta_text(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.text:
        await message.answer("⚠️ Илтимос, матн юборинг!")
        return

    user = await get_user(pool, message.from_user.id)
    phone = user["phone"] if user else "—"

    ann_id = await save_announcement(pool, message.from_user.id, "pochta", message.text)

    await message.answer(
        f"✅ Почта буюртмангиз қабул қилинди! (ID: {ann_id})\n"
        "Барча шофёрларга юборилди.",
        reply_markup=main_menu_kb(),
    )

    await broadcast_to_drivers(
        bot,
        f"📦 Янги почта буюртмаси\n\n"
        f"👤 {message.from_user.full_name}\n"
        f"📞 {phone}\n\n"
        f"{message.text}",
    )
    await send_order_to_channel(bot, message.from_user.id, "Yangi zakaz (Pochta)", phone, message.text)
    await state.clear()


# ── Созламалар ───────────────────────────────────────────────────────────────

@router.message(F.text == "⚙️ Созламалар")
async def handle_sozlamalar(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "⚙️ Созламалар — қуйидаги тугмалардан бирини танланг:",
        reply_markup=sozlamalar_kb(),
    )


# ── Клиент ───────────────────────────────────────────────────────────────────

@router.message(F.text == "👤 Клиент")
async def handle_klient(message: Message, state: FSMContext) -> None:
    await set_user_role(pool, message.from_user.id, "client")
    await message.answer(
        "Сиз 👤 Клиент сифатида рўйхатдан ўтдингиз!",
        reply_markup=main_menu_kb(),
    )


# ── Шофёр ────────────────────────────────────────────────────────────────────

@router.message(F.text == "🚕 Шофёр")
async def handle_shofyor(message: Message, state: FSMContext) -> None:
    await set_user_role(pool, message.from_user.id, "driver")
    await message.answer(
        "Сиз 🚕 Шофёр сифатида рўйхатдан ўтдингиз!",
        reply_markup=driver_menu_kb(),
    )


# ── Элон бериш (driver announcement) ────────────────────────────────────────

ELON_BERISH_TEXT = (
    "👋 Ассалому алайкум, Сурхондарё, Денов, Сариосиё, Термиз "
    "(ва бошқа) ТОШКEНТ йўналишидаги энг катта ВИП гуруҳига "
    "азо бўлишингиз мумкин.\n\n"
    "⚠️ Бунинг учун бизнинг Сурхондарё шофёрлар (ВИП) гуруҳимизга "
    "қўшилишингиз керак.\n\n"
    "Гуруҳга қўшилиш учун: @Nematov_Javoxir_manager'га мурожаат қилинг!"
)


@router.message(F.text == "📢 Элон бериш")
async def handle_elon_berish(message: Message, state: FSMContext) -> None:
    await message.answer(ELON_BERISH_TEXT)
    await message.answer(
        "📝 Илтимос, элон матнингизни юборинг:",
        reply_markup=cancel_kb(),
    )
    await state.set_state(UserStates.waiting_for_driver_elon)


@router.message(UserStates.waiting_for_driver_elon)
async def receive_driver_elon(message: Message, state: FSMContext, bot: Bot) -> None:
    if not message.text:
        await message.answer("⚠️ Илтимос, матн юборинг!")
        return

    user = await get_user(pool, message.from_user.id)
    phone = user["phone"] if user else "—"

    ann_id = await save_announcement(pool, message.from_user.id, "driver_elon", message.text)

    await message.answer(
        f"✅ Элонингиз қабул қилинди! (ID: {ann_id})\n"
        "Барча клиентларга юборилди.",
        reply_markup=driver_menu_kb(),
    )

    await broadcast_to_clients(
        bot,
        f"📢 Шофёрдан элон\n\n"
        f"👤 {message.from_user.full_name}\n"
        f"📞 {phone}\n\n"
        f"{message.text}",
    )
    await state.clear()


# ── Broadcast helpers ────────────────────────────────────────────────────────


async def send_order_to_channel(bot: Bot, user_id: int, title: str, phone: str, text: str) -> None:
    if not CHANNEL_ID:
        return
        
    msg_text = (
        f"📦 {title}\n\n"
        f"📞 Mijoz raqami: {phone}\n"
        f"🗣 Mijoz xabari 👇\n"
        f"{text}"
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Mijoz lichkasiga yozish 👤", url=f"tg://user?id={user_id}")]
        ]
    )
    
    try:
        await bot.send_message(chat_id=CHANNEL_ID, text=msg_text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"Could not send to channel {CHANNEL_ID}: {e}")


async def broadcast_to_drivers(bot: Bot, text: str) -> None:
    """Send a message to every registered driver."""
    drivers = await get_all_drivers(pool)
    for driver in drivers:
        try:
            await bot.send_message(chat_id=driver["telegram_id"], text=text)
        except Exception as exc:
            logger.warning("Could not send to driver %s: %s", driver["telegram_id"], exc)


async def broadcast_to_clients(bot: Bot, text: str) -> None:
    """Send a message to every registered client."""
    clients = await get_all_clients(pool)
    for client in clients:
        try:
            await bot.send_message(chat_id=client["telegram_id"], text=text)
        except Exception as exc:
            logger.warning("Could not send to client %s: %s", client["telegram_id"], exc)


# ── Fallback for unknown messages ────────────────────────────────────────────

@router.message()
async def fallback(message: Message) -> None:
    if message.chat.type != "private":
        return
    await message.answer(
        "❓ Тушунарсиз буйруқ. Илтимос, менюдан фойдаланинг.",
        reply_markup=main_menu_kb(),
    )


# ─── Main entry point ───────────────────────────────────────────────────────


async def main() -> None:
    global pool

    # Create DB pool and initialize tables
    pool = await create_pool()
    await init_db(pool)

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Bot is starting …")
    try:
        await dp.start_polling(bot)
    finally:
        await pool.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
