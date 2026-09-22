"""
humo_listener.py — Adminning shaxsiy Telegram akkaunti (session) orqali
@HUMOcardbot dan kelayotgan "To'ldirish" xabarlarini o'qib, botdagi
kutilayotgan avtomatik to'lovlar (auto_payments) bilan solishtiradi.
Mos summa topilsa — foydalanuvchi balansi avtomatik to'ldiriladi.

DIQQAT — bu ODDIY BOT EMAS, USERBOT (Telethon orqali shaxsiy akkaunt
sifatida ishlaydi). Shuning uchun:
  1) Faqat bitta joyda (bitta serverda) ishga tushirilishi kerak — aks
     holda Telegram akkauntni "bir nechta joyda anomal faollik" deb
     bloklashi mumkin.
  2) Session stringni hech kimga bermang, uni .env faylida saqlang va
     serverga faqat SSH orqali kirish huquqini cheklang.

O'rnatish:
    pip install telethon

Bir martalik sozlash — session string olish:
    python3 generate_session.py
    (telefon raqam + SMS kod so'raladi, oxirida uzun SESSION STRING chiqadi)

.env ga qo'shing:
    HUMO_API_ID=1234567
    HUMO_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    HUMO_SESSION_STRING=1BVtsO...  (generate_session.py dan olingan)
    HUMO_BOT_USERNAME=HUMOcardbot
"""
from __future__ import annotations  # Python 3.9 va undan eskilarida ham
                                     # "dict | None" kabi sintaksis ishlashi uchun

import os
import re
import asyncio
import logging
from datetime import datetime

logger = logging.getLogger("humo_listener")

HUMO_API_ID          = os.getenv("HUMO_API_ID", "")
HUMO_API_HASH         = os.getenv("HUMO_API_HASH", "")
HUMO_SESSION_STRING  = os.getenv("HUMO_SESSION_STRING", "")
HUMO_BOT_USERNAME    = os.getenv("HUMO_BOT_USERNAME", "HUMOcardbot")

HUMO_ENABLED = bool(HUMO_API_ID and HUMO_API_HASH and HUMO_SESSION_STRING)


def parse_humocard_message(text: str) -> dict | None:
    """
    HUMOcard xabarini tahlil qiladi. Namuna:

        🎉 To'ldirish
        ➕ 100.000,00 UZS
        📍 Alif uz h2h 075>Tosh
        🏦 HUMOCARD *2747
        🕐 20:16 27.07.2026
        💰 100.341,16 UZS

    DIQQAT: summa oldida ODDIY "+" emas, "➕" (U+2795, Heavy Plus Sign)
    emojisi keladi — bular butunlay boshqa belgilar! Xavfsizlik uchun
    ikkalasini ham (va manfiy tomon uchun "➖"/"-" ni ham) qabul qilamiz.

    Qaytaradi: {"type": "deposit"|"withdraw", "amount": int, "card_last": str}
    yoki mos kelmasa None.
    """
    if not text:
        return None
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]
    if not lines:
        return None

    header = lines[0].lower()
    # "To'ldirish" — HUMOcard uchun, "Kartaga o'tkazma" — ba'zi UzCard/boshqa
    # kartalar uchun ishlatiladigan muqobil sarlavha — ikkalasi ham pul KIRIM.
    is_deposit = any(k in header for k in ["to'ldirish", "to‘ldirish", "kartaga o'tkazma", "kartaga o‘tkazma"])
    is_withdraw = any(k in header for k in ["to'lov", "to‘lov"]) and not is_deposit
    if not (is_deposit or is_withdraw):
        return None

    PLUS_CHARS  = ("+", "➕")
    MINUS_CHARS = ("-", "➖")
    amount_line = next(
        (l for l in lines if "uzs" in l.lower() and l.strip().startswith(PLUS_CHARS + MINUS_CHARS)),
        None
    )
    if not amount_line:
        return None

    m = re.search(r"[+➕\-➖]\s*([\d\s.,]+)\s*UZS", amount_line, re.IGNORECASE)
    if not m:
        return None

    raw_num = m.group(1).strip()
    # "100.000,00" -> butun qismi "100.000" -> "100000"
    integer_part = raw_num.split(",")[0].replace(".", "").replace(" ", "")
    try:
        amount = int(integer_part)
    except ValueError:
        return None

    card_line = next((l for l in lines if "*" in l), "")
    card_m = re.search(r"\*\s*(\d+)", card_line)
    card_last = card_m.group(1) if card_m else ""

    return {
        "type": "deposit" if is_deposit else "withdraw",
        "amount": amount,
        "card_last": card_last,
    }


async def process_deposit_amount(db, bot, admin_id: int, E, amount: int, card_last: str = "",
                                  source: str = "unknown", log_event=None) -> bool:
    """
    Har qanday manbadan (Telethon tinglovchisi, webhook, yoki kelajakda
    boshqa integratsiya) kelgan pul kirimini kutilayotgan avtomatik
    to'lovlar bilan solishtiradi. Mos kelsa — balansni to'ldirib, ikkala
    tomonga (foydalanuvchi + admin) xabar yuboradi.

    log_event — ixtiyoriy async funksiya (bot.py dagi umumiy "hodisalar
    kanali"ga yozib qo'yish uchun); berilmasa, shunchaki o'tkazib yuboriladi.

    Ikki xil manba (masalan HUMOcard tinglovchisi VA tashqi webhook) bir
    xil summani ikki marta yuborib yubormasligi uchun (masalan ikkalasi
    ham ishlab tursa) — moslashtirish bazada FOR UPDATE SKIP LOCKED bilan
    himoyalangan, shuning uchun bitta to'lov faqat BIR marta hisoblanadi.

    Qaytaradi: True — agar mos to'lov topilib, hisoblansa; False — mos
    kelmasa (masalan bu summani hech kim kutmayotgan bo'lsa).
    """
    payment = await db.find_matching_auto_payment(amount, card_last)
    if not payment:
        # Diqqat: bu shart emas — bu yerga tushish demak, "kirim aniqlandi,
        # LEKIN shu summani kutayotgan hech kim yo'q" degani (masalan
        # tasodifiy/eski to'lov, yoki band muddati allaqachon tugagan).
        # Buni ham kanalga yozamiz — shunda admin tizim chindan ishlab
        # turganini (shunchaki mos kelmayotganini) aniq ko'radi.
        if log_event:
            try:
                await log_event(
                    f"{E('search')} <b>Kirim aniqlandi, lekin mos to'lov topilmadi</b> ({source})\n\n"
                    f"{E('money')} Summa: {amount:,} so'm\n"
                    f"💳 Karta: *{card_last or '?'}\n\n"
                    f"<i>Bu odatiy holat bo'lishi mumkin (masalan eski/tasodifiy pul o'tkazma, "
                    f"yoki reservatsiya muddati tugagan). Agar foydalanuvchi shu summani "
                    f"KUTAYOTGAN bo'lsa va bu xabar chiqsa — band muddati juda qisqa bo'lishi mumkin.</i>"
                )
            except Exception:
                pass
        return False

    await db.complete_auto_payment(payment["id"], card_used=card_last)
    await db.update_balance(payment["user_id"], payment["final_amount"])
    await db.log_transaction(
        payment["user_id"], "topup", payment["final_amount"],
        note=f"auto:{source}:{card_last}"
    )
    user = await db.get_user(payment["user_id"])
    new_balance = user["balance"] if user else payment["final_amount"]

    try:
        await bot.send_message(
            payment["user_id"],
            f"{E('check')} <b>To'lovingiz avtomatik tasdiqlandi!</b>\n\n"
            f"{E('money')} Hisobingizga <b>{payment['final_amount']:,} so'm</b> qo'shildi.\n"
            f"{E('wallet')} Joriy balans: <b>{new_balance:,} so'm</b>\n\n"
            f"Endi xohlagan xizmatdan foydalanishingiz mumkin {E('party')}"
        )
    except Exception:
        pass

    try:
        await bot.send_message(
            admin_id,
            f"{E('check')} Avtomatik to'lov tasdiqlandi! ({source})\n\n"
            f"{E('profile')} Foydalanuvchi: <code>{payment['user_id']}</code>\n"
            f"{E('money')} Summa: {payment['final_amount']:,} so'm\n"
            f"💳 Karta: *{card_last or '?'}"
        )
    except Exception:
        pass

    if log_event:
        try:
            await log_event(
                f"{E('rocket')} <b>Avtomatik to'lov tasdiqlandi</b> ({source})\n\n"
                f"{E('profile')} Foydalanuvchi: <code>{payment['user_id']}</code>\n"
                f"{E('money')} +{payment['final_amount']:,} so'm\n"
                f"{E('wallet')} Yangi balans: {new_balance:,} so'm\n"
                f"💳 Karta: *{card_last or '?'}"
            )
        except Exception:
            pass

    return True


# ─── ULANISH HOLATINI KUZATISH ──────────────────────────────────
# Admin panelda "Holatni tekshirish" tugmasi shu global o'zgaruvchidan
# o'qiydi — shu bilan sessiya CHINDAN ulanganini, qaysi akkaunt ekanini
# aniq ko'rish mumkin (taxmin qilishga hojat qolmaydi).
_current_status = {"connected": False, "account": None, "phone": None, "error": None}

def get_status() -> dict:
    return dict(_current_status)


async def start_humo_listener_with_creds(db, bot, admin_id: int, E, api_id: int, api_hash: str,
                                          session_string: str, log_event=None):
    """HUMOcard tinglovchisini berilgan (bazadan yoki .env dan olingan)
    login ma'lumotlari bilan ishga tushiradi. Ulanish holatini
    _current_status ga yozib boradi — shu orqali admin panel "Holatni
    tekshirish" tugmasi orqali aniq bilib oladi."""
    global _current_status
    try:
        from telethon import TelegramClient, events
    except ImportError:
        _current_status = {"connected": False, "account": None, "phone": None,
                            "error": "telethon o'rnatilmagan (pip install telethon)"}
        logger.error("telethon o'rnatilmagan — 'pip install telethon' qiling.")
        return
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(session_string), int(api_id), api_hash)

    # DIQQAT: avval faqat HUMO_BOT_USERNAME dan kelgan xabarlarni tinglar
    # edik — agar bu nom haqiqiy bot nomiga (yoki uning shaxsiy chat
    # ko'rinishiga) mos kelmasa, xabar UMUMAN ko'rinmay qolar edi. Endi
    # BARCHA kiruvchi shaxsiy xabarlarni tinglaymiz va faqat MATN
    # SHAKLIGA qarab (parse_humocard_message orqali) ajratamiz — bot
    # nomiga bog'liq emas, ancha ishonchli.
    @client.on(events.NewMessage(incoming=True))
    async def _on_humo_message(event):
        try:
            if not event.is_private:
                return  # guruh/kanal xabarlarini e'tiborsiz qoldiramiz
            raw = event.raw_text or ""
            parsed = parse_humocard_message(raw)
            if not parsed:
                # Agar xabar to'lovga o'xshab ko'rinsa (UZS/karta so'zlari bor),
                # lekin bizning tahlilchimiz tanimasa — buni ANIQ ko'rish
                # uchun XOM matnni kanalga chiqaramiz. Shu orqali qaysi
                # so'zlar/shakl formatimizga mos kelmayotganini bilib olamiz.
                looks_like_payment = any(k in raw.upper() for k in ["UZS", "HUMOCARD", "KARTA", "*"])
                if looks_like_payment and log_event:
                    try:
                        await log_event(
                            f"{E('warn')} <b>Tanib bo'lmagan xabar keldi</b> (to'lovga o'xshaydi, lekin format mos emas)\n\n"
                            f"<pre>{raw[:500]}</pre>"
                        )
                    except Exception:
                        pass
                return  # bu umuman to'lov xabari emas (shakli mos kelmadi)
            sender = await event.get_sender()
            sender_name = getattr(sender, "username", None) or getattr(sender, "first_name", "noma'lum")
            logger.info(f"📩 Xabar keldi ({sender_name}): {parsed['type']}, {parsed['amount']} so'm")
            if parsed["type"] != "deposit":
                return
            await process_deposit_amount(
                db, bot, admin_id, E,
                amount=parsed["amount"], card_last=parsed.get("card_last", ""),
                source="telethon", log_event=log_event
            )
        except Exception as e:
            logger.exception("HUMOcard xabarini qayta ishlashda xato: %s", e)

    # Ba'zi bildirishnoma botlari yangi xabar yubormasdan, ESKI xabarni
    # TAHRIRLAB (masalan "bugungi xulosa" xabarini yangilab) turishi
    # mumkin — oddiy NewMessage buni ushlamaydi, shuning uchun
    # MessageEdited hodisasini ham alohida tinglaymiz.
    @client.on(events.MessageEdited(incoming=True))
    async def _on_humo_message_edited(event):
        await _on_humo_message(event)

    try:
        await client.start()
        me = await client.get_me()
        account_name = f"{me.first_name} (@{me.username})" if me.username else (me.first_name or "Noma'lum")
        _current_status = {"connected": True, "account": account_name, "phone": me.phone, "error": None}
        logger.info(f"✅ HUMOcard tinglovchisi ulandi: {account_name}")
        async with client:
            await client.run_until_disconnected()
    except Exception as e:
        _current_status = {"connected": False, "account": None, "phone": None, "error": str(e)}
        logger.exception("HUMOcard sessiyasiga ulanishda xatolik: %s", e)
    finally:
        _current_status["connected"] = False


async def start_humo_listener(db, bot, admin_id: int, E, log_event=None):
    """Botning main() ichidan asyncio.create_task() bilan chaqiriladi.
    .env dagi HUMO_* o'zgaruvchilaridan foydalanadi (eski usul, hali ham
    qo'llab-quvvatlanadi). Bazadan (admin panel orqali) sozlangan bo'lsa,
    bot.py o'rniga start_or_restart_humo_listener() dan foydalanadi."""
    if not HUMO_ENABLED:
        logger.warning(
            "HUMO_API_ID / HUMO_API_HASH / HUMO_SESSION_STRING sozlanmagan — "
            "avtomatik to'lov tinglovchisi ishga tushmadi (qo'lda chek tizimi ishlayveradi)."
        )
        return
    await start_humo_listener_with_creds(
        db, bot, admin_id, E,
        api_id=int(HUMO_API_ID), api_hash=HUMO_API_HASH, session_string=HUMO_SESSION_STRING,
        log_event=log_event
    )
