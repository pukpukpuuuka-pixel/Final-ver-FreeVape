import asyncio
import logging
import os
import re

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    User,
    WebAppInfo,
)

from api import create_app

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])  # твой числовой Telegram ID
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://mellifluous-pika-bf5e4c.netlify.app")

WEBHOOK_PATH = "/tg-webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "freevape-secret-123")
PUBLIC_URL = os.getenv("PUBLIC_URL") or f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"

dp = Dispatcher()


def catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Открыть каталог", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )


# --- Ответ админа клиенту (reply на сообщение от бота) ---
@dp.message(F.from_user.id == ADMIN_ID, F.reply_to_message, F.text)
async def admin_reply(message: Message, bot: Bot):
    source = message.reply_to_message.text or ""
    match = re.search(r"ID:\s*(\d+)", source)
    if not match:
        await message.answer("Не нашёл ID клиента. Отвечай именно на сообщение от клиента.")
        return
    user_id = int(match.group(1))
    try:
        await bot.send_message(user_id, f"💬 Поддержка FreeVape:\n\n{message.text}")
        await message.answer("✅ Отправлено")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 Добро пожаловать в FreeVape!\n\n"
        "Здесь можно посмотреть наш ассортимент и оформить заказ.\n"
        "Нажми кнопку ниже, чтобы открыть каталог.\n\n"
        "🔞 Только для лиц старше 18 лет.\n"
        "Нужна помощь — /help",
        reply_markup=catalog_keyboard(),
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🆘 Поддержка FreeVape\n\n"
        "Просто напиши свой вопрос прямо сюда — сообщение придёт нам, "
        "и мы ответим в этом же чате.\n\n"
        "🛍 Каталог — по кнопке ниже.",
        reply_markup=catalog_keyboard(),
    )


# --- Любое текстовое сообщение клиента -> админу ---
@dp.message(F.text, ~F.text.startswith("/"), F.from_user.id != ADMIN_ID)
async def to_support(message: Message, bot: Bot):
    user = message.from_user
    username = f"@{user.username}" if user.username else "нет username"
    await bot.send_message(
        ADMIN_ID,
        f"📩 Сообщение от {user.full_name} ({username})\n"
        f"ID: {user.id}\n\n"
        f"{message.text}\n\n"
        "↩️ Ответь на это сообщение (Reply), и ответ уйдёт клиенту.",
    )
    await message.answer("✅ Сообщение отправлено в поддержку. Скоро ответим!")


async def main():
    logging.basicConfig(level=logging.INFO)
    bot = Bot(BOT_TOKEN)
    # Не делаем запрос GetMe при старте: ID бота берём из токена.
    bot._me = User(
        id=int(BOT_TOKEN.split(":")[0]),
        is_bot=True,
        first_name="FreeVape",
        username=os.getenv("BOT_USERNAME") or None,
    )

    app = create_app()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(
        app, path=WEBHOOK_PATH
    )
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

    while True:
        try:
            await bot.set_webhook(
                PUBLIC_URL + WEBHOOK_PATH,
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=True,
            )
            break
        except TelegramRetryAfter as e:
            logging.warning("Flood control, жду %s сек", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)

    logging.info("Webhook set: %s", PUBLIC_URL + WEBHOOK_PATH)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
    
