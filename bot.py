import asyncio
import csv
import io
import logging
import os
import re
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import (
    InlineKeyboardButton,
    BufferedInputFile,
    InlineKeyboardMarkup,
    Message,
    User,
    WebAppInfo,
)

from api import create_app, db

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])  # твой числовой Telegram ID
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://mellifluous-pika-bf5e4c.netlify.app")

WEBHOOK_PATH = "/tg-webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "freevape-secret-123")
PUBLIC_URL = os.getenv("PUBLIC_URL") or f"https://{os.getenv('RAILWAY_PUBLIC_DOMAIN', 'example.com')}"
# 1 = бот сам ставит webhook при старте (в фоне), 0 = webhook поставлен вручную
AUTO_SET_WEBHOOK = os.getenv("AUTO_SET_WEBHOOK", "0") == "1"

BOT_USERNAME = os.getenv("BOT_USERNAME", "")
ADMIN_USERNAME = "@FreeVape_admin"
REF_GOAL = 5  # сколько друзей нужно привести

# Ссылки для /contacts. Пустая ссылка = кнопка не показывается.
LINKS = [
    ("🚚 Доставка", "https://t.me/Freevapessilki/3"),
    ("👑 Условия сотрудничества", "https://t.me/c/4314166384/4"),
    ("🤝 Реферальная программа", "https://t.me/c/4314166384/5"),
    ("💬 Выплата за комментарии в TikTok", "https://t.me/c/4314166384/6"),
    ("🔎 Хештеги", "https://t.me/c/4314166384/7"),
    ("🤵‍♂️ Команда FreeVape", "https://t.me/FreeVape1/33"),
    ("💳 Выплаты", "https://t.me/c/4314166384/9"),
    ("📺 FreeVape — MEDIA", "https://t.me/+vwtIH_EcMzFiMmUx"),
]

dp = Dispatcher()


def init_bot_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                joined INTEGER NOT NULL,
                banned INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS referrals(
                user_id INTEGER PRIMARY KEY,
                referrer_id INTEGER NOT NULL,
                at INTEGER NOT NULL
            );
            """
        )


def save_user(user: User) -> bool:
    """Сохраняет пользователя. True — если он новый."""
    with db() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO users(user_id,username,full_name,joined) VALUES(?,?,?,?)",
            (user.id, user.username, user.full_name, int(time.time())),
        )
        if cur.rowcount == 0:
            c.execute(
                "UPDATE users SET username=?, full_name=? WHERE user_id=?",
                (user.username, user.full_name, user.id),
            )
        return cur.rowcount == 1


def is_banned(user_id: int) -> bool:
    with db() as c:
        row = c.execute("SELECT banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["banned"])


@dp.message.outer_middleware()
async def ban_filter(handler, event: Message, data):
    user = event.from_user
    if user and user.id != ADMIN_ID:
        if is_banned(user.id):
            return None
        save_user(user)
    return await handler(event, data)


def admin_only(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


def catalog_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛍 Открыть каталог", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]
    )


@dp.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, bot: Bot):
    user = message.from_user
    arg = (command.args or "").strip()
    if arg.startswith("ref_") and arg[4:].isdigit():
        referrer = int(arg[4:])
        if referrer != user.id:
            with db() as c:
                known = c.execute("SELECT 1 FROM referrals WHERE user_id=?", (user.id,)).fetchone()
                ref_exists = c.execute("SELECT 1 FROM users WHERE user_id=?", (referrer,)).fetchone()
                # засчитываем только тех, кто пришёл впервые (joined только что)
                just_joined = c.execute(
                    "SELECT joined FROM users WHERE user_id=?", (user.id,)
                ).fetchone()
                if not known and ref_exists and just_joined and time.time() - just_joined["joined"] < 120:
                    c.execute(
                        "INSERT INTO referrals(user_id,referrer_id,at) VALUES(?,?,?)",
                        (user.id, referrer, int(time.time())),
                    )
                    total = c.execute(
                        "SELECT COUNT(*) n FROM referrals WHERE referrer_id=?", (referrer,)
                    ).fetchone()["n"]
                    try:
                        await bot.send_message(
                            referrer, f"🎉 По твоей ссылке пришёл друг! Приглашено: {total}/{REF_GOAL}"
                        )
                        if total == REF_GOAL:
                            await bot.send_message(
                                referrer,
                                f"🔥 Цель выполнена! Напиши {ADMIN_USERNAME}, чтобы получить скидку 15%.",
                            )
                            await bot.send_message(
                                ADMIN_ID, f"🎯 Пользователь ID: {referrer} привёл {REF_GOAL} друзей."
                            )
                    except Exception:
                        pass
    await message.answer(
        "👋 Добро пожаловать в FreeVape!\n\n"
        "Здесь можно посмотреть наш ассортимент и оформить заказ.\n"
        "Нажми кнопку ниже, чтобы открыть каталог.\n\n"
        "🔞 Только для лиц старше 18 лет.\n"
        "Все команды — /help",
        reply_markup=catalog_keyboard(),
    )


@dp.message(Command("catalog"))
async def cmd_catalog(message: Message):
    await message.answer("🛍 Каталог FreeVape:", reply_markup=catalog_keyboard())


@dp.message(Command("order"))
async def cmd_order(message: Message):
    await message.answer(
        "🛒 Как заказать:\n\n"
        "1. Открой каталог и выбери товар.\n"
        f"2. Напиши {ADMIN_USERNAME} или прямо сюда в бот.\n"
        "3. Мы уточним детали и доставку.\n\n"
        "Не нашёл нужное — напиши, у нас есть не всё в каталоге.",
        reply_markup=catalog_keyboard(),
    )


@dp.message(Command("contacts"))
async def cmd_contacts(message: Message):
    rows = [[InlineKeyboardButton(text=t, url=u)] for t, u in LINKS if u]
    rows.append([InlineKeyboardButton(text="✉️ Написать админу", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}")])
    await message.answer(
        "❗ ВАЖНО ❗\n"
        f"Если не нашли то, что нужно, обязательно напишите админу ({ADMIN_USERNAME}) "
        "и уточните. С шансом 95% у нас это будет (мы не всё выкладываем).\n\n"
        "❗ ВАЖНЫЕ ССЫЛКИ ❗\n\n"
        f"📩 Как начать — пиши в личку {ADMIN_USERNAME}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@dp.message(Command("promo"))
async def cmd_promo(message: Message):
    with db() as c:
        row = c.execute(
            "SELECT r.code, p.percent FROM redemptions r JOIN promos p ON p.code=r.code "
            "WHERE r.user_id=? ORDER BY r.at DESC, r.rowid DESC LIMIT 1",
            (message.from_user.id,),
        ).fetchone()
    if row:
        await message.answer(f"🎟 Твой промокод: {row['code']} — скидка {row['percent']}%")
    else:
        await message.answer(
            "У тебя пока нет активного промокода.\nВведи его в каталоге, в разделе промокодов.",
            reply_markup=catalog_keyboard(),
        )


@dp.message(Command("ref"))
async def cmd_ref(message: Message):
    with db() as c:
        n = c.execute(
            "SELECT COUNT(*) n FROM referrals WHERE referrer_id=?", (message.from_user.id,)
        ).fetchone()["n"]
    if not BOT_USERNAME:
        await message.answer("Реферальная ссылка пока недоступна. Напиши админу.")
        return
    link = f"https://t.me/{BOT_USERNAME}?start=ref_{message.from_user.id}"
    await message.answer(
        "🤝 Реферальная программа\n\n"
        f"Приведи {REF_GOAL} друзей — получи скидку 15% на первую покупку.\n\n"
        f"Твоя ссылка:\n{link}\n\n"
        f"Приглашено: {n}/{REF_GOAL}"
    )


# --- Админ-команды ---
@dp.message(Command("stats"), admin_only)
async def cmd_stats(message: Message):
    day = int(time.time()) - 86400
    with db() as c:
        one = lambda q, *a: c.execute(q, a).fetchone()[0]
        text = (
            "📊 Статистика\n\n"
            f"👥 Пользователей: {one('SELECT COUNT(*) FROM users')}\n"
            f"🆕 За 24 часа: {one('SELECT COUNT(*) FROM users WHERE joined>?', day)}\n"
            f"🚫 В бане: {one('SELECT COUNT(*) FROM users WHERE banned=1')}\n"
            f"🤝 Приглашений по рефералке: {one('SELECT COUNT(*) FROM referrals')}\n"
            f"🛍 Товаров в каталоге: {one('SELECT COUNT(*) FROM products')}\n"
            f"🎟 Промокодов: {one('SELECT COUNT(*) FROM promos')}, "
            f"использований: {one('SELECT COALESCE(SUM(used),0) FROM promos')}"
        )
    await message.answer(text)


@dp.message(Command("promo_new"), admin_only)
async def cmd_promo_new(message: Message, command: CommandObject):
    parts = (command.args or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Формат: /promo_new КОД ПРОЦЕНТ ЛИМИТ\nПример: /promo_new SALE15 15 100")
        return
    code = parts[0].upper()
    percent, max_uses = int(parts[1]), int(parts[2])
    if not re.fullmatch(r"[A-ZА-ЯЁ0-9_-]{3,24}", code) or not 1 <= percent <= 100 or max_uses < 1:
        await message.answer("Код: 3–24 символа (буквы, цифры, _ -), процент 1–100, лимит ≥ 1.")
        return
    with db() as c:
        try:
            c.execute(
                "INSERT INTO promos(code,percent,max_uses,used) VALUES(?,?,?,0)",
                (code, percent, max_uses),
            )
        except Exception:
            await message.answer("Такой промокод уже есть.")
            return
    await message.answer(f"✅ Промокод {code}: {percent}%, лимит {max_uses}")


@dp.message(Command("ban"), admin_only)
async def cmd_ban(message: Message, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        await message.answer("Формат: /ban ID")
        return
    uid = int(command.args.strip())
    with db() as c:
        c.execute(
            "INSERT INTO users(user_id,joined,banned) VALUES(?,?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET banned=1",
            (uid, int(time.time())),
        )
    await message.answer(f"🚫 {uid} заблокирован")


@dp.message(Command("unban"), admin_only)
async def cmd_unban(message: Message, command: CommandObject):
    if not (command.args or "").strip().isdigit():
        await message.answer("Формат: /unban ID")
        return
    with db() as c:
        c.execute("UPDATE users SET banned=0 WHERE user_id=?", (int(command.args.strip()),))
    await message.answer(f"✅ {command.args.strip()} разблокирован")


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой ID: {message.from_user.id}")


@dp.message(Command("top"))
async def cmd_top(message: Message):
    with db() as c:
        rows = c.execute(
            "SELECT r.referrer_id, COUNT(*) n, u.username, u.full_name FROM referrals r "
            "LEFT JOIN users u ON u.user_id=r.referrer_id "
            "GROUP BY r.referrer_id ORDER BY n DESC LIMIT 10"
        ).fetchall()
    if not rows:
        await message.answer("Рейтинг пока пуст. Стань первым — /ref")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        name = f"@{r['username']}" if r["username"] else (r["full_name"] or "Аноним")
        lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {name} — {r['n']}")
    await message.answer("🏆 Топ по приглашениям:\n\n" + "\n".join(lines))


# --- Дополнительные админ-команды ---
@dp.message(Command("admin"), admin_only)
async def cmd_admin(message: Message):
    await message.answer(
        "🛠 Админ-команды:\n\n"
        "/stats — статистика\n"
        "/broadcast текст — рассылка\n"
        "/reply ID текст — написать клиенту\n"
        "/user ID — информация о пользователе\n"
        "/export — список пользователей (CSV)\n"
        "/promo_new КОД % ЛИМИТ — создать промокод\n"
        "/promo_del КОД — удалить промокод\n"
        "/ban ID, /unban ID — блокировка"
    )


@dp.message(Command("reply"), admin_only)
async def cmd_reply(message: Message, command: CommandObject, bot: Bot):
    parts = (command.args or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[0].isdigit():
        await message.answer("Формат: /reply ID текст")
        return
    try:
        await bot.send_message(int(parts[0]), f"💬 Поддержка FreeVape:\n\n{parts[1]}")
        await message.answer("✅ Отправлено")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")


@dp.message(Command("user"), admin_only)
async def cmd_user(message: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Формат: /user ID")
        return
    uid = int(arg)
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u:
            await message.answer("Пользователь не найден.")
            return
        invited = c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        by = c.execute("SELECT referrer_id FROM referrals WHERE user_id=?", (uid,)).fetchone()
        promo = c.execute(
            "SELECT r.code, p.percent FROM redemptions r JOIN promos p ON p.code=r.code "
            "WHERE r.user_id=? ORDER BY r.at DESC LIMIT 1",
            (uid,),
        ).fetchone()
    joined = time.strftime("%Y-%m-%d %H:%M", time.gmtime(u["joined"] + 3 * 3600))
    await message.answer(
        f"👤 {u['full_name'] or '—'} (@{u['username'] or 'нет'})\n"
        f"ID: {uid}\n"
        f"Первый раз в боте: {joined} (МСК)\n"
        f"Статус: {'🚫 в бане' if u['banned'] else 'активен'}\n"
        f"Пригласил друзей: {invited}\n"
        f"Пришёл от: {by['referrer_id'] if by else '—'}\n"
        f"Промокод: {promo['code'] + ' (' + str(promo['percent']) + '%)' if promo else '—'}"
    )


@dp.message(Command("export"), admin_only)
async def cmd_export(message: Message):
    with db() as c:
        rows = c.execute(
            "SELECT user_id, username, full_name, joined, banned FROM users ORDER BY joined"
        ).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "full_name", "joined_utc", "banned"])
    for r in rows:
        w.writerow([
            r["user_id"], r["username"] or "", r["full_name"] or "",
            time.strftime("%Y-%m-%d %H:%M", time.gmtime(r["joined"])), r["banned"],
        ])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    await message.answer_document(BufferedInputFile(data, filename="users.csv"), caption=f"Пользователей: {len(rows)}")


@dp.message(Command("promo_del"), admin_only)
async def cmd_promo_del(message: Message, command: CommandObject):
    code = (command.args or "").strip().upper()
    if not code:
        await message.answer("Формат: /promo_del КОД")
        return
    with db() as c:
        c.execute("DELETE FROM redemptions WHERE code=?", (code,))
        cur = c.execute("DELETE FROM promos WHERE code=?", (code,))
    await message.answer("🗑 Удалён" if cur.rowcount else "Такого промокода нет.")


async def run_broadcast(bot: Bot, text: str):
    with db() as c:
        ids = [r["user_id"] for r in c.execute("SELECT user_id FROM users WHERE banned=0")]
    ok = fail = 0
    for uid in ids:
        if uid == ADMIN_ID:
            continue
        try:
            await bot.send_message(uid, text)
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.send_message(uid, text)
                ok += 1
            except Exception:
                fail += 1
        except TelegramForbiddenError:
            fail += 1  # пользователь заблокировал бота
        except Exception:
            fail += 1
        await asyncio.sleep(0.06)
    await bot.send_message(ADMIN_ID, f"📣 Рассылка завершена.\n✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")


@dp.message(Command("broadcast"), admin_only)
async def cmd_broadcast(message: Message, command: CommandObject, bot: Bot):
    text = (command.args or "").strip()
    if not text:
        await message.answer("Формат: /broadcast текст рассылки")
        return
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM users WHERE banned=0").fetchone()[0]
    await message.answer(f"📣 Запускаю рассылку на {n} пользователей…")
    asyncio.create_task(run_broadcast(bot, text))


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


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "🆘 Поддержка FreeVape\n\n"
        "Просто напиши свой вопрос прямо сюда — сообщение придёт нам, "
        "и мы ответим в этом же чате.\n\n"
        "🛍 Каталог — по кнопке ниже.\n\n"
        "Команды:\n"
        "/catalog — открыть каталог\n"
        "/order — как заказать\n"
        "/promo — мой промокод\n"
        "/ref — реферальная ссылка\n"
        "/contacts — контакты и важные ссылки\n"
        "/top — рейтинг по приглашениям\n"
        "/myid — мой ID",
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


async def set_webhook_forever(bot: Bot):
    while True:
        try:
            await bot.set_webhook(
                PUBLIC_URL + WEBHOOK_PATH,
                secret_token=WEBHOOK_SECRET,
                drop_pending_updates=True,
            )
            logging.info("Webhook set: %s", PUBLIC_URL + WEBHOOK_PATH)
            return
        except TelegramRetryAfter as e:
            logging.warning("Flood control, жду %s сек", e.retry_after)
            await asyncio.sleep(e.retry_after + 1)
        except Exception:
            logging.exception("set_webhook failed")
            await asyncio.sleep(30)


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

    app = create_app()  # создаёт папку и основные таблицы БД
    init_bot_db()
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(
        app, path=WEBHOOK_PATH
    )
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()

    if AUTO_SET_WEBHOOK:
        asyncio.create_task(set_webhook_forever(bot))

    logging.info("Server started. Webhook path: %s", WEBHOOK_PATH)
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        logging.basicConfig(level=logging.INFO)
        logging.exception("BOT CRASHED")
        raise
        
