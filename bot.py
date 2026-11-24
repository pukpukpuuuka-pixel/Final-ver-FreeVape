from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import re
import time

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.types import (
    InlineKeyboardButton,
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    User,
    WebAppInfo,
)

from api import create_app, db

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Админы бота (числовые Telegram ID). Дополнительно можно задать в Railway: ADMIN_ID / ADMIN_IDS (через запятую)
ADMIN_IDS = {7944549421, 8839872292, 6739188141}
for _x in [os.getenv("ADMIN_ID", "")] + os.getenv("ADMIN_IDS", "").split(","):
    if _x.strip().isdigit():
        ADMIN_IDS.add(int(_x.strip()))
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
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
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
    if user and user.id not in ADMIN_IDS:
        if is_banned(user.id):
            return None
        save_user(user)
    return await handler(event, data)


def admin_only(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


DEFAULT_WELCOME = (
    "👋 Добро пожаловать в FreeVape!\n\n"
    "Здесь можно посмотреть наш ассортимент и оформить заказ.\n"
    "Нажми кнопку ниже, чтобы открыть каталог.\n\n"
    "🔞 Только для лиц старше 18 лет.\n"
    "Все команды — /help"
)


def get_welcome() -> str:
    return get_setting("welcome", DEFAULT_WELCOME)


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
                            await notify_admins(
                                bot, f"🎯 Пользователь ID: {referrer} привёл {REF_GOAL} друзей."
                            )
                    except Exception:
                        pass
    await message.answer(get_welcome(), reply_markup=catalog_keyboard())


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


@dp.message(Command("myid"))
async def cmd_myid(message: Message):
    await message.answer(f"Твой ID: {message.from_user.id}")


# ======================= АДМИН-ПАНЕЛЬ =======================
class Adm(StatesGroup):
    broadcast = State()
    broadcast_confirm = State()
    find_user = State()
    ban = State()
    unban = State()
    promo_new = State()
    promo_del = State()
    write_id = State()
    write_text = State()
    welcome = State()


IS_ADMIN = F.from_user.id.in_(ADMIN_IDS)
BROADCAST_RUNNING = False

PANEL_TEXT = "🛠 Админ-панель FreeVape\n\nВыбери действие:"

COMMANDS_HELP = (
    "📖 Команды админа:\n\n"
    "/admin — открыть панель\n"
    "/cancel — отменить текущее действие\n"
    "/stats — статистика\n"
    "/user ID — карточка пользователя\n"
    "/reply ID текст — написать клиенту\n"
    "/export — список пользователей (CSV)\n"
    "/promo_new КОД % ЛИМИТ — создать промокод\n"
    "/promo_del КОД — удалить промокод\n"
    "/ban ID, /unban ID — блокировка\n\n"
    "Ответить клиенту можно и реплаем на его сообщение в чате с ботом."
)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📊 Статистика", "adm:stats"), btn("📈 Рост за 7 дней", "adm:growth")],
            [btn("📣 Рассылка (реклама)", "adm:bc"), btn("✏️ Приветствие", "adm:welcome")],
            [btn("👥 Пользователи", "adm:users"), btn("🔍 Найти", "adm:find")],
            [btn("🎟 Создать промокод", "adm:promo_new"), btn("🗑 Удалить промокод", "adm:promo_del")],
            [btn("🚫 Бан", "adm:ban"), btn("✅ Разбан", "adm:unban")],
            [btn("✉️ Написать клиенту", "adm:write"), btn("🏆 Топ рефералов", "adm:top")],
            [btn("📤 Экспорт CSV", "adm:export"), btn("📖 Команды", "adm:help")],
        ]
    )


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("⬅️ В панель", "adm:home")]])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Отмена", "adm:cancel")]])


def bc_kb(with_btn: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Отправить всем", "adm:bcgo")],
            [btn(f"🛍 Кнопка «Каталог» под постом: {'вкл' if with_btn else 'выкл'}", "adm:bckb")],
            [btn("❌ Отмена", "adm:cancel")],
        ]
    )


def not_cmd(m: Message) -> bool:
    return not (m.text or "").startswith("/")


async def safe_edit(msg: Message, text: str, kb: InlineKeyboardMarkup | None = None):
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await msg.answer(text, reply_markup=kb)


async def notify_admins(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


def get_setting(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str | None):
    with db() as c:
        if value is None:
            c.execute("DELETE FROM settings WHERE key=?", (key,))
        else:
            c.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def resolve_user(arg: str) -> int | None:
    arg = (arg or "").strip()
    if arg.isdigit():
        return int(arg)
    name = arg.lstrip("@").lower()
    if not name:
        return None
    with db() as c:
        row = c.execute("SELECT user_id FROM users WHERE lower(username)=?", (name,)).fetchone()
    return row["user_id"] if row else None


def stats_text() -> str:
    now = int(time.time())
    with db() as c:
        one = lambda q, *a: c.execute(q, a).fetchone()[0]
        return (
            "📊 Статистика\n\n"
            f"👥 Пользователей: {one('SELECT COUNT(*) FROM users')}\n"
            f"🆕 За 24 часа: {one('SELECT COUNT(*) FROM users WHERE joined>?', now - 86400)}\n"
            f"🗓 За 7 дней: {one('SELECT COUNT(*) FROM users WHERE joined>?', now - 7 * 86400)}\n"
            f"🚫 В бане: {one('SELECT COUNT(*) FROM users WHERE banned=1')}\n"
            f"🤝 Приглашений по рефералке: {one('SELECT COUNT(*) FROM referrals')}\n"
            f"🛍 Товаров в каталоге: {one('SELECT COUNT(*) FROM products')}\n"
            f"🎟 Промокодов: {one('SELECT COUNT(*) FROM promos')}, "
            f"использований: {one('SELECT COALESCE(SUM(used),0) FROM promos')}"
        )


def growth_text() -> str:
    now = int(time.time())
    with db() as c:
        rows = c.execute(
            "SELECT date(joined+10800,'unixepoch') d, COUNT(*) n FROM users "
            "WHERE joined>? GROUP BY d",
            (now - 8 * 86400,),
        ).fetchall()
    counts = {r["d"]: r["n"] for r in rows}
    lines = []
    for i in range(6, -1, -1):
        d = time.strftime("%Y-%m-%d", time.gmtime(now + 10800 - i * 86400))
        n = counts.get(d, 0)
        lines.append(f"{d[5:]}  {'█' * min(n, 20)} {n}")
    return "📈 Новые пользователи за 7 дней (МСК):\n\n" + "\n".join(lines)


def users_text() -> str:
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = c.execute(
            "SELECT user_id, username, full_name, banned FROM users ORDER BY joined DESC LIMIT 10"
        ).fetchall()
    lines = [
        f"{'🚫' if r['banned'] else '•'} {r['full_name'] or '—'} "
        f"({'@' + r['username'] if r['username'] else 'нет username'}) — {r['user_id']}"
        for r in rows
    ]
    return f"👥 Всего: {total}\n\nПоследние 10:\n" + ("\n".join(lines) if lines else "пока никого")


def top_text() -> str:
    with db() as c:
        rows = c.execute(
            "SELECT r.referrer_id, COUNT(*) n, u.username, u.full_name FROM referrals r "
            "LEFT JOIN users u ON u.user_id=r.referrer_id "
            "GROUP BY r.referrer_id ORDER BY n DESC LIMIT 10"
        ).fetchall()
    if not rows:
        return "🏆 Рефералов пока нет."
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, r in enumerate(rows):
        name = f"@{r['username']}" if r["username"] else (r["full_name"] or "—")
        lines.append(f"{medals[i] if i < 3 else str(i + 1) + '.'} {name} (ID: {r['referrer_id']}) — {r['n']}")
    return "🏆 Топ по приглашениям:\n\n" + "\n".join(lines)


def user_card(uid: int):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if not u:
            return None
        invited = c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        by = c.execute("SELECT referrer_id FROM referrals WHERE user_id=?", (uid,)).fetchone()
        promo = c.execute(
            "SELECT r.code, p.percent FROM redemptions r JOIN promos p ON p.code=r.code "
            "WHERE r.user_id=? ORDER BY r.at DESC LIMIT 1",
            (uid,),
        ).fetchone()
    joined = time.strftime("%Y-%m-%d %H:%M", time.gmtime(u["joined"] + 3 * 3600))
    text = (
        f"👤 {u['full_name'] or '—'} (@{u['username'] or 'нет'})\n"
        f"ID: {uid}\n"
        f"Первый раз в боте: {joined} (МСК)\n"
        f"Статус: {'🚫 в бане' if u['banned'] else 'активен'}\n"
        f"Пригласил друзей: {invited}\n"
        f"Пришёл от: {by['referrer_id'] if by else '—'}\n"
        f"Промокод: {promo['code'] + ' (' + str(promo['percent']) + '%)' if promo else '—'}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                btn("✅ Разбанить" if u["banned"] else "🚫 Забанить",
                    f"adm:{'unban2' if u['banned'] else 'ban2'}:{uid}"),
                btn("✉️ Написать", f"adm:wr:{uid}"),
            ],
            [btn("⬅️ В панель", "adm:home")],
        ]
    )
    return text, kb


def set_ban(uid: int, banned: bool):
    with db() as c:
        c.execute(
            "INSERT INTO users(user_id,joined,banned) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET banned=excluded.banned",
            (uid, int(time.time()), 1 if banned else 0),
        )


def create_promo(text: str) -> str:
    parts = (text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return "❌ Формат: КОД ПРОЦЕНТ ЛИМИТ\nПример: SALE15 15 100"
    code = parts[0].upper()
    percent, max_uses = int(parts[1]), int(parts[2])
    if not re.fullmatch(r"[A-ZА-ЯЁ0-9_-]{3,24}", code) or not 1 <= percent <= 100 or max_uses < 1:
        return "❌ Код: 3–24 символа (буквы, цифры, _ -), процент 1–100, лимит ≥ 1."
    with db() as c:
        try:
            c.execute(
                "INSERT INTO promos(code,percent,max_uses,used) VALUES(?,?,?,0)",
                (code, percent, max_uses),
            )
        except Exception:
            return "❌ Такой промокод уже есть."
    return f"✅ Промокод {code}: {percent}%, лимит {max_uses}"


def delete_promo(code: str) -> str:
    code = (code or "").strip().upper()
    if not code:
        return "❌ Укажи код."
    with db() as c:
        c.execute("DELETE FROM redemptions WHERE code=?", (code,))
        cur = c.execute("DELETE FROM promos WHERE code=?", (code,))
    return f"🗑 Промокод {code} удалён" if cur.rowcount else "❌ Такого промокода нет."


# --- Команды админа ---
@dp.message(Command("admin", "panel"), admin_only)
async def cmd_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(PANEL_TEXT, reply_markup=panel_kb())


@dp.message(Command("cancel"), admin_only)
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=panel_kb())


@dp.message(Command("stats"), admin_only)
async def cmd_stats(message: Message):
    await message.answer(stats_text())


@dp.message(Command("promo_new"), admin_only)
async def cmd_promo_new(message: Message, command: CommandObject):
    await message.answer(create_promo(command.args or ""))


@dp.message(Command("promo_del"), admin_only)
async def cmd_promo_del(message: Message, command: CommandObject):
    await message.answer(delete_promo(command.args or ""))


@dp.message(Command("ban"), admin_only)
async def cmd_ban(message: Message, command: CommandObject):
    uid = resolve_user(command.args or "")
    if not uid:
        await message.answer("Формат: /ban ID")
        return
    set_ban(uid, True)
    await message.answer(f"🚫 {uid} заблокирован")


@dp.message(Command("unban"), admin_only)
async def cmd_unban(message: Message, command: CommandObject):
    uid = resolve_user(command.args or "")
    if not uid:
        await message.answer("Формат: /unban ID")
        return
    set_ban(uid, False)
    await message.answer(f"✅ {uid} разблокирован")


@dp.message(Command("reply"), admin_only)
async def cmd_reply(message: Message, command: CommandObject, bot: Bot):
    parts = (command.args or "").split(maxsplit=1)
    uid = resolve_user(parts[0]) if parts else None
    if len(parts) != 2 or not uid:
        await message.answer("Формат: /reply ID текст")
        return
    try:
        await bot.send_message(uid, f"💬 Поддержка FreeVape:\n\n{parts[1]}")
        await message.answer("✅ Отправлено")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")


@dp.message(Command("user"), admin_only)
async def cmd_user(message: Message, command: CommandObject):
    uid = resolve_user(command.args or "")
    card = user_card(uid) if uid else None
    if not card:
        await message.answer("Пользователь не найден. Формат: /user ID")
        return
    await message.answer(card[0], reply_markup=card[1])


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
    await message.answer_document(
        BufferedInputFile(data, filename="users.csv"), caption=f"Пользователей: {len(rows)}"
    )


# --- Рассылка ---
async def run_broadcast(bot: Bot, report_to: int, from_chat: int, msg_id: int, with_btn: bool):
    global BROADCAST_RUNNING
    BROADCAST_RUNNING = True
    kb = catalog_keyboard() if with_btn else None
    ok = fail = 0
    try:
        with db() as c:
            ids = [r["user_id"] for r in c.execute("SELECT user_id FROM users WHERE banned=0")]
        for uid in ids:
            if uid in ADMIN_IDS:
                continue
            for attempt in range(2):
                try:
                    await bot.copy_message(uid, from_chat, msg_id, reply_markup=kb)
                    ok += 1
                    break
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    if attempt == 1:
                        fail += 1
                except Exception:
                    fail += 1  # заблокировал бота или удалил чат
                    break
            await asyncio.sleep(0.06)
    finally:
        BROADCAST_RUNNING = False
    await bot.send_message(
        report_to, f"📣 Рассылка завершена.\n✅ Доставлено: {ok}\n❌ Не доставлено: {fail}"
    )


# --- Нажатия кнопок панели ---
@dp.callback_query(F.data.startswith("adm:"), IS_ADMIN)
async def adm_callback(cb: CallbackQuery, state: FSMContext, bot: Bot):
    parts = cb.data.split(":")
    act = parts[1]
    msg = cb.message
    await cb.answer()

    if act in ("home", "cancel"):
        await state.clear()
        await safe_edit(msg, PANEL_TEXT, panel_kb())
    elif act == "stats":
        await safe_edit(msg, stats_text(), back_kb())
    elif act == "growth":
        await safe_edit(msg, growth_text(), back_kb())
    elif act == "users":
        await safe_edit(msg, users_text(), back_kb())
    elif act == "top":
        await safe_edit(msg, top_text(), back_kb())
    elif act == "help":
        await safe_edit(msg, COMMANDS_HELP, back_kb())
    elif act == "export":
        await cmd_export(msg)
    elif act == "find":
        await state.set_state(Adm.find_user)
        await safe_edit(msg, "🔍 Отправь ID или @username пользователя:", cancel_kb())
    elif act == "ban":
        await state.set_state(Adm.ban)
        await safe_edit(msg, "🚫 Отправь ID или @username, кого заблокировать:", cancel_kb())
    elif act == "unban":
        await state.set_state(Adm.unban)
        await safe_edit(msg, "✅ Отправь ID или @username, кого разблокировать:", cancel_kb())
    elif act == "promo_new":
        await state.set_state(Adm.promo_new)
        await safe_edit(msg, "🎟 Отправь: КОД ПРОЦЕНТ ЛИМИТ\nПример: SALE15 15 100", cancel_kb())
    elif act == "promo_del":
        await state.set_state(Adm.promo_del)
        await safe_edit(msg, "🗑 Отправь код промокода, который удалить:", cancel_kb())
    elif act == "write":
        await state.set_state(Adm.write_id)
        await safe_edit(msg, "✉️ Кому написать? Отправь ID или @username:", cancel_kb())
    elif act == "wr" and len(parts) > 2:
        await state.set_state(Adm.write_text)
        await state.update_data(target=int(parts[2]))
        await safe_edit(msg, f"✉️ Напиши текст сообщения для {parts[2]}:", cancel_kb())
    elif act in ("ban2", "unban2") and len(parts) > 2:
        uid = int(parts[2])
        set_ban(uid, act == "ban2")
        card = user_card(uid)
        if card:
            await safe_edit(msg, card[0], card[1])
    elif act == "welcome":
        await state.set_state(Adm.welcome)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [btn("↩️ Вернуть стандартное", "adm:wreset")],
                [btn("❌ Отмена", "adm:cancel")],
            ]
        )
        await safe_edit(
            msg,
            "✏️ Текущее приветствие (/start):\n\n"
            f"{get_welcome()}\n\n"
            "Отправь новый текст, и он заменит это приветствие.",
            kb,
        )
    elif act == "wreset":
        set_setting("welcome", None)
        await state.clear()
        await safe_edit(msg, "✅ Приветствие сброшено на стандартное.", back_kb())
    elif act == "bc":
        if BROADCAST_RUNNING:
            await safe_edit(msg, "⏳ Рассылка уже идёт, дождись окончания.", back_kb())
            return
        await state.set_state(Adm.broadcast)
        await safe_edit(
            msg,
            "📣 Рассылка\n\n"
            "Отправь сообщение, которое получат все пользователи бота: текст, фото, видео "
            "или любой пост (можно с подписью). Перед отправкой покажу предпросмотр.",
            cancel_kb(),
        )
    elif act == "bckb":
        data = await state.get_data()
        if "bc_msg" not in data:
            await safe_edit(msg, "Сессия истекла. Начни рассылку заново.", back_kb())
            return
        flag = not data.get("bc_btn", False)
        await state.update_data(bc_btn=flag)
        await msg.edit_reply_markup(reply_markup=bc_kb(flag))
    elif act == "bcgo":
        data = await state.get_data()
        if "bc_msg" not in data:
            await safe_edit(msg, "Сессия истекла. Начни рассылку заново.", back_kb())
            return
        if BROADCAST_RUNNING:
            await safe_edit(msg, "⏳ Рассылка уже идёт, дождись окончания.", back_kb())
            return
        with db() as c:
            n = c.execute("SELECT COUNT(*) FROM users WHERE banned=0").fetchone()[0]
        await state.clear()
        await safe_edit(msg, f"🚀 Рассылка запущена на {n} пользователей. Пришлю отчёт, когда закончу.", back_kb())
        asyncio.create_task(
            run_broadcast(bot, cb.from_user.id, data["bc_chat"], data["bc_msg"], data.get("bc_btn", False))
        )


# --- Ввод данных в состояниях ---
@dp.message(Adm.broadcast, IS_ADMIN, not_cmd)
async def st_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.update_data(bc_chat=message.chat.id, bc_msg=message.message_id, bc_btn=False)
    await state.set_state(Adm.broadcast_confirm)
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM users WHERE banned=0").fetchone()[0]
    await message.answer("👆 Так увидят пользователи (предпросмотр ниже).")
    try:
        await bot.copy_message(message.chat.id, message.chat.id, message.message_id)
    except Exception as e:
        await message.answer(f"❌ Этот тип сообщения не получится разослать: {e}", reply_markup=back_kb())
        await state.clear()
        return
    await message.answer(f"Отправить {n} пользователям?", reply_markup=bc_kb(False))


@dp.message(Adm.find_user, IS_ADMIN, F.text, not_cmd)
async def st_find(message: Message, state: FSMContext):
    uid = resolve_user(message.text)
    card = user_card(uid) if uid else None
    if not card:
        await message.answer("Не нашёл такого пользователя. Попробуй ещё раз или нажми «Отмена».", reply_markup=cancel_kb())
        return
    await state.clear()
    await message.answer(card[0], reply_markup=card[1])


@dp.message(Adm.ban, IS_ADMIN, F.text, not_cmd)
async def st_ban(message: Message, state: FSMContext):
    uid = resolve_user(message.text)
    if not uid:
        await message.answer("Не нашёл. Отправь числовой ID или @username.", reply_markup=cancel_kb())
        return
    set_ban(uid, True)
    await state.clear()
    await message.answer(f"🚫 {uid} заблокирован", reply_markup=back_kb())


@dp.message(Adm.unban, IS_ADMIN, F.text, not_cmd)
async def st_unban(message: Message, state: FSMContext):
    uid = resolve_user(message.text)
    if not uid:
        await message.answer("Не нашёл. Отправь числовой ID или @username.", reply_markup=cancel_kb())
        return
    set_ban(uid, False)
    await state.clear()
    await message.answer(f"✅ {uid} разблокирован", reply_markup=back_kb())


@dp.message(Adm.promo_new, IS_ADMIN, F.text, not_cmd)
async def st_promo_new(message: Message, state: FSMContext):
    result = create_promo(message.text)
    if result.startswith("✅"):
        await state.clear()
        await message.answer(result, reply_markup=back_kb())
    else:
        await message.answer(result, reply_markup=cancel_kb())


@dp.message(Adm.promo_del, IS_ADMIN, F.text, not_cmd)
async def st_promo_del(message: Message, state: FSMContext):
    result = delete_promo(message.text)
    if result.startswith("🗑"):
        await state.clear()
        await message.answer(result, reply_markup=back_kb())
    else:
        await message.answer(result, reply_markup=cancel_kb())


@dp.message(Adm.write_id, IS_ADMIN, F.text, not_cmd)
async def st_write_id(message: Message, state: FSMContext):
    uid = resolve_user(message.text)
    if not uid:
        await message.answer("Не нашёл. Отправь числовой ID или @username.", reply_markup=cancel_kb())
        return
    await state.update_data(target=uid)
    await state.set_state(Adm.write_text)
    await message.answer(f"Напиши текст сообщения для {uid}:", reply_markup=cancel_kb())


@dp.message(Adm.write_text, IS_ADMIN, F.text, not_cmd)
async def st_write_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    uid = data.get("target")
    await state.clear()
    try:
        await bot.send_message(uid, f"💬 Поддержка FreeVape:\n\n{message.text}")
        await message.answer("✅ Отправлено", reply_markup=back_kb())
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}", reply_markup=back_kb())


@dp.message(Adm.welcome, IS_ADMIN, F.text, not_cmd)
async def st_welcome(message: Message, state: FSMContext):
    set_setting("welcome", message.text)
    await state.clear()
    await message.answer("✅ Приветствие обновлено. Проверь командой /start.", reply_markup=back_kb())


# --- Ответ админа клиенту (reply на сообщение от бота) ---
@dp.message(IS_ADMIN, F.reply_to_message, F.text)
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
        "/ref — реферальная ссылка\n"
        "/contacts — контакты и важные ссылки\n"
        "/myid — мой ID",
        reply_markup=catalog_keyboard(),
    )


# --- Любое текстовое сообщение клиента -> админу ---
@dp.message(F.text, ~F.text.startswith("/"), F.from_user.id.not_in(ADMIN_IDS))
async def to_support(message: Message, bot: Bot):
    user = message.from_user
    username = f"@{user.username}" if user.username else "нет username"
    await notify_admins(
        bot,
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
        
