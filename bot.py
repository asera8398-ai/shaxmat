"""
bot.py — Shashka Arena: Telegram bot + Mini App backend.

Ishga tushirish:
    python bot.py

.env (Railway/VPS o'zgaruvchilari):
    BOT_TOKEN=...            @BotFather dan
    ADMIN_ID=123456789       admin Telegram ID (bir nechta bo'lsa: 111,222)
    WEBAPP_URL=https://...   Mini App manzili (shu serverning "/" yo'li)
    DATABASE_URL=postgresql://...
    CARD_NUMBER=8600...      to'lov kartasi
    CARD_OWNER=Familiya I
    CHANNEL_USERNAME=kanal   (ixtiyoriy) majburiy obuna kanali
    PORT=8080
"""
from __future__ import annotations

import os
import re
import time
import json
import random
import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.types import (
    Message, CallbackQuery, TelegramObject,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, WebAppInfo,
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

from database import Database
import webapp_api
import tolov_api
import humo_listener

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("shashka")

# ─── SOZLAMALAR ────────────────────────────────────────────────
def _clean_token(v: str) -> str:
    """Telegram bot tokenida HECH QANDAY bo'shliq bo'lmaydi — boshida,
    oxirida yoki o'rtasida bo'lsin, hammasini olib tashlaymiz. Bu
    BotFather'dan nusxalashda tasodifan qo'shilib qolgan probel yoki
    qator ko'chirish sabab bo'lgan 'Token is invalid! It can't contain
    spaces.' xatosining oldini oladi."""
    return re.sub(r"\s+", "", v or "")

BOT_TOKEN   = _clean_token(os.getenv("BOT_TOKEN", ""))
ADMIN_IDS   = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_ID", "0")) if int(x)}
ADMIN_ID    = min(ADMIN_IDS) if ADMIN_IDS else 0
WEBAPP_URL  = os.getenv("WEBAPP_URL", "").strip()
CARD_NUMBER = os.getenv("CARD_NUMBER", "8600 0000 0000 0000")
CARD_OWNER  = os.getenv("CARD_OWNER", "Familiya I.")
CHANNEL     = os.getenv("CHANNEL_USERNAME", "").lstrip("@")
LOG_CHANNEL = os.getenv("LOG_CHANNEL", "")
PORT        = int(os.getenv("PORT", "8080"))

if not re.fullmatch(r"\d+:[\w-]{20,}", BOT_TOKEN):
    logger.error(
        "=" * 60 + "\n"
        "BOT_TOKEN NOTO'G'RI yoki bo'sh!\n"
        "Railway → xizmatingiz → Variables → BOT_TOKEN qatorini oching,\n"
        "qiymatni butunlay o'chirib, @BotFather bergan tokenni FAQAT\n"
        "o'zini (masalan 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)\n"
        "qo'shtirnoqsiz, bo'shliqsiz qayta joylashtiring.\n"
        f"Hozirgi topilgan qiymat (uzunligi {len(BOT_TOKEN)} belgi): {BOT_TOKEN!r}\n"
        + "=" * 60
    )
    raise SystemExit(1)

bot   = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp    = Dispatcher(storage=MemoryStorage())
user_router  = Router()
admin_router = Router()
db = Database()

EXTRA_ADMIN_IDS: set[int] = set()   # botning o'zidan qo'shilgan qo'shimcha adminlar (DB'dan yuklanadi)
is_admin = lambda uid: uid in ADMIN_IDS or uid in EXTRA_ADMIN_IDS
is_root_admin = lambda uid: uid in ADMIN_IDS   # faqat .env dagi asosiy adminlar boshqa admin qo'sha/o'chira oladi
all_admin_ids = lambda: ADMIN_IDS | EXTRA_ADMIN_IDS


# ─── HOLATLAR ──────────────────────────────────────────────────
class Pay(StatesGroup):
    amount = State()
    check  = State()
    auto   = State()
    wd_amount = State()
    wd_card   = State()

class Adm(StatesGroup):
    value      = State()   # universal sozlama qiymati
    search     = State()
    give       = State()
    broadcast  = State()
    ann        = State()
    pack       = State()
    tour       = State()
    channel    = State()
    admin_add  = State()


# ─── YORDAMCHILAR ──────────────────────────────────────────────
async def S(key, default=0, cast=int):
    v = await db.get_setting(key)
    try:
        return cast(v) if v is not None else cast(default)
    except Exception:
        return cast(default)

def money(n) -> str:
    return f"{int(n):,}".replace(",", " ")

def name_of(u) -> str:
    return (u.get("first_name", "") + " " + (u.get("last_name") or "")).strip() if isinstance(u, dict) else ""

async def log_event(text: str):
    if not LOG_CHANNEL:
        return
    try:
        await bot.send_message(LOG_CHANNEL, text)
    except Exception:
        pass

async def notify(user_id: int, text: str, kb=None):
    try:
        await bot.send_message(user_id, text, reply_markup=kb)
        return True
    except Exception:
        return False

def kb(rows) -> InlineKeyboardMarkup:
    def one(t, d):
        if d.startswith("webapp:"):
            return InlineKeyboardButton(text=t, web_app=WebAppInfo(url=d[7:]))
        if d.startswith("url:"):
            return InlineKeyboardButton(text=t, url=d[4:])
        return InlineKeyboardButton(text=t, callback_data=d)
    return InlineKeyboardMarkup(inline_keyboard=[[one(t, d) for t, d in row] for row in rows])

def _webapp_url_ok() -> bool:
    """WEBAPP_URL haqiqiy ochiq (https://...) manzilmi, yoki Railway'ning
    faqat ICHKI tarmog'iga tegishli '.railway.internal' manzilmi — buni
    tekshiradi. Telegram web_app tugmasi faqat public HTTPS manzilni
    qabul qiladi; '.railway.internal' unga umuman ko'rinmaydi."""
    return bool(WEBAPP_URL) and WEBAPP_URL.startswith("https://") and ".railway.internal" not in WEBAPP_URL

def main_menu() -> ReplyKeyboardMarkup:
    play_btn = (KeyboardButton(text="🎮 O'ynash", web_app=WebAppInfo(url=WEBAPP_URL))
                if _webapp_url_ok() else KeyboardButton(text="🎮 O'ynash (havola sozlanmagan)"))
    rows = [[play_btn],
            [KeyboardButton(text="💎 Ball sotib olish"), KeyboardButton(text="💰 Hisobim")],
            [KeyboardButton(text="🏆 Chempionat"), KeyboardButton(text="👥 Do'st taklif qilish")],
            [KeyboardButton(text="📞 Yordam")]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

BOT_USERNAME = {"v": ""}


# ─── MIDDLEWARE: ta'mirlash + faollik ──────────────────────────
SUB_CACHE: dict[int, float] = {}   # user_id -> oxirgi tasdiqlangan vaqt (majburiy obuna keshi)
SUB_CACHE_TTL = 600                # soniya — shuncha vaqt qayta tekshirilmaydi

async def get_missing_channels(user_id: int, force: bool = False) -> list:
    """Foydalanuvchi hali obuna bo'lmagan majburiy kanallar ro'yxati.
    Bot biror kanalni tekshira olmasa (masalan u yerda admin emas),
    o'sha kanal talabdan chetlab o'tiladi — xato sozlangan kanal butun
    botni qulflab qo'ymasligi kerak. Tasdiqlangan foydalanuvchilar bir
    necha daqiqaga keshlanadi — har xabarda Telegram API'ga urilmaslik
    uchun."""
    if not force and SUB_CACHE.get(user_id, 0) > time.time() - SUB_CACHE_TTL:
        return []
    channels = await db.list_required_channels()
    if not channels:
        SUB_CACHE[user_id] = time.time()
        return []
    missing = []
    for ch in channels:
        target = ch["chat_id"] or (f"@{ch['username']}" if ch["username"] else None)
        if not target:
            continue
        try:
            member = await bot.get_chat_member(target, user_id)
            if member.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            continue
    if not missing:
        SUB_CACHE[user_id] = time.time()
    return missing

def subscribe_kb(channels) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        url = ch["invite_link"] or (f"https://t.me/{ch['username']}" if ch["username"] else "")
        if url:
            rows.append([(f"📢 {ch['title'] or ch['username'] or 'Kanal'}", "url:" + url)])
    rows.append([("✅ Tekshirish", "check_sub")])
    return kb(rows)


class Guard(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user:
            if not is_admin(user.id) and await S("maintenance", 0):
                if isinstance(event, Message):
                    await event.answer("🔧 Bot vaqtincha texnik ishlar rejimida. Tez orada qaytamiz!")
                return
            row = await db.get_user(user.id)
            if row and row["is_blocked"] and not is_admin(user.id):
                return
            if not is_admin(user.id):
                is_start = isinstance(event, Message) and (event.text or "").startswith("/start")
                is_check_cb = isinstance(event, CallbackQuery) and event.data == "check_sub"
                if not is_start and not is_check_cb:
                    missing = await get_missing_channels(user.id)
                    if missing:
                        if isinstance(event, Message):
                            await event.answer(
                                "📢 Botdan foydalanish uchun avval quyidagi kanal(lar)ga obuna bo'ling, "
                                "so'ng \"✅ Tekshirish\" tugmasini bosing:", reply_markup=subscribe_kb(missing))
                        elif isinstance(event, CallbackQuery):
                            await event.answer("Avval kanal(lar)ga obuna bo'ling", show_alert=True)
                        return
        return await handler(event, data)


@user_router.callback_query(F.data == "check_sub")
async def check_sub_cb(call: CallbackQuery):
    missing = await get_missing_channels(call.from_user.id, force=True)
    if missing:
        return await call.answer("❌ Hali hammasiga obuna bo'lmagansiz", show_alert=True)
    try:
        await call.message.delete()
    except Exception:
        pass
    await call.message.answer("✅ Rahmat! Endi botdan to'liq foydalanishingiz mumkin.", reply_markup=main_menu())
    await call.answer("Tasdiqlandi ✅")


# ══════════════════════════════════════════════════════════════
#                      FOYDALANUVCHI QISMI
# ══════════════════════════════════════════════════════════════
@user_router.message(CommandStart())
async def start_handler(msg: Message, state: FSMContext):
    await state.clear()
    args = msg.text.split(maxsplit=1)
    param = args[1].strip() if len(args) > 1 else ""
    ref = None
    if param.isdigit() and int(param) != msg.from_user.id:
        ref = int(param)

    existing = await db.get_user(msg.from_user.id)
    fname = (msg.from_user.first_name or "") + (" " + msg.from_user.last_name if msg.from_user.last_name else "")
    await db.add_user(msg.from_user.id, fname.strip(), msg.from_user.username or "", "", ref)

    if not existing:
        start_pts = await S("start_points", 100)
        await log_event(f"🆕 Yangi o'yinchi: {fname} (<code>{msg.from_user.id}</code>)")
        if ref:
            bonus = await S("referral_points", 100)
            await db.add_points(ref, bonus)
            await db.log_transaction(ref, "referral", bonus, f"ref:{msg.from_user.id}", "ball")
            await notify(ref, f"🎁 <b>Yangi o'yinchi</b> sizning havolangiz orqali qo'shildi!\n+{bonus} ball berildi.")
    else:
        start_pts = 0

    missing = await get_missing_channels(msg.from_user.id) if not is_admin(msg.from_user.id) else []
    if missing:
        return await msg.answer(
            f"♟ <b>Shashka Arena</b>ga xush kelibsiz, {msg.from_user.first_name}!\n\n"
            + (f"🎁 Sovg'a: <b>{start_pts} ball</b> hisobingizga allaqachon qo'shildi!\n\n" if start_pts else "")
            + "📢 Botdan foydalanishdan oldin quyidagi kanal(lar)ga obuna bo'ling, "
              "so'ng \"✅ Tekshirish\" tugmasini bosing:",
            reply_markup=subscribe_kb(missing))


    t = await db.active_tournament()
    extra = ""
    if t:
        extra = (f"\n\n🏆 <b>{t['title']}</b>\n{t['prize_text']}\n"
                 f"⏳ Tugashiga: {left_text(t['ends_at'])}")

    await msg.answer(
        f"♟ <b>Shashka Arena</b>ga xush kelibsiz, {msg.from_user.first_name}!\n\n"
        f"• Rus shashkasi — bot va real raqiblar bilan\n"
        f"• G'alaba qozoning va <b>ball</b> yig'ing\n"
        f"• Haftalik chempionatlarda <b>pul mukofotlari</b>\n"
        + (f"\n🎁 Sovg'a: <b>{start_pts} ball</b> hisobingizga qo'shildi!" if start_pts else "")
        + extra + "\n\nPastdagi <b>🎮 O'ynash</b> tugmasini bosing 👇",
        reply_markup=main_menu())

    # Mini App ichidan kelgan havolalar: ?start=ball | topup | ref
    if param == "ball":
        await show_packs(msg, msg.from_user.id)
    elif param == "topup":
        await topup_menu(msg)
    elif param == "ref":
        await referral(msg)


def left_text(dt: datetime) -> str:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    d = dt - now
    if d.total_seconds() <= 0:
        return "tugadi"
    days, rem = divmod(int(d.total_seconds()), 86400)
    hours = rem // 3600
    return (f"{days} kun " if days else "") + f"{hours} soat"


@user_router.message(F.text == "💰 Hisobim")
async def my_account(msg: Message):
    u = await db.get_user(msg.from_user.id)
    if not u:
        u = await db.add_user(msg.from_user.id, msg.from_user.full_name, msg.from_user.username or "")
    rank = await db.my_rank(msg.from_user.id)
    games = u["wins"] + u["losses"] + u["draws"]
    wr = round(u["wins"] / games * 100) if games else 0
    await msg.answer(
        f"👤 <b>{u['fullname']}</b>\n"
        f"🆔 <code>{u['user_id']}</code>\n\n"
        f"💎 Ball: <b>{money(u['points'])}</b>\n"
        f"💰 Hisob: <b>{money(u['balance'])} so'm</b>\n\n"
        f"📊 ELO: <b>{u['elo']}</b> · {rank}-o'rin\n"
        f"🏅 {u['wins']} g'alaba / {u['losses']} mag'lub / {u['draws']} durang ({wr}%)",
        reply_markup=kb([[("💳 Hisobni to'ldirish", "topup")], [("💎 Ball sotib olish", "packs")],
                        [("🏧 Kartaga yechish", "withdraw")]]))


@user_router.callback_query(F.data == "withdraw")
async def withdraw_start(call: CallbackQuery, state: FSMContext):
    u = await db.get_user(call.from_user.id)
    mn = await S("min_withdraw", 20000)
    if u["balance"] < mn:
        await call.message.answer(
            f"❌ Yechish uchun kamida <b>{money(mn)} so'm</b> hisobingizda bo'lishi kerak.\n"
            f"Joriy hisob: <b>{money(u['balance'])} so'm</b>")
        return await call.answer()
    await state.set_state(Pay.wd_amount)
    await call.message.answer(
        f"🏧 <b>Kartaga pul yechish</b>\n\n"
        f"Joriy hisobingiz: <b>{money(u['balance'])} so'm</b>\n"
        f"Eng kami: <b>{money(mn)} so'm</b>\n\n"
        f"Qancha so'm yechmoqchisiz? Summani yozing:")
    await call.answer()


@user_router.message(Pay.wd_amount)
async def withdraw_amount(msg: Message, state: FSMContext):
    amount = int(re.sub(r"\D", "", msg.text or "") or 0)
    u = await db.get_user(msg.from_user.id)
    mn = await S("min_withdraw", 20000)
    if amount < mn:
        return await msg.answer(f"❌ Eng kamida {money(mn)} so'm.")
    if amount > u["balance"]:
        return await msg.answer(f"❌ Hisobingizda yetarli mablag' yo'q. Joriy: {money(u['balance'])} so'm.")
    await state.update_data(amount=amount)
    await state.set_state(Pay.wd_card)
    await msg.answer("💳 Pulni qaysi karta raqamiga o'tkazib berishimizni yozing (16 xonali):")


@user_router.message(Pay.wd_card)
async def withdraw_card(msg: Message, state: FSMContext):
    card = re.sub(r"\s+", " ", (msg.text or "").strip())
    digits = re.sub(r"\D", "", card)
    if len(digits) < 16:
        return await msg.answer("❌ Karta raqami noto'g'ri ko'rinadi. 16 xonali raqamni to'liq yuboring.")
    data = await state.get_data()
    amount = data["amount"]
    await state.clear()
    u = await db.get_user(msg.from_user.id)
    if amount > u["balance"]:
        return await msg.answer("❌ Hisobingizda yetarli mablag' yo'q edi. Qaytadan urinib ko'ring.")
    await db.update_balance(msg.from_user.id, -amount)
    await db.log_transaction(msg.from_user.id, "withdraw_request", -amount, card)
    wid = await db.add_withdrawal(msg.from_user.id, amount, card)
    await msg.answer(
        f"✅ So'rovingiz qabul qilindi!\n\n💰 Summa: <b>{money(amount)} so'm</b>\n💳 Karta: <code>{card}</code>\n\n"
        f"Admin tekshirib, tez orada pulingizni o'tkazadi. Hisobingizdan summa hozircha ayirib qo'yildi "
        f"(rad etilsa, avtomatik qaytariladi).")
    cap = (f"🏧 <b>Yangi pul yechish so'rovi</b>\n\n👤 {u['fullname']}\n🆔 <code>{msg.from_user.id}</code>\n"
           f"💰 <b>{money(amount)} so'm</b>\n💳 <code>{card}</code>")
    mk = kb([[("✅ To'landi", f"wdpaid:{wid}"), ("❌ Rad etish", f"wdrej:{wid}")]])
    for aid in all_admin_ids():
        await notify(aid, cap, mk)


@user_router.message(F.text.in_({"💎 Ball sotib olish", "/ball"}))
async def packs_msg(msg: Message):
    await show_packs(msg)

@user_router.callback_query(F.data == "packs")
async def packs_cb(call: CallbackQuery):
    await show_packs(call.message, call.from_user.id)
    await call.answer()

async def show_packs(target: Message, uid: int | None = None):
    uid = uid or target.chat.id
    u = await db.get_user(uid)
    packs = await db.packages()
    if not packs:
        return await target.answer("Hozircha paketlar mavjud emas.")
    rows = [[(f"{p['title']} · {money(p['points'])} ball — {money(p['price'])} so'm"
              + (f" {p['badge']}" if p["badge"] else ""), f"buy:{p['id']}")] for p in packs]
    rows.append([("💳 Hisobni to'ldirish", "topup")])
    await target.answer(
        f"💎 <b>Ball paketlari</b>\n\nJoriy hisobingiz: <b>{money(u['balance'] if u else 0)} so'm</b>\n"
        f"Ball: <b>{money(u['points'] if u else 0)}</b>\n\n"
        f"Ball bilan maslahat, xodni qaytarish, damka va yangi taxta dizaynlarini olasiz.\n"
        f"Paketni tanlang 👇", reply_markup=kb(rows))


@user_router.callback_query(F.data.startswith("buy:"))
async def buy_pack(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    p = await db.get_package(pid)
    u = await db.get_user(call.from_user.id)
    if not p or not p["active"]:
        return await call.answer("Paket topilmadi", show_alert=True)
    if u["balance"] < p["price"]:
        need = p["price"] - u["balance"]
        await call.message.answer(
            f"❌ Hisobingizda mablag' yetarli emas.\n\n"
            f"Kerak: <b>{money(p['price'])} so'm</b>\n"
            f"Bor: <b>{money(u['balance'])} so'm</b>\n"
            f"Yetishmaydi: <b>{money(need)} so'm</b>",
            reply_markup=kb([[("💳 Hisobni to'ldirish", "topup")]]))
        return await call.answer()
    await db.update_balance(call.from_user.id, -p["price"])
    pts = await db.add_points(call.from_user.id, p["points"])
    await db.log_transaction(call.from_user.id, "package", p["price"], f"pack:{p['id']}")
    await db.log_transaction(call.from_user.id, "package", p["points"], p["title"], "ball")
    await call.message.answer(
        f"✅ <b>{money(p['points'])} ball</b> hisobingizga qo'shildi!\n\n"
        f"💎 Jami ball: <b>{money(pts)}</b>\n"
        f"💰 Qolgan hisob: <b>{money(u['balance'] - p['price'])} so'm</b>\n\n"
        f"Endi o'ynashda foydalanishingiz mumkin 🎮")
    await log_event(f"💎 Sotuv: {u['fullname']} — {p['title']} ({money(p['price'])} so'm)")
    await call.answer("Sotib olindi ✅")


# ─── HISOBNI TO'LDIRISH ────────────────────────────────────────
@user_router.message(F.text == "📞 Yordam")
async def support(msg: Message):
    admin_un = os.getenv("SUPPORT_USERNAME", "")
    await msg.answer(
        "📞 <b>Yordam</b>\n\n"
        "• To'lov 2 daqiqada avtomatik tasdiqlanadi\n"
        "• Tasdiqlanmasa — chekni admin ko'rib chiqadi\n"
        "• Savol/taklif bo'lsa yozing"
        + (f"\n\n👤 Admin: @{admin_un}" if admin_un else ""))


@user_router.message(F.text == "💳 Hisobni to'ldirish")
@user_router.callback_query(F.data == "topup")
async def topup_menu(ev, state: FSMContext = None):
    msg = ev.message if isinstance(ev, CallbackQuery) else ev
    auto = await S("auto_pay_enabled", 1)
    rows = []
    if auto:
        rows.append([("⚡️ Avtomatik to'lov (tavsiya)", "topup_auto")])
    rows.append([("🧾 Chek yuborish (qo'lda)", "topup_manual")])
    await msg.answer(
        "💳 <b>Hisobni to'ldirish</b>\n\n"
        "⚡️ <b>Avtomatik</b> — summani kiritasiz, biz sizga <u>maxsus summa</u> beramiz. "
        "Shu summani kartaga o'tkazasiz va balans <b>1-2 daqiqada o'zi</b> to'ladi.\n\n"
        "🧾 <b>Qo'lda</b> — to'lov chekini yuborasiz, admin tasdiqlaydi.",
        reply_markup=kb(rows))
    if isinstance(ev, CallbackQuery):
        await ev.answer()


@user_router.callback_query(F.data == "topup_auto")
async def topup_auto(call: CallbackQuery, state: FSMContext):
    mn = await S("min_topup", 5000)
    await state.set_state(Pay.auto)
    await call.message.answer(
        f"⚡️ <b>Avtomatik to'lov</b>\n\nQancha so'm to'ldirmoqchisiz?\n"
        f"Eng kami: <b>{money(mn)} so'm</b>\n\nSummani yozing (masalan: 20000)")
    await call.answer()


@user_router.message(Pay.auto)
async def topup_auto_amount(msg: Message, state: FSMContext):
    amount = int(re.sub(r"\D", "", msg.text or "") or 0)
    mn = await S("min_topup", 5000)
    if amount < mn:
        return await msg.answer(f"❌ Eng kamida {money(mn)} so'm kiriting.")
    res = await reserve_amount(msg.from_user.id, amount)
    if not res:
        await state.clear()
        return await msg.answer("⚠️ Hozir tizim band. 1-2 daqiqadan so'ng urinib ko'ring yoki chek yuboring.")
    await state.clear()
    await msg.answer(
        f"⚡️ <b>To'lovni amalga oshiring</b>\n\n"
        f"💳 Karta: <code>{res['card_number']}</code>\n"
        f"👤 Egasi: <b>{res['card_owner']}</b>\n\n"
        f"💰 <b>AYNAN shu summani</b> yuboring:\n"
        f"👉 <code>{res['final_amount']}</code> so'm\n\n"
        f"⚠️ Summa <b>{res['offset']} so'mga</b> ko'paytirilgan — bu sizning to'lovingizni "
        f"tanib olish uchun. Boshqa summa yuborilsa, avtomatik tasdiqlanmaydi.\n\n"
        f"⏳ Amal qilish muddati: <b>{res['expiry_min']} daqiqa</b>\n"
        f"To'lovdan so'ng balans o'zi to'ladi va sizga xabar keladi ✅",
        reply_markup=kb([[("❌ Bekor qilish", f"cancelpay:{res['payment_id']}")]]))


async def reserve_amount(user_id: int, base: int):
    """Noyob summa band qiladi (asl loyihadagi mantiq bilan bir xil)."""
    await db.expire_old_auto_payments()
    max_off  = await S("auto_pay_offset", 50)
    expiry   = await S("auto_pay_expiry", 30)
    card_num = await db.get_setting("card_number") or CARD_NUMBER
    card_own = await db.get_setting("card_owner") or CARD_OWNER
    last4 = "".join(c for c in card_num if c.isdigit())[-4:]
    reserved = await db.get_reserved_amounts(last4)
    final = None
    for _ in range(max(10, max_off * 2)):
        cand = base + random.randint(1, max(2, max_off))
        if cand not in reserved:
            final = cand
            break
    if final is None:
        return None
    exp = datetime.now(timezone.utc) + timedelta(minutes=expiry)
    pid = await db.add_auto_payment(user_id, base, final, exp, last4)
    return {"payment_id": pid, "final_amount": final, "offset": final - base,
            "card_number": card_num, "card_owner": card_own, "expiry_min": expiry}


@user_router.callback_query(F.data.startswith("cancelpay:"))
async def cancel_pay(call: CallbackQuery):
    await db.cancel_auto_payment(int(call.data.split(":")[1]))
    await call.message.edit_text("❌ To'lov bekor qilindi.")
    await call.answer()


@user_router.callback_query(F.data == "topup_manual")
async def topup_manual(call: CallbackQuery, state: FSMContext):
    await state.set_state(Pay.amount)
    await call.message.answer("🧾 Qancha so'm to'ldirdingiz? Summani yozing:")
    await call.answer()


@user_router.message(Pay.amount)
async def manual_amount(msg: Message, state: FSMContext):
    amount = int(re.sub(r"\D", "", msg.text or "") or 0)
    mn = await S("min_topup", 5000)
    if amount < mn:
        return await msg.answer(f"❌ Eng kamida {money(mn)} so'm.")
    card_num = await db.get_setting("card_number") or CARD_NUMBER
    card_own = await db.get_setting("card_owner") or CARD_OWNER
    await state.update_data(amount=amount)
    await state.set_state(Pay.check)
    await msg.answer(
        f"💳 Karta: <code>{card_num}</code>\n👤 {card_own}\n💰 Summa: <b>{money(amount)} so'm</b>\n\n"
        f"To'lovni amalga oshiring va <b>chek rasmini</b> shu yerga yuboring 📸")


@user_router.message(Pay.check, F.photo | F.document)
async def manual_check(msg: Message, state: FSMContext):
    data = await state.get_data()
    amount = data.get("amount", 0)
    await state.clear()
    pay_id = secrets.token_hex(4)
    await db.add_pending_payment(pay_id, msg.from_user.id, amount, msg.from_user.full_name)
    await msg.answer("✅ Chek qabul qilindi. Admin tekshirgach, hisobingiz to'ldiriladi (odatda 5-10 daqiqa).")
    cap = (f"🧾 <b>Yangi chek</b>\n\n👤 {msg.from_user.full_name}\n"
           f"🆔 <code>{msg.from_user.id}</code>\n💰 <b>{money(amount)} so'm</b>")
    mk = kb([[("✅ Tasdiqlash", f"payok:{pay_id}"), ("❌ Rad etish", f"payno:{pay_id}")]])
    for aid in all_admin_ids():
        try:
            await bot.copy_message(aid, msg.chat.id, msg.message_id, caption=cap, reply_markup=mk)
        except Exception:
            await notify(aid, cap, mk)


@user_router.message(Pay.check)
async def manual_check_wrong(msg: Message):
    await msg.answer("📸 Iltimos, chek <b>rasmini</b> yuboring.")


@admin_router.callback_query(F.data.startswith("payok:"))
async def pay_ok(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    row = await db.pop_pending_payment(call.data.split(":")[1])
    if not row:
        return await call.answer("Bu to'lov allaqachon ko'rib chiqilgan", show_alert=True)
    await db.update_balance(row["user_id"], row["amount"])
    await db.log_transaction(row["user_id"], "topup", row["amount"], "manual")
    u = await db.get_user(row["user_id"])
    await notify(row["user_id"],
                 f"✅ To'lovingiz tasdiqlandi!\n\n💰 +{money(row['amount'])} so'm\n"
                 f"Joriy hisob: <b>{money(u['balance'])} so'm</b>",
                 kb([[("💎 Ball sotib olish", "packs")]]))
    await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n✅ TASDIQLANDI")
    await call.answer("Tasdiqlandi")


@admin_router.callback_query(F.data.startswith("payno:"))
async def pay_no(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    row = await db.pop_pending_payment(call.data.split(":")[1])
    if row:
        await notify(row["user_id"], "❌ To'lovingiz tasdiqlanmadi. Chekni qayta yuboring yoki admin bilan bog'laning.")
    await call.message.edit_caption(caption=(call.message.caption or "") + "\n\n❌ RAD ETILDI")
    await call.answer()


# ─── CHEMPIONAT / REFERAL ──────────────────────────────────────
@user_router.message(F.text == "🏆 Chempionat")
async def champ(msg: Message):
    t = await db.active_tournament()
    if not t:
        return await msg.answer("🏆 Hozircha faol chempionat yo'q.\nTez orada e'lon qilamiz — kuzatib boring!")
    top = await db.tournament_top(t["id"], 10)
    place = await db.tournament_place(t["id"], msg.from_user.id)
    lines = "\n".join(
        f"{'🥇🥈🥉'[i] if i < 3 else f'{i+1}.'} {r['fullname'][:18]} — <b>{r['score']}</b>"
        for i, r in enumerate(top)) or "Hali ishtirokchi yo'q — birinchi bo'ling!"
    await msg.answer(
        f"🏆 <b>{t['title']}</b>\n\n{t['descr']}\n\n"
        f"🎁 <b>Mukofot jamg'armasi:</b>\n{t['prize_text']}\n\n"
        f"⏳ Tugashiga: <b>{left_text(t['ends_at'])}</b>\n\n"
        f"📊 <b>TOP-10</b>\n{lines}\n\n"
        f"📍 Sizning o'rningiz: <b>{place}</b>\n\n"
        f"Har g'alaba = <b>{await S('tournament_win_score', 10)} ochko</b>. O'ynang va yuqoriga chiqing!",
        reply_markup=kb([[("🎮 Hoziroq o'ynash", "webapp:" + WEBAPP_URL)]] if _webapp_url_ok() else []))


@user_router.message(F.text == "👥 Do'st taklif qilish")
async def referral(msg: Message):
    bonus = await S("referral_points", 100)
    link = f"https://t.me/{BOT_USERNAME['v']}?start={msg.from_user.id}"
    await msg.answer(
        f"👥 <b>Do'stlaringizni taklif qiling</b>\n\n"
        f"Havolangiz orqali kirgan har bir yangi o'yinchi uchun <b>+{bonus} ball</b> olasiz.\n\n"
        f"🔗 <code>{link}</code>\n\nUlashing va ball yig'ing!")


# ══════════════════════════════════════════════════════════════
#                          ADMIN PANEL
# ══════════════════════════════════════════════════════════════
async def admin_home(target, edit=False):
    st = await db.stats()
    t = await db.active_tournament()
    pend_w = await db.list_withdrawals("pending")
    text = (
        f"🛠 <b>ADMIN PANEL</b>\n\n"
        f"👥 O'yinchilar: <b>{st['users']}</b> (bugun +{st['new_today']})\n"
        f"🟢 Bugun faol: <b>{st['active_today']}</b>\n"
        f"🎮 O'yinlar: <b>{st['games']}</b> (bugun {st['games_today']})\n\n"
        f"💰 Bugungi tushum: <b>{money(st['topup_today'])} so'm</b>\n"
        f"💰 Jami tushum: <b>{money(st['topup_all'])} so'm</b>\n"
        f"💎 Muomaladagi ball: <b>{money(st['points_sum'])}</b>\n\n"
        f"🏆 Chempionat: <b>{t['title'] + ' (' + left_text(t['ends_at']) + ')' if t else 'yo`q'}</b>"
        + (f"\n💸 Kutilayotgan pul yechish: <b>{len(pend_w)} ta</b>" if pend_w else "")
    )
    m = kb([
        [("📊 Statistika", "a_stats"), ("👥 O'yinchilar", "a_users")],
        [("💎 Ball paketlari", "a_packs"), ("🎮 O'yin sozlamasi", "a_game")],
        [("🏆 Chempionat", "a_tour"), ("📣 E'lonlar", "a_ann")],
        [(f"💸 Pul yechish{' ('+str(len(pend_w))+')' if pend_w else ''}", "a_withdraw"), ("💳 To'lov sozlamasi", "a_pay")],
        [("📢 Majburiy obuna", "a_channels"), ("👤 Adminlar", "a_admins")],
        [("📨 Xabar yuborish", "a_bcast"), ("🔧 Texnik rejim", "a_maint")],
        [("🔄 Yangilash", "a_home")],
    ])
    if edit and isinstance(target, CallbackQuery):
        try:
            return await target.message.edit_text(text, reply_markup=m)
        except Exception:
            return await target.message.answer(text, reply_markup=m)
    await (target.message if isinstance(target, CallbackQuery) else target).answer(text, reply_markup=m)


@admin_router.message(Command("admin"))
async def admin_cmd(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    await admin_home(msg)


@admin_router.callback_query(F.data == "a_home")
async def a_home(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    await admin_home(call, edit=True)
    await call.answer()


@admin_router.callback_query(F.data == "a_stats")
async def a_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    st = await db.stats()
    await call.message.edit_text(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Jami o'yinchi: <b>{st['users']}</b>\n"
        f"🆕 Bugun qo'shilgan: <b>{st['new_today']}</b>\n"
        f"🟢 Bugun faol: <b>{st['active_today']}</b>\n\n"
        f"🎮 Jami o'yin: <b>{st['games']}</b>\n"
        f"🎮 Bugun: <b>{st['games_today']}</b>\n\n"
        f"💰 Tushum (bugun): <b>{money(st['topup_today'])} so'm</b>\n"
        f"💰 Tushum (jami): <b>{money(st['topup_all'])} so'm</b>\n"
        f"🛒 Ball sotuvi (bugun): <b>{money(st['sales_today'])} so'm</b>\n\n"
        f"💰 Hisoblardagi pul: <b>{money(st['balance_sum'])} so'm</b>\n"
        f"💎 Muomaladagi ball: <b>{money(st['points_sum'])}</b>",
        reply_markup=kb([[("⬅️ Orqaga", "a_home")]]))
    await call.answer()


# ─── O'YINCHILAR ───────────────────────────────────────────────
@admin_router.callback_query(F.data == "a_users")
async def a_users(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(Adm.search)
    await call.message.edit_text(
        "👥 <b>O'yinchini qidirish</b>\n\nID, ism yoki @username yuboring.\n"
        "Masalan: <code>123456789</code> yoki <code>Alijon</code>",
        reply_markup=kb([[("⬅️ Orqaga", "a_home")]]))
    await call.answer()


@admin_router.message(Adm.search)
async def a_search(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    rows = await db.find_users(msg.text.strip().lstrip("@"))
    await state.clear()
    if not rows:
        return await msg.answer("❌ Topilmadi.", reply_markup=kb([[("⬅️ Orqaga", "a_home")]]))
    if len(rows) == 1:
        return await show_user_card(msg, rows[0]["user_id"])
    await msg.answer("Topildi:", reply_markup=kb(
        [[(f"{r['fullname'][:22]} · {money(r['points'])} ball", f"au:{r['user_id']}")] for r in rows[:10]]
        + [[("⬅️ Orqaga", "a_home")]]))


async def show_user_card(target, uid: int, edit=False):
    u = await db.get_user(uid)
    if not u:
        return
    hist = await db.user_history(uid, 5)
    h = "\n".join(f"• {r['type']} {'+' if r['amount'] > 0 else ''}{money(r['amount'])}"
                  f" {'ball' if r['currency'] == 'ball' else 'so`m'}" for r in hist) or "—"
    text = (f"👤 <b>{u['fullname']}</b> {'@' + u['username'] if u['username'] else ''}\n"
            f"🆔 <code>{u['user_id']}</code>\n"
            f"📅 {u['created_at']:%d.%m.%Y}\n\n"
            f"💎 Ball: <b>{money(u['points'])}</b>\n"
            f"💰 Hisob: <b>{money(u['balance'])} so'm</b>\n"
            f"💳 Jami to'ldirgan: <b>{money(u['total_deposited'])} so'm</b>\n\n"
            f"📊 ELO {u['elo']} · {u['wins']}W/{u['losses']}L/{u['draws']}D\n"
            f"🚫 Bloklangan: <b>{'HA' if u['is_blocked'] else 'yo`q'}</b>\n\n"
            f"🧾 Oxirgi amallar:\n{h}")
    m = kb([
        [("💎 Ball berish", f"agive:ball:{uid}"), ("💰 Pul berish", f"agive:sum:{uid}")],
        [("♻️ Ballni nolga", f"azero:{uid}"),
         ("🚫 Blok" if not u["is_blocked"] else "✅ Blokdan olish", f"aban:{uid}")],
        [("✉️ Shaxsiy xabar", f"amsg:{uid}")],
        [("⬅️ Orqaga", "a_home")]])
    t = target.message if isinstance(target, CallbackQuery) else target
    if edit:
        try:
            return await t.edit_text(text, reply_markup=m)
        except Exception:
            pass
    await t.answer(text, reply_markup=m)


@admin_router.callback_query(F.data.startswith("au:"))
async def a_user_open(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await show_user_card(call, int(call.data.split(":")[1]), edit=True)
    await call.answer()


@admin_router.callback_query(F.data.startswith(("agive:", "amsg:")))
async def a_give_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    parts = call.data.split(":")
    if parts[0] == "amsg":
        await state.set_state(Adm.give)
        await state.update_data(mode="msg", uid=int(parts[1]))
        await call.message.answer("✉️ Yuboriladigan xabar matnini yozing:")
    else:
        await state.set_state(Adm.give)
        await state.update_data(mode=parts[1], uid=int(parts[2]))
        unit = "ball" if parts[1] == "ball" else "so'm"
        await call.message.answer(f"Qancha {unit} qo'shamiz? (ayirish uchun minus bilan: <code>-500</code>)")
    await call.answer()


@admin_router.message(Adm.give)
async def a_give_apply(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    d = await state.get_data()
    uid = d["uid"]
    await state.clear()
    if d["mode"] == "msg":
        ok = await notify(uid, f"📩 <b>Admindan xabar</b>\n\n{msg.text}")
        return await msg.answer("✅ Yuborildi" if ok else "❌ Yuborilmadi (bot bloklangan)")
    val = int(re.sub(r"[^\d-]", "", msg.text or "") or 0)
    if not val:
        return await msg.answer("❌ Noto'g'ri son.")
    if d["mode"] == "ball":
        pts = await db.add_points(uid, val)
        await db.log_transaction(uid, "admin", val, "admin", "ball")
        await notify(uid, f"🎁 Adminstrator sizga <b>{val:+} ball</b> berdi!\n💎 Joriy: <b>{money(pts)}</b>")
    else:
        await db.update_balance(uid, val)
        await db.log_transaction(uid, "admin", val, "admin")
        u = await db.get_user(uid)
        await notify(uid, f"💰 Hisobingiz o'zgardi: <b>{val:+} so'm</b>\nJoriy: <b>{money(u['balance'])} so'm</b>")
    await msg.answer("✅ Bajarildi")
    await show_user_card(msg, uid)


@admin_router.callback_query(F.data.startswith("azero:"))
async def a_zero(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    await db.set_points(uid, 0)
    await show_user_card(call, uid, edit=True)
    await call.answer("Ball nolga tushirildi")


@admin_router.callback_query(F.data.startswith("aban:"))
async def a_ban(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    uid = int(call.data.split(":")[1])
    u = await db.get_user(uid)
    await db.set_blocked(uid, not u["is_blocked"])
    await show_user_card(call, uid, edit=True)
    await call.answer("Holat o'zgartirildi")


# ─── BALL PAKETLARI ────────────────────────────────────────────
@admin_router.callback_query(F.data == "a_packs")
async def a_packs(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    rows = await db.packages(only_active=False)
    text = "💎 <b>Ball paketlari</b>\n\nO'zgartirish uchun paketni tanlang:"
    m = [[(f"{'🟢' if p['active'] else '⚪️'} {p['title']}: {money(p['points'])} ball / {money(p['price'])} so'm",
           f"ap:{p['id']}")] for p in rows]
    m += [[("➕ Yangi paket", "ap_new")], [("⬅️ Orqaga", "a_home")]]
    await call.message.edit_text(text, reply_markup=kb(m))
    await call.answer()


@admin_router.callback_query(F.data.startswith("ap:"))
async def a_pack_one(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    pid = int(call.data.split(":")[1])
    p = await db.get_package(pid)
    await call.message.edit_text(
        f"💎 <b>{p['title']}</b>\n\n"
        f"Ball: <b>{money(p['points'])}</b>\nNarx: <b>{money(p['price'])} so'm</b>\n"
        f"Yorliq: <b>{p['badge'] or '—'}</b>\nHolat: <b>{'faol' if p['active'] else 'o`chirilgan'}</b>",
        reply_markup=kb([
            [("✏️ Nomi", f"ape:title:{pid}"), ("💎 Ball", f"ape:points:{pid}")],
            [("💰 Narx", f"ape:price:{pid}"), ("🏷 Yorliq", f"ape:badge:{pid}")],
            [("🔁 Yoqish/o'chirish", f"aptog:{pid}"), ("🗑 O'chirish", f"apdel:{pid}")],
            [("⬅️ Orqaga", "a_packs")]]))
    await call.answer()


@admin_router.callback_query(F.data.startswith("aptog:"))
async def a_pack_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    pid = int(call.data.split(":")[1])
    p = await db.get_package(pid)
    await db.update_package(pid, active=not p["active"])
    await a_pack_one(call)


@admin_router.callback_query(F.data.startswith("apdel:"))
async def a_pack_del(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await db.del_package(int(call.data.split(":")[1]))
    await call.answer("O'chirildi")
    await a_packs(call, state)


@admin_router.callback_query(F.data.startswith("ape:"))
async def a_pack_edit(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    _, field, pid = call.data.split(":")
    await state.set_state(Adm.pack)
    await state.update_data(field=field, pid=int(pid))
    await call.message.answer(f"Yangi qiymatni yuboring ({field}):")
    await call.answer()


@admin_router.message(Adm.pack)
async def a_pack_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    d = await state.get_data()
    await state.clear()
    val = msg.text.strip()
    if d.get("new"):
        parts = [p.strip() for p in val.split("|")]
        if len(parts) < 3:
            return await msg.answer("❌ Format: <code>Nomi | ball | narx | yorliq</code>")
        await db.add_package(parts[0], int(re.sub(r"\D", "", parts[1])), int(re.sub(r"\D", "", parts[2])),
                             0, parts[3] if len(parts) > 3 else "")
        return await msg.answer("✅ Paket qo'shildi", reply_markup=kb([[("⬅️ Paketlar", "a_packs")]]))
    field = d["field"]
    v = val if field in ("title", "badge") else int(re.sub(r"\D", "", val) or 0)
    await db.update_package(d["pid"], **{field: v})
    await msg.answer("✅ Saqlandi", reply_markup=kb([[("⬅️ Paketlar", "a_packs")]]))


@admin_router.callback_query(F.data == "ap_new")
async def a_pack_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(Adm.pack)
    await state.update_data(new=True)
    await call.message.answer(
        "➕ Yangi paket.\nFormat: <code>Nomi | ball | narx | yorliq</code>\n"
        "Masalan: <code>Mega | 3000 | 90000 | TOP</code>")
    await call.answer()


# ─── O'YIN SOZLAMALARI (universal tahrirlagich) ────────────────
GAME_FIELDS = [
    ("win_reward_easy", "🤖 Oson bot g'alabasi (ball)"),
    ("win_reward_mid", "🤖 O'rta bot g'alabasi (ball)"),
    ("win_reward_hard", "🤖 Qiyin bot g'alabasi (ball)"),
    ("win_reward_human", "🧑\u200d🤝\u200d🧑 Odam bilan g'alaba (ball)"),
    ("minutes_pvp", "⏱ Odam bilan o'yin vaqti (daqiqa)"),
    ("elo_win", "📈 G'alabada ELO"),
    ("elo_lose", "📉 Mag'lubiyatda ELO"),
    ("price_undo", "↩️ Xodni qaytarish narxi"),
    ("price_king", "👑 Damka narxi"),
    ("price_extra", "➕ Qo'shimcha xod narxi"),
    ("price_hint", "💡 Maslahat narxi"),
    ("price_theme", "🎨 Taxta dizayni narxi"),
    ("price_skin", "⚪️ Dona to'plami narxi"),
    ("start_points", "🎁 Yangi o'yinchiga ball"),
    ("referral_points", "👥 Referal bonusi (ball)"),
    ("daily_limit_bot", "🛡 Kunlik mukofotli o'yin limiti"),
    ("tournament_win_score", "🏆 Chempionat: g'alaba ochkosi"),
    ("tournament_draw_score", "🏆 Chempionat: durang ochkosi"),
]
PAY_FIELDS = [
    ("card_number", "💳 Karta raqami"),
    ("card_owner", "👤 Karta egasi"),
    ("min_topup", "⬇️ Eng kam to'ldirish (so'm)"),
    ("min_withdraw", "🏧 Eng kam pul yechish (so'm)"),
    ("auto_pay_offset", "🎲 Summa farqi (1..N so'm)"),
    ("auto_pay_expiry", "⏳ To'lov muddati (daqiqa)"),
    ("tolov_shop_id", "🔑 TolovAPI shop_id"),
    ("tolov_shop_key", "🔑 TolovAPI shop_key"),
]


async def render_fields(call, fields, title, back="a_home", prefix="af"):
    s = await db.all_settings()
    lines = "\n".join(f"{label}: <b>{s.get(key, '—')}</b>" for key, label in fields)
    m = [[(label, f"{prefix}:{key}")] for key, label in fields]
    m += [[("⬅️ Orqaga", back)]]
    await call.message.edit_text(f"{title}\n\n{lines}\n\nO'zgartirish uchun tanlang:", reply_markup=kb(m))


@admin_router.callback_query(F.data == "a_game")
async def a_game(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    await render_fields(call, GAME_FIELDS, "🎮 <b>O'yin sozlamalari</b>")
    await call.answer()


@admin_router.callback_query(F.data == "a_pay")
async def a_pay(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    auto = await S("auto_pay_enabled", 1)
    s = await db.all_settings()
    lines = "\n".join(f"{label}: <b>{s.get(key) or '—'}</b>" for key, label in PAY_FIELDS)
    m = [[(label, f"af:{key}")] for key, label in PAY_FIELDS]
    m = [[("⚡️ Avto to'lov: " + ("YOQILGAN ✅" if auto else "O'CHIQ ❌"), "a_autotog")]] + m
    m += [[("⬅️ Orqaga", "a_home")]]
    await call.message.edit_text(f"💳 <b>To'lov sozlamalari</b>\n\n{lines}", reply_markup=kb(m))
    await call.answer()


@admin_router.callback_query(F.data == "a_autotog")
async def a_auto_toggle(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await db.set_setting("auto_pay_enabled", 0 if await S("auto_pay_enabled", 1) else 1)
    await a_pay(call, state)


@admin_router.callback_query(F.data.startswith("af:"))
async def a_field(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    key = call.data.split(":", 1)[1]
    cur = await db.get_setting(key)
    await state.set_state(Adm.value)
    await state.update_data(key=key)
    await call.message.answer(f"🔧 <code>{key}</code>\nJoriy qiymat: <b>{cur or '—'}</b>\n\nYangi qiymatni yuboring:")
    await call.answer()


@admin_router.message(Adm.value)
async def a_field_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    d = await state.get_data()
    await state.clear()
    key = d["key"]
    val = msg.text.strip()
    if key not in ("card_number", "card_owner", "tolov_shop_id", "tolov_shop_key"):
        val = re.sub(r"\D", "", val) or "0"
    await db.set_setting(key, val)
    back = "a_pay" if key in dict(PAY_FIELDS) else "a_game"
    await msg.answer(f"✅ <code>{key}</code> = <b>{val}</b>", reply_markup=kb([[("⬅️ Orqaga", back)]]))


@admin_router.callback_query(F.data == "a_maint")
async def a_maint(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    now = await S("maintenance", 0)
    await db.set_setting("maintenance", 0 if now else 1)
    await call.answer("Texnik rejim " + ("o'chirildi" if now else "yoqildi"), show_alert=True)
    await admin_home(call, edit=True)


# ─── E'LONLAR (Mini App ichida ko'rinadi) ──────────────────────
@admin_router.callback_query(F.data == "a_ann")
async def a_ann(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    rows = await db.announcements(only_active=False)
    m = [[(f"{'🟢' if r['active'] else '⚪️'} {r['emoji']} {(r['title'] or r['text'])[:28]}", f"aan:{r['id']}")]
         for r in rows]
    m += [[("➕ Yangi e'lon", "aan_new")], [("⬅️ Orqaga", "a_home")]]
    await call.message.edit_text(
        "📣 <b>E'lonlar</b>\n\nBu e'lonlar o'yin ichida (Mini App bosh sahifasida) "
        "ko'zga tashlanadigan joyda chiqadi.", reply_markup=kb(m))
    await call.answer()


@admin_router.callback_query(F.data == "aan_new")
async def a_ann_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(Adm.ann)
    await call.message.answer(
        "📣 <b>Yangi e'lon</b>\n\nFormat:\n<code>Sarlavha | Matn | emoji | turi</code>\n\n"
        "turi: <code>prize</code> (oltin), <code>info</code> (ko'k), <code>warning</code> (qizil)\n\n"
        "Masalan:\n<code>Haftalik chempionat | 1-o'rin 500 000 so'm! Yakshanbagacha | 🏆 | prize</code>")
    await call.answer()


@admin_router.message(Adm.ann)
async def a_ann_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    p = [x.strip() for x in msg.text.split("|")]
    title = p[0]
    text = p[1] if len(p) > 1 else ""
    emoji = p[2] if len(p) > 2 else "📣"
    kind = p[3] if len(p) > 3 else "info"
    await db.add_announcement(title, text, kind, emoji, pinned=(kind == "prize"))
    await msg.answer("✅ E'lon qo'shildi — barcha o'yinchilar ilovada ko'radi.",
                     reply_markup=kb([[("📨 Botda ham yuborish", "aan_push")], [("⬅️ E'lonlar", "a_ann")]]))


@admin_router.callback_query(F.data == "aan_push")
async def a_ann_push(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    rows = await db.announcements(limit=1)
    if not rows:
        return await call.answer("E'lon yo'q")
    a = rows[0]
    await call.answer("Yuborilmoqda…")
    await do_broadcast(f"{a['emoji']} <b>{a['title']}</b>\n\n{a['text']}", call.from_user.id)


@admin_router.callback_query(F.data.startswith("aan:"))
async def a_ann_one(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    aid = int(call.data.split(":")[1])
    await db.toggle_announcement(aid)
    await call.answer("Holat o'zgartirildi")
    await a_ann(call, state)


# ─── CHEMPIONAT ────────────────────────────────────────────────
@admin_router.callback_query(F.data == "a_tour")
async def a_tour(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    t = await db.active_tournament()
    if t:
        top = await db.tournament_top(t["id"], 10)
        lines = "\n".join(f"{i+1}. {r['fullname'][:18]} — {r['score']} ochko ({r['wins']}W)"
                          for i, r in enumerate(top)) or "Ishtirokchi yo'q"
        text = (f"🏆 <b>{t['title']}</b>\n\n{t['descr']}\n\n🎁 {t['prize_text']}\n"
                f"⏳ Tugashiga: <b>{left_text(t['ends_at'])}</b>\n\n<b>TOP-10</b>\n{lines}")
        m = [[("🏁 Yakunlash va mukofotlash", f"atfin:{t['id']}")],
             [("📨 Eslatma yuborish", f"atpush:{t['id']}")],
             [("⬅️ Orqaga", "a_home")]]
    else:
        text = ("🏆 <b>Chempionat</b>\n\nHozir faol chempionat yo'q.\n\n"
                "Yangi chempionat yaratsangiz — barcha o'yinchilar ilovada va botda ko'radi.")
        m = [[("➕ Yangi chempionat", "at_new")],
             [("💸 To'lovlar ro'yxati", "at_payouts")],
             [("⬅️ Orqaga", "a_home")]]
    await call.message.edit_text(text, reply_markup=kb(m))
    await call.answer()


@admin_router.callback_query(F.data == "at_new")
async def a_tour_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(Adm.tour)
    await call.message.answer(
        "🏆 <b>Yangi chempionat</b>\n\nBitta xabarda yuboring:\n\n"
        "<code>Nomi | Tavsif | Necha kun | 1:500000, 2:300000, 3:150000</code>\n\n"
        "Masalan:\n<code>Haftalik chempionat | Eng ko'p g'alaba qozongan 3 kishi pul mukofoti oladi | 7 | "
        "1:500000, 2:300000, 3:150000</code>")
    await call.answer()


@admin_router.message(Adm.tour)
async def a_tour_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    p = [x.strip() for x in msg.text.split("|")]
    if len(p) < 4:
        return await msg.answer("❌ Format noto'g'ri. Qaytadan urinib ko'ring.")
    title, descr = p[0], p[1]
    days = int(re.sub(r"\D", "", p[2]) or 7)
    prizes = []
    for m_ in re.finditer(r"(\d+)\s*:\s*(\d+)", p[3]):
        prizes.append({"place": int(m_.group(1)), "amount": int(m_.group(2))})
    prize_text = "\n".join(f"{'🥇🥈🥉'[x['place']-1] if x['place'] <= 3 else str(x['place'])+'.'} "
                           f"{money(x['amount'])} so'm" for x in prizes)
    ends = datetime.now(timezone.utc) + timedelta(days=days)
    tid = await db.create_tournament(title, descr, prize_text, prizes, ends)
    await db.add_announcement(title, f"{descr}\n\n{prize_text}", "prize", "🏆", pinned=True)
    await msg.answer(
        f"✅ <b>{title}</b> yaratildi!\n\n{prize_text}\n⏳ {days} kun\n\n"
        f"Ilovada e'lon avtomatik chiqdi. Botda ham xabar yuboraymi?",
        reply_markup=kb([[("📨 Hammaga yuborish", f"atpush:{tid}")], [("⬅️ Chempionat", "a_tour")]]))


@admin_router.callback_query(F.data.startswith("atpush:"))
async def a_tour_push(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    t = await db.active_tournament()
    if not t:
        return await call.answer("Faol chempionat yo'q")
    await call.answer("Yuborilmoqda…")
    await do_broadcast(
        f"🏆 <b>{t['title']}</b>\n\n{t['descr']}\n\n🎁 <b>Mukofotlar:</b>\n{t['prize_text']}\n\n"
        f"⏳ Tugashiga: <b>{left_text(t['ends_at'])}</b>\n\nHoziroq o'ynang va TOPga chiqing! 🎮",
        call.from_user.id)


@admin_router.callback_query(F.data.startswith("atfin:"))
async def a_tour_finish(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    tid = int(call.data.split(":")[1])
    await finish_tournament(tid, announced_by=call.from_user.id)
    await call.answer("Yakunlandi ✅", show_alert=True)
    await admin_home(call, edit=True)


async def finish_tournament(tid: int, announced_by: int | None = None):
    async with db.pool.acquire() as conn:
        t = await conn.fetchrow("SELECT * FROM tournaments WHERE id=$1", tid)
    if not t or t["status"] != "active":
        return
    prizes = json.loads(t["prizes"] or "[]")
    top = await db.tournament_top(tid, max([p["place"] for p in prizes] or [3]))
    await db.finish_tournament(tid)
    lines = []
    for p in prizes:
        i = p["place"] - 1
        if i < len(top):
            w = top[i]
            await db.add_payout(tid, w["user_id"], p["place"], p["amount"])
            await db.update_balance(w["user_id"], p["amount"])
            await db.log_transaction(w["user_id"], "prize", p["amount"], f"tournament:{tid}")
            await notify(w["user_id"],
                         f"🎉 <b>Tabriklaymiz!</b>\n\n«{t['title']}» chempionatida <b>{p['place']}-o'rin</b>ni "
                         f"egalladingiz!\n\n💰 Mukofot: <b>{money(p['amount'])} so'm</b> hisobingizga qo'shildi.\n"
                         f"Pulni yechish uchun admin bilan bog'laning.")
            lines.append(f"{'🥇🥈🥉'[i] if i < 3 else str(p['place'])+'.'} {w['fullname']} — {money(p['amount'])} so'm")
    text = (f"🏁 <b>«{t['title']}» yakunlandi!</b>\n\n<b>G'oliblar:</b>\n" + ("\n".join(lines) or "—") +
            "\n\nBarchaga rahmat! Keyingi chempionat tez orada 🏆")
    await db.add_announcement(f"{t['title']} yakunlandi", "\n".join(lines) or "—", "prize", "🏁", pinned=True)
    await do_broadcast(text, announced_by or ADMIN_ID)


@admin_router.callback_query(F.data == "at_payouts")
async def a_payouts(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    rows = await db.payouts(limit=20)
    if not rows:
        return await call.answer("To'lovlar yo'q", show_alert=True)
    m = []
    txt = "💸 <b>Mukofot to'lovlari</b>\n\n"
    for r in rows:
        u = await db.get_user(r["user_id"])
        txt += (f"{'✅' if r['status'] == 'paid' else '⏳'} {r['place']}-o'rin · "
                f"{(u['fullname'] if u else r['user_id'])} — {money(r['amount'])} so'm\n")
        if r["status"] != "paid":
            m.append([(f"✅ To'landi: {r['id']}", f"appaid:{r['id']}")])
    m.append([("⬅️ Orqaga", "a_tour")])
    await call.message.edit_text(txt, reply_markup=kb(m))
    await call.answer()


@admin_router.callback_query(F.data.startswith("appaid:"))
async def a_payout_paid(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await db.mark_payout_paid(int(call.data.split(":")[1]))
    await call.answer("Belgilandi")
    await a_payouts(call)


# ─── MAJBURIY OBUNA KANALLARI ──────────────────────────────────
@admin_router.callback_query(F.data == "a_channels")
async def a_channels(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    rows = await db.list_required_channels(only_active=False)
    text = "📢 <b>Majburiy obuna kanallari</b>\n\n"
    text += ("Bu kanallarga obuna bo'lmagan foydalanuvchi botdan foydalana olmaydi "
             "(Mini App ham shu qoidaga bo'ysunadi)." if rows else
             "Hozircha kanal yo'q — foydalanuvchilar erkin kirishadi.")
    m = [[(f"{'🟢' if r['active'] else '⚪️'} {r['title'] or r['username'] or r['id']}", f"ach:{r['id']}")]
         for r in rows]
    m += [[("➕ Kanal qo'shish", "ach_new")], [("⬅️ Orqaga", "a_home")]]
    await call.message.edit_text(text, reply_markup=kb(m))
    await call.answer()


@admin_router.callback_query(F.data == "ach_new")
async def a_channel_new(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(Adm.channel)
    await call.message.answer(
        "➕ <b>Kanal qo'shish</b>\n\n"
        "Kanalning <b>@username</b>ini yozing (ochiq kanal uchun) YOKI shu kanaldagi "
        "istalgan postni shu yerga <b>forward</b> qiling (yopiq/xususiy kanal uchun ham ishlaydi).\n\n"
        "⚠️ Muhim: botni albatta o'sha kanalga <b>admin</b> qilib qo'shing — aks holda "
        "bot obunani tekshira olmaydi va bu kanal talabdan avtomatik chetlab o'tiladi.")
    await call.answer()


@admin_router.message(Adm.channel)
async def a_channel_save(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    chat_id = None
    username = ""
    title = ""
    invite = ""
    if msg.forward_from_chat:
        chat_id = msg.forward_from_chat.id
        username = msg.forward_from_chat.username or ""
        title = msg.forward_from_chat.title or username or str(chat_id)
    else:
        uname = (msg.text or "").strip().lstrip("@")
        if "t.me/" in uname:
            uname = uname.split("t.me/")[-1]
        if not uname:
            return await msg.answer("❌ Bo'sh qiymat. Qaytadan urinib ko'ring.")
        try:
            chat = await bot.get_chat("@" + uname)
            chat_id = chat.id
            username = chat.username or uname
            title = chat.title or uname
        except Exception as e:
            return await msg.answer(f"❌ Kanal topilmadi: <code>{e}</code>\n\n"
                                    "@username to'g'riligiga va kanal ochiqligiga ishonch hosil qiling, "
                                    "yoki o'sha kanaldan bitta postni forward qiling.")
    if username:
        invite = f"https://t.me/{username}"
    cid = await db.add_required_channel(username, title, invite, chat_id)
    await msg.answer(
        f"✅ <b>{title}</b> majburiy obuna ro'yxatiga qo'shildi.\n\n"
        "Botni shu kanalga admin qilishni unutmang, aks holda tekshiruv ishlamaydi.",
        reply_markup=kb([[("⬅️ Kanallar", "a_channels")]]))


@admin_router.callback_query(F.data.startswith("ach:"))
async def a_channel_one(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    cid = int(call.data.split(":")[1])
    ch = await db.get_required_channel(cid)
    if not ch:
        return await call.answer("Topilmadi", show_alert=True)
    await call.message.edit_text(
        f"📢 <b>{ch['title'] or ch['username']}</b>\n\n"
        f"Username: <b>{'@'+ch['username'] if ch['username'] else '—'}</b>\n"
        f"Chat ID: <code>{ch['chat_id']}</code>\n"
        f"Holat: <b>{'faol' if ch['active'] else 'o`chirilgan'}</b>",
        reply_markup=kb([
            [("🔁 Yoqish/o'chirish", f"achtog:{cid}"), ("🗑 O'chirish", f"achdel:{cid}")],
            [("⬅️ Orqaga", "a_channels")]]))
    await call.answer()


@admin_router.callback_query(F.data.startswith("achtog:"))
async def a_channel_toggle(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    await db.toggle_required_channel(int(call.data.split(":")[1]))
    await a_channel_one(call)


@admin_router.callback_query(F.data.startswith("achdel:"))
async def a_channel_del(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await db.delete_required_channel(int(call.data.split(":")[1]))
    await call.answer("O'chirildi")
    await a_channels(call, state)


# ─── QO'SHIMCHA ADMINLAR ────────────────────────────────────────
@admin_router.callback_query(F.data == "a_admins")
async def a_admins(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    extra = await db.list_admins()
    text = "👤 <b>Adminlar</b>\n\n<b>Asosiy (o'zgartirib bo'lmaydi):</b>\n"
    text += "\n".join(f"• <code>{aid}</code>" for aid in ADMIN_IDS) or "—"
    text += "\n\n<b>Qo'shimcha (botdan qo'shilgan):</b>\n"
    text += "\n".join(f"• {a['fullname'] or a['user_id']} (<code>{a['user_id']}</code>)" for a in extra) or "— yo'q"
    m = []
    if is_root_admin(call.from_user.id):
        m.append([("➕ Admin qo'shish", "aad_new")])
        m += [[(f"🗑 {a['fullname'] or a['user_id']}", f"aad_del:{a['user_id']}")] for a in extra]
    m.append([("⬅️ Orqaga", "a_home")])
    await call.message.edit_text(text, reply_markup=kb(m))
    await call.answer()


@admin_router.callback_query(F.data == "aad_new")
async def a_admin_new(call: CallbackQuery, state: FSMContext):
    if not is_root_admin(call.from_user.id):
        return await call.answer("Faqat asosiy admin qo'sha oladi", show_alert=True)
    await state.set_state(Adm.admin_add)
    await call.message.answer(
        "➕ <b>Yangi admin qo'shish</b>\n\n"
        "Foydalanuvchining Telegram ID raqamini yuboring, YOKI undan kelgan "
        "istalgan xabarni shu yerga <b>forward</b> qiling.")
    await call.answer()


@admin_router.message(Adm.admin_add)
async def a_admin_save(msg: Message, state: FSMContext):
    if not is_root_admin(msg.from_user.id):
        return
    await state.clear()
    uid = None
    fname = ""
    if msg.forward_from:
        uid = msg.forward_from.id
        fname = msg.forward_from.full_name
    elif (msg.text or "").strip().isdigit():
        uid = int(msg.text.strip())
    if not uid:
        return await msg.answer("❌ ID topilmadi. Raqam yuboring yoki xabarni forward qiling.")
    if not fname:
        u = await db.get_user(uid)
        fname = (u["fullname"] if u else "") or str(uid)
    await db.add_admin(uid, fname, msg.from_user.id)
    EXTRA_ADMIN_IDS.add(uid)
    await msg.answer(f"✅ <b>{fname}</b> endi admin.", reply_markup=kb([[("⬅️ Adminlar", "a_admins")]]))
    await notify(uid, "🛠 Sizga bot boshqaruvida <b>admin huquqi</b> berildi. /admin buyrug'i orqali kiring.")


@admin_router.callback_query(F.data.startswith("aad_del:"))
async def a_admin_del(call: CallbackQuery, state: FSMContext):
    if not is_root_admin(call.from_user.id):
        return await call.answer("Faqat asosiy admin o'chira oladi", show_alert=True)
    uid = int(call.data.split(":")[1])
    await db.remove_admin(uid)
    EXTRA_ADMIN_IDS.discard(uid)
    await notify(uid, "🛠 Sizning admin huquqingiz bekor qilindi.")
    await call.answer("O'chirildi")
    await a_admins(call, state)


# ─── PULNI KARTAGA YECHISH ──────────────────────────────────────
@admin_router.callback_query(F.data == "a_withdraw")
async def a_withdraw(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.clear()
    rows = await db.list_withdrawals("pending")
    text = "💸 <b>Pul yechish so'rovlari</b>\n\n"
    if not rows:
        text += "Hozircha kutilayotgan so'rov yo'q."
    m = []
    for r in rows:
        u = await db.get_user(r["user_id"])
        text += (f"\n👤 {(u['fullname'] if u else r['user_id'])} — <b>{money(r['amount'])} so'm</b>\n"
                 f"💳 <code>{r['card']}</code>")
        m.append([("✅ To'landi: " + str(r["id"]), f"wdpaid:{r['id']}"),
                  ("❌ Rad etish: " + str(r["id"]), f"wdrej:{r['id']}")])
    m.append([("📜 Tarix (oxirgi 10)", "a_withdraw_hist")])
    m.append([("⬅️ Orqaga", "a_home")])
    await call.message.edit_text(text, reply_markup=kb(m))
    await call.answer()


@admin_router.callback_query(F.data == "a_withdraw_hist")
async def a_withdraw_hist(call: CallbackQuery):
    if not is_admin(call.from_user.id):
        return
    rows = await db.list_withdrawals(limit=10)
    if not rows:
        return await call.answer("Tarix bo'sh", show_alert=True)
    text = "📜 <b>Yechish tarixi</b>\n\n"
    icon = {"paid": "✅", "rejected": "❌", "pending": "⏳"}
    for r in rows:
        u = await db.get_user(r["user_id"])
        text += (f"{icon.get(r['status'],'•')} {(u['fullname'] if u else r['user_id'])} — "
                 f"{money(r['amount'])} so'm — <code>{r['card']}</code>\n")
    await call.message.edit_text(text, reply_markup=kb([[("⬅️ Orqaga", "a_withdraw")]]))
    await call.answer()


@admin_router.callback_query(F.data.startswith("wdpaid:"))
async def a_withdraw_paid(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    wid = int(call.data.split(":")[1])
    w = await db.get_withdrawal(wid)
    if not w or w["status"] != "pending":
        return await call.answer("Bu so'rov allaqachon ko'rib chiqilgan", show_alert=True)
    await db.set_withdrawal_status(wid, "paid")
    await notify(w["user_id"],
                 f"✅ <b>{money(w['amount'])} so'm</b> kartangizga (<code>{w['card']}</code>) o'tkazildi. Rahmat!")
    await call.answer("To'landi deb belgilandi ✅")
    await a_withdraw(call, state)


@admin_router.callback_query(F.data.startswith("wdrej:"))
async def a_withdraw_reject(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    wid = int(call.data.split(":")[1])
    w = await db.get_withdrawal(wid)
    if not w or w["status"] != "pending":
        return await call.answer("Bu so'rov allaqachon ko'rib chiqilgan", show_alert=True)
    await db.set_withdrawal_status(wid, "rejected")
    await db.update_balance(w["user_id"], w["amount"])  # pulni qaytarish
    await db.log_transaction(w["user_id"], "withdraw_reject", w["amount"], f"wd:{wid}")
    await notify(w["user_id"],
                 f"❌ Pul yechish so'rovingiz (<b>{money(w['amount'])} so'm</b>) rad etildi. "
                 f"Mablag' hisobingizga qaytarildi. Savol bo'lsa admin bilan bog'laning.")
    await call.answer("Rad etildi, pul qaytarildi")
    await a_withdraw(call, state)


# ─── BROADCAST ─────────────────────────────────────────────────
@admin_router.callback_query(F.data == "a_bcast")
async def a_bcast(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return
    await state.set_state(Adm.broadcast)
    await call.message.answer("📨 Yuboriladigan xabarni yozing (HTML qo'llab-quvvatlanadi):")
    await call.answer()


@admin_router.message(Adm.broadcast)
async def a_bcast_send(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.clear()
    await do_broadcast(msg.html_text, msg.from_user.id)


async def do_broadcast(text: str, admin: int):
    ids = await db.all_user_ids()
    sent = fail = 0
    m = kb([[("🎮 O'ynash", "webapp:" + WEBAPP_URL)]]) if _webapp_url_ok() else None
    for uid in ids:
        if await notify(uid, text, m):
            sent += 1
        else:
            fail += 1
        await asyncio.sleep(0.05)
    await notify(admin, f"📨 Yuborildi: <b>{sent}</b> ta\n❌ Yetib bormadi: <b>{fail}</b> ta")


# ══════════════════════════════════════════════════════════════
#                     FON JARAYONLARI + START
# ══════════════════════════════════════════════════════════════
async def tournament_watcher():
    """Muddati tugagan chempionatlarni avtomatik yakunlaydi va e'lon qiladi."""
    while True:
        try:
            async with db.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id FROM tournaments WHERE status='active' AND ends_at <= NOW()")
            for r in rows:
                await finish_tournament(r["id"])
        except Exception as e:
            logger.exception("tournament_watcher: %s", e)
        await asyncio.sleep(120)


async def get_tolov_config():
    return {"enabled": bool(await S("auto_pay_enabled", 1)),
            "api_url": (await db.get_setting("tolov_api_url")) or tolov_api.TOLOV_API_URL_DEFAULT,
            "shop_id": await db.get_setting("tolov_shop_id") or "",
            "shop_key": await db.get_setting("tolov_shop_key") or ""}


def E(slug: str) -> str:
    """humo_listener/tolov_api modullari kutadigan emoji funksiyasi."""
    return {"check": "✅", "money": "💰", "wallet": "👛", "party": "🎉", "profile": "👤",
            "rocket": "🚀", "search": "🔍"}.get(slug, "")


async def on_start():
    await db.init()
    me = await bot.get_me()
    BOT_USERNAME["v"] = me.username
    await db.set_setting("bot_username", me.username)
    logger.info("Bot ishga tushdi: @%s", me.username)

    EXTRA_ADMIN_IDS.clear()
    EXTRA_ADMIN_IDS.update(r["user_id"] for r in await db.list_admins())
    if EXTRA_ADMIN_IDS:
        logger.info("Qo'shimcha adminlar yuklandi: %s", EXTRA_ADMIN_IDS)

    if not _webapp_url_ok():
        logger.error(
            "=" * 60 + "\n"
            "WEBAPP_URL NOTO'G'RI!\n"
            f"Hozirgi qiymat: {WEBAPP_URL!r}\n"
            "Bu Railway'ning ICHKI (.railway.internal) manzili yoki bo'sh/https"
            "siz qiymat bo'lishi mumkin. Telegram web_app tugmasi FAQAT ochiq\n"
            "https://... manzilni qabul qiladi. Railway → Settings → Networking\n"
            "→ 'Public Networking' → Generate Domain → chiqqan https://...\n"
            "manzilni Variables → WEBAPP_URL ga qo'ying.\n"
            "Bu tuzatilmaguncha /start tugmasi 'O'ynash' o'rniga ogohlantirish\n"
            "matni bilan chiqadi (lekin bot boshqa hamma narsada ishlayveradi)."
            + "\n" + "=" * 60)

    # Avtomatik to'lov: TolovAPI polling
    asyncio.create_task(tolov_api.tolov_polling_loop(
        db, bot, ADMIN_ID, E, get_tolov_config, humo_listener.process_deposit_amount, log_event))
    # Avtomatik to'lov: HUMOcard userbot (agar .env da sozlangan bo'lsa)
    if getattr(humo_listener, "HUMO_ENABLED", False):
        asyncio.create_task(humo_listener.start_humo_listener(db, bot, ADMIN_ID, E, log_event))
    asyncio.create_task(tournament_watcher())

    for aid in all_admin_ids():
        msg = "♟ <b>Shashka Arena</b> ishga tushdi.\n/admin — boshqaruv paneli"
        if not _webapp_url_ok():
            msg += (f"\n\n⚠️ <b>Diqqat:</b> WEBAPP_URL noto'g'ri sozlangan "
                    f"(<code>{WEBAPP_URL or '—'}</code>). Foydalanuvchilar /start bosganda "
                    "'O'ynash' tugmasi ishlamaydi. Railway → Settings → Networking → "
                    "Public Networking → Generate Domain, so'ng shu https:// manzilni "
                    "Variables → WEBAPP_URL ga qo'ying.")
        await notify(aid, msg)


@dp.errors()
async def global_error_handler(event):
    """Har qanday kutilmagan xato ushlanadi: foydalanuvchi butunlay
    javobsiz qolmasligi, admin esa Railway logiga kirmasdan ham xatoni
    darhol Telegram'da ko'rishi uchun."""
    exc = event.exception
    logger.exception("Kutilmagan xato: %s", exc)
    try:
        upd = event.update
        chat_id = None
        if upd.message:
            chat_id = upd.message.chat.id
        elif upd.callback_query and upd.callback_query.message:
            chat_id = upd.callback_query.message.chat.id
        if chat_id and not is_admin(chat_id):
            await bot.send_message(chat_id, "⚠️ Kutilmagan xato yuz berdi. Iltimos, qayta urinib ko'ring "
                                            "yoki /start bosing.")
    except Exception:
        pass
    for aid in all_admin_ids():
        try:
            await bot.send_message(aid, f"🛑 <b>Bot xatosi:</b>\n<code>{type(exc).__name__}: {exc}</code>")
        except Exception:
            pass
    return True


@web.middleware
async def json_error_middleware(request, handler):
    """Har qanday kutilmagan server xatosi Mini App'ga HAR DOIM to'g'ri
    JSON ko'rinishida qaytadi — aiohttp'ning standart HTML xato sahifasi
    emas. Aks holda mijoz `r.json()` chaqirganda xato beradi va bu
    "server bilan aloqa yo'q" degan noaniq xabar sifatida ko'rinadi,
    holbuki server ishlagan, faqat bitta so'rov ichida xato chiqqan."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        logger.exception("API xatosi %s: %s", request.path, e)
        return web.json_response({"ok": False, "error": "server"}, status=500)


async def main():
    dp.message.middleware(Guard())
    dp.callback_query.middleware(Guard())
    dp.include_router(admin_router)
    dp.include_router(user_router)

    app = web.Application(middlewares=[json_error_middleware])
    webapp_api.setup_webapp_api(app, db, bot, ADMIN_ID, notify)

    await on_start()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logger.info("Mini App serveri: http://0.0.0.0:%s", PORT)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
