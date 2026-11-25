import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl, unquote

import aiohttp
from aiohttp import web

BOT_TOKEN = os.environ["BOT_TOKEN"]
# Пароль админ-панели хранится в переменной Railway ADMIN_PASSWORD, а не в коде
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
# База лежит на Railway Volume (путь /data), иначе данные пропадут при деплое
DB_PATH = os.getenv("DB_PATH") or ("/data/freevape.db" if os.path.isdir("/data") else "freevape.db")

WEBAPP_URL = os.getenv("WEBAPP_URL", "https://mellifluous-pika-bf5e4c.netlify.app")

# Админы бота (числовые Telegram ID). Дополнительно: переменные Railway ADMIN_ID / ADMIN_IDS (через запятую)
ADMIN_IDS = {7944549421, 8839872292, 6739188141}
for _x in [os.getenv("ADMIN_ID", "")] + os.getenv("ADMIN_IDS", "").split(","):
    if _x.strip().isdigit():
        ADMIN_IDS.add(int(_x.strip()))

ORDER_STATUSES = {
    "new": "🆕 Новый",
    "accepted": "✅ Принят",
    "shipped": "🚚 Отправлен",
    "done": "✔️ Выполнен",
    "cancelled": "❌ Отменён",
}

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type, X-Admin-Password, X-Telegram-Init-Data",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
}

_fails: dict[str, list[float]] = {}


class ApiError(Exception):
    def __init__(self, msg: str, status: int = 400):
        self.msg = msg
        self.status = status


# ---------- БАЗА ----------
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price INTEGER NOT NULL DEFAULT 0,
                image BLOB,
                image_mime TEXT,
                updated INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS promos(
                code TEXT PRIMARY KEY,
                percent INTEGER NOT NULL,
                max_uses INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS redemptions(
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                at INTEGER NOT NULL,
                PRIMARY KEY(code, user_id)
            );
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                items TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                unpriced INTEGER NOT NULL DEFAULT 0,
                promo_code TEXT,
                percent INTEGER NOT NULL DEFAULT 0,
                comment TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'new',
                created INTEGER NOT NULL,
                updated INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS orders_user ON orders(user_id, created);
            CREATE TABLE IF NOT EXISTS stock_subs(
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                PRIMARY KEY(user_id, product_id)
            );
            """
        )
        cols = [r["name"] for r in c.execute("PRAGMA table_info(products)")]
        if "in_stock" not in cols:
            c.execute("ALTER TABLE products ADD COLUMN in_stock INTEGER NOT NULL DEFAULT 1")
    logging.info("DB: %s", DB_PATH)


# ---------- ПРОВЕРКИ ----------
def client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    return fwd.split(",")[0].strip() if fwd else (request.remote or "?")


def check_admin(request: web.Request):
    ip = client_ip(request)
    now = time.time()
    recent = [t for t in _fails.get(ip, []) if now - t < 600]
    _fails[ip] = recent
    if len(recent) >= 10:
        raise ApiError("Слишком много попыток, подожди 10 минут", 429)
    pw = unquote(request.headers.get("X-Admin-Password", ""))
    ok = bool(ADMIN_PASSWORD) and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode())
    if not ok:
        recent.append(now)
        raise ApiError("Неверный пароль", 401)


def tg_user(request: web.Request) -> dict:
    """Проверяет подпись Telegram и возвращает данные пользователя."""
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    err = ApiError("Открой магазин через Telegram", 401)
    if not init_data:
        raise err
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        raise err
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        raise err
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > 3 * 86400:
            raise ApiError("Сессия устарела, закрой и снова открой магазин", 401)
        user = json.loads(pairs["user"])
        int(user["id"])
        return user
    except (KeyError, ValueError, TypeError):
        raise err


def tg_user_id(request: web.Request) -> int:
    return int(tg_user(request)["id"])


# ---------- TELEGRAM (отправка сообщений с сервера) ----------
_http: aiohttp.ClientSession | None = None


async def tg_call(method: str, payload: dict):
    global _http
    if _http is None or _http.closed:
        _http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    for attempt in range(2):
        try:
            async with _http.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=payload
            ) as r:
                data = await r.json()
            if data.get("ok"):
                return data
            if data.get("error_code") == 429 and attempt == 0:
                wait = data.get("parameters", {}).get("retry_after", 1)
                await asyncio.sleep(min(wait, 10) + 0.5)
                continue
            logging.warning("Telegram %s failed: %s", method, data)
            return None
        except Exception:
            logging.exception("Telegram %s error", method)
            return None
    return None


def money(n: int) -> str:
    return f"{int(n):,}".replace(",", " ") + " ₽"


def order_text(o) -> str:
    items = json.loads(o["items"])
    lines = []
    for i in items:
        if i["price"]:
            lines.append(f"• {i['name']} × {i['qty']} — {money(i['price'] * i['qty'])}")
        else:
            lines.append(f"• {i['name']} × {i['qty']} — цена по запросу")
    total = f"Итого: {money(o['total'])}"
    if o["percent"]:
        total += f" (скидка {o['percent']}%, промокод {o['promo_code']})"
    if o["unpriced"]:
        total += "\n+ товары с ценой по запросу"
    who = f"{o['full_name'] or '—'} ({'@' + o['username'] if o['username'] else 'нет username'})"
    text = (
        f"🛒 Заказ №{o['id']}\n"
        f"👤 {who}\n"
        f"ID: {o['user_id']}\n\n"
        + "\n".join(lines)
        + f"\n\n{total}"
    )
    if o["comment"]:
        text += f"\n\n💬 Комментарий: {o['comment']}"
    text += f"\n\nСтатус: {ORDER_STATUSES.get(o['status'], o['status'])}"
    return text


def order_buttons(oid: int, back: bool = False) -> dict:
    rows = [
        [
            {"text": "✅ Принять", "callback_data": f"ord:set:{oid}:accepted"},
            {"text": "🚚 Отправлен", "callback_data": f"ord:set:{oid}:shipped"},
        ],
        [
            {"text": "✔️ Выполнен", "callback_data": f"ord:set:{oid}:done"},
            {"text": "❌ Отменить", "callback_data": f"ord:set:{oid}:cancelled"},
        ],
    ]
    if back:
        rows.append([{"text": "⬅️ К заказам", "callback_data": "adm:orders"}])
    return {"inline_keyboard": rows}


async def notify_admins_order(oid: int):
    with db() as c:
        o = c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return
    text, kb = order_text(o), order_buttons(oid)
    for aid in ADMIN_IDS:
        await tg_call("sendMessage", {"chat_id": aid, "text": text, "reply_markup": kb})
        await asyncio.sleep(0.05)


async def notify_stock(pid: int, name: str):
    with db() as c:
        ids = [r["user_id"] for r in c.execute("SELECT user_id FROM stock_subs WHERE product_id=?", (pid,))]
        c.execute("DELETE FROM stock_subs WHERE product_id=?", (pid,))
    kb = {"inline_keyboard": [[{"text": "🛍 Открыть каталог", "web_app": {"url": WEBAPP_URL}}]]}
    for uid in ids:
        await tg_call(
            "sendMessage",
            {"chat_id": uid, "text": f"🔔 «{name}» снова в наличии!", "reply_markup": kb},
        )
        await asyncio.sleep(0.06)


async def read_json(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise ApiError("Некорректный запрос")
    if not isinstance(data, dict):
        raise ApiError("Некорректный запрос")
    return data


def decode_image(data_url: str):
    m = re.match(r"^data:(image/(?:jpeg|png|webp));base64,(.+)$", data_url, re.S)
    if not m:
        raise ApiError("Некорректное фото")
    try:
        raw = base64.b64decode(m.group(2))
    except (binascii.Error, ValueError):
        raise ApiError("Некорректное фото")
    if len(raw) > 4 * 1024 * 1024:
        raise ApiError("Фото слишком большое")
    return raw, m.group(1)


# ---------- MIDDLEWARE ----------
@web.middleware
async def mw(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except ApiError as e:
            resp = web.json_response({"error": e.msg}, status=e.status)
        except web.HTTPException as e:
            resp = e
        except Exception:
            logging.exception("API error")
            resp = web.json_response({"error": "Ошибка сервера"}, status=500)
    resp.headers.update(CORS)
    return resp


# ---------- ПУБЛИЧНЫЕ ЭНДПОИНТЫ ----------
async def root(request):
    return web.Response(text="FreeVape API OK")


async def list_products(request):
    with db() as c:
        rows = c.execute(
            "SELECT id,name,category,description,price,updated,in_stock,"
            "image IS NOT NULL AS has_image FROM products ORDER BY id"
        ).fetchall()
    products = [
        {
            "id": r["id"],
            "name": r["name"],
            "category": r["category"],
            "description": r["description"],
            "price": r["price"],
            "in_stock": bool(r["in_stock"]),
            "image": f"/api/products/{r['id']}/image?v={r['updated']}" if r["has_image"] else None,
        }
        for r in rows
    ]
    return web.json_response({"products": products}, headers={"Cache-Control": "no-store"})


async def product_image(request):
    pid = int(request.match_info["id"])
    with db() as c:
        row = c.execute("SELECT image,image_mime FROM products WHERE id=?", (pid,)).fetchone()
    if not row or row["image"] is None:
        raise web.HTTPNotFound()
    return web.Response(
        body=row["image"],
        content_type=row["image_mime"],
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


async def promo_mine(request):
    uid = tg_user_id(request)
    with db() as c:
        row = c.execute(
            "SELECT r.code, p.percent FROM redemptions r JOIN promos p ON p.code=r.code "
            "WHERE r.user_id=? ORDER BY r.at DESC, r.rowid DESC LIMIT 1",
            (uid,),
        ).fetchone()
    return web.json_response({"promo": dict(row) if row else None})


async def promo_redeem(request):
    uid = tg_user_id(request)
    body = await read_json(request)
    code = str(body.get("code", "")).strip().upper()
    if not code:
        raise ApiError("Введи промокод")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            promo = c.execute(
                "SELECT percent,max_uses,used FROM promos WHERE code=?", (code,)
            ).fetchone()
            if not promo:
                raise ApiError("Такого промокода не существует")
            if c.execute(
                "SELECT 1 FROM redemptions WHERE code=? AND user_id=?", (code, uid)
            ).fetchone():
                raise ApiError("Ты уже использовал этот промокод")
            if promo["used"] >= promo["max_uses"]:
                raise ApiError("Промокод закончился")
            c.execute(
                "INSERT INTO redemptions(code,user_id,at) VALUES(?,?,?)",
                (code, uid, int(time.time())),
            )
            c.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    return web.json_response({"promo": {"code": code, "percent": promo["percent"]}})


async def banner(request):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='banner'").fetchone()
    return web.json_response({"text": row["value"] if row else ""}, headers={"Cache-Control": "no-store"})


async def subs_mine(request):
    uid = tg_user_id(request)
    with db() as c:
        rows = c.execute("SELECT product_id FROM stock_subs WHERE user_id=?", (uid,)).fetchall()
    return web.json_response({"ids": [r["product_id"] for r in rows]})


async def product_notify(request):
    """Подписка/отписка «сообщить о поступлении»."""
    uid = tg_user_id(request)
    pid = int(request.match_info["id"])
    with db() as c:
        p = c.execute("SELECT in_stock FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            raise ApiError("Товар не найден", 404)
        has = c.execute(
            "SELECT 1 FROM stock_subs WHERE user_id=? AND product_id=?", (uid, pid)
        ).fetchone()
        if has:
            c.execute("DELETE FROM stock_subs WHERE user_id=? AND product_id=?", (uid, pid))
            return web.json_response({"subscribed": False})
        if p["in_stock"]:
            raise ApiError("Этот товар уже в наличии")
        c.execute("INSERT INTO stock_subs(user_id,product_id) VALUES(?,?)", (uid, pid))
    return web.json_response({"subscribed": True})


async def orders_create(request):
    user = tg_user(request)
    uid = int(user["id"])
    body = await read_json(request)
    raw = body.get("items")
    if not isinstance(raw, list) or not raw or len(raw) > 30:
        raise ApiError("Корзина пуста")
    comment = str(body.get("comment", "")).strip()[:500]

    merged: dict[int, int] = {}
    for it in raw:
        try:
            pid, qty = int(it["id"]), int(it["qty"])
        except (KeyError, TypeError, ValueError):
            raise ApiError("Некорректная корзина")
        if qty < 1 or qty > 20:
            raise ApiError("Количество должно быть от 1 до 20")
        merged[pid] = min(20, merged.get(pid, 0) + qty)

    now = int(time.time())
    with db() as c:
        recent = c.execute(
            "SELECT COUNT(*) FROM orders WHERE user_id=? AND created>?", (uid, now - 3600)
        ).fetchone()[0]
        if recent >= 5:
            raise ApiError("Слишком много заказов за час, попробуй позже", 429)

        promo = c.execute(
            "SELECT r.code, p.percent FROM redemptions r JOIN promos p ON p.code=r.code "
            "WHERE r.user_id=? ORDER BY r.at DESC, r.rowid DESC LIMIT 1",
            (uid,),
        ).fetchone()
        percent = promo["percent"] if promo else 0

        items, total, unpriced = [], 0, 0
        for pid, qty in merged.items():
            p = c.execute("SELECT id,name,price,in_stock FROM products WHERE id=?", (pid,)).fetchone()
            if not p:
                raise ApiError("Один из товаров больше недоступен, обнови каталог")
            if not p["in_stock"]:
                raise ApiError(f"«{p['name']}» нет в наличии")
            items.append({"id": p["id"], "name": p["name"], "price": p["price"], "qty": qty})
            if p["price"]:
                total += (p["price"] * (100 - percent) * 2 + 100) // 200 * qty
            else:
                unpriced = 1

        cur = c.execute(
            "INSERT INTO orders(user_id,username,full_name,items,total,unpriced,promo_code,percent,comment,status,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid,
                user.get("username"),
                " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x) or None,
                json.dumps(items, ensure_ascii=False),
                total,
                unpriced,
                promo["code"] if promo else None,
                percent,
                comment,
                "new",
                now,
                now,
            ),
        )
        oid = cur.lastrowid
    asyncio.create_task(notify_admins_order(oid))
    return web.json_response({"id": oid, "total": total, "unpriced": bool(unpriced)})


async def orders_mine(request):
    uid = tg_user_id(request)
    with db() as c:
        rows = c.execute(
            "SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 20", (uid,)
        ).fetchall()
    out = [
        {
            "id": r["id"],
            "items": json.loads(r["items"]),
            "total": r["total"],
            "unpriced": bool(r["unpriced"]),
            "percent": r["percent"],
            "status": r["status"],
            "status_label": ORDER_STATUSES.get(r["status"], r["status"]),
            "created": r["created"],
        }
        for r in rows
    ]
    return web.json_response({"orders": out}, headers={"Cache-Control": "no-store"})


# ---------- АДМИН ----------
async def admin_login(request):
    check_admin(request)
    return web.json_response({"ok": True})


async def admin_save_product(request):
    check_admin(request)
    body = await read_json(request)
    name = str(body.get("name", "")).strip()[:120]
    if not name:
        raise ApiError("Укажи название")
    category = (str(body.get("category", "")).strip() or "Без категории")[:60]
    description = str(body.get("description", "")).strip()[:1000]
    try:
        price = max(0, int(body.get("price") or 0))
    except (TypeError, ValueError):
        raise ApiError("Некорректная цена")

    in_stock = None
    if "in_stock" in body:
        in_stock = 0 if body["in_stock"] in (0, False, "0", "false") else 1

    set_image = "image" in body
    raw = mime = None
    if set_image and body["image"]:
        raw, mime = decode_image(str(body["image"]))

    pid = body.get("id")
    now = int(time.time())
    back_in_stock = False
    with db() as c:
        if pid:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                raise ApiError("Некорректный товар")
            old = c.execute("SELECT in_stock FROM products WHERE id=?", (pid,)).fetchone()
            if not old:
                raise ApiError("Товар не найден", 404)
            c.execute(
                "UPDATE products SET name=?,category=?,description=?,price=?,updated=? WHERE id=?",
                (name, category, description, price, now, pid),
            )
            if in_stock is not None:
                c.execute("UPDATE products SET in_stock=? WHERE id=?", (in_stock, pid))
                back_in_stock = old["in_stock"] == 0 and in_stock == 1
        else:
            cur = c.execute(
                "INSERT INTO products(name,category,description,price,updated,in_stock) VALUES(?,?,?,?,?,?)",
                (name, category, description, price, now, 1 if in_stock is None else in_stock),
            )
            pid = cur.lastrowid
        if set_image:
            if raw:
                c.execute("UPDATE products SET image=?, image_mime=? WHERE id=?", (raw, mime, pid))
            else:
                c.execute("UPDATE products SET image=NULL, image_mime=NULL WHERE id=?", (pid,))
    if back_in_stock:
        asyncio.create_task(notify_stock(pid, name))
    return web.json_response({"ok": True, "id": pid})


async def admin_delete_product(request):
    check_admin(request)
    pid = int(request.match_info["id"])
    with db() as c:
        c.execute("DELETE FROM products WHERE id=?", (pid,))
        c.execute("DELETE FROM stock_subs WHERE product_id=?", (pid,))
    return web.json_response({"ok": True})


async def admin_list_promos(request):
    check_admin(request)
    with db() as c:
        rows = c.execute(
            "SELECT code,percent,max_uses,used FROM promos ORDER BY rowid DESC"
        ).fetchall()
    return web.json_response({"promos": [dict(r) for r in rows]})


async def admin_create_promo(request):
    check_admin(request)
    body = await read_json(request)
    code = re.sub(r"\s+", "", str(body.get("code", "")).upper())
    if not re.fullmatch(r"[A-ZА-ЯЁ0-9_-]{3,24}", code):
        raise ApiError("Код: 3–24 символа, буквы и цифры")
    try:
        percent = int(body.get("percent"))
        max_uses = int(body.get("max_uses"))
    except (TypeError, ValueError):
        raise ApiError("Некорректные числа")
    if not 1 <= percent <= 90:
        raise ApiError("Скидка должна быть от 1 до 90%")
    if not 1 <= max_uses <= 1_000_000:
        raise ApiError("Лимит должен быть от 1 до 1 000 000")
    with db() as c:
        try:
            c.execute(
                "INSERT INTO promos(code,percent,max_uses,used) VALUES(?,?,?,0)",
                (code, percent, max_uses),
            )
        except sqlite3.IntegrityError:
            raise ApiError("Такой промокод уже есть")
    return web.json_response({"ok": True})


async def admin_delete_promo(request):
    check_admin(request)
    code = request.match_info["code"].upper()
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("DELETE FROM redemptions WHERE code=?", (code,))
        c.execute("DELETE FROM promos WHERE code=?", (code,))
        c.execute("COMMIT")
    return web.json_response({"ok": True})


def create_app() -> web.Application:
    init_db()
    app = web.Application(middlewares=[mw], client_max_size=8 * 1024 * 1024)
    app.add_routes(
        [
            web.get("/", root),
            web.get("/api/products", list_products),
            web.get(r"/api/products/{id:\d+}/image", product_image),
            web.get("/api/promo/mine", promo_mine),
            web.post("/api/promo/redeem", promo_redeem),
            web.get("/api/banner", banner),
            web.get("/api/subs/mine", subs_mine),
            web.post(r"/api/products/{id:\d+}/notify", product_notify),
            web.post("/api/orders", orders_create),
            web.get("/api/orders/mine", orders_mine),
            web.post("/api/admin/login", admin_login),
            web.post("/api/admin/products", admin_save_product),
            web.delete(r"/api/admin/products/{id:\d+}", admin_delete_product),
            web.get("/api/admin/promos", admin_list_promos),
            web.post("/api/admin/promos", admin_create_promo),
            web.delete("/api/admin/promos/{code}", admin_delete_promo),
        ]
    )
    return app
    
