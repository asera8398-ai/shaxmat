"""
tolov_api.py — https://tolov.run.place orqali avtomatik to'lovlarni
tekshiruvchi modul.

Bu — Telethon (o'z sessiyangiz) yoki webhook (tashqi xizmat sizga so'rov
yuboradi) dan farqli, UCHINCHI mustaqil usul: SIZ o'zingiz TolovAPI
serveriga davriy ravishda "shu summa keldimi?" deb so'rov (polling)
yuborasiz. Bu servis allaqachon karta SMS/bildirishnomalarini o'zi
kuzatib turadi — sizga hech qanday sessiya ulashning hojati yo'q, faqat
shop_id va shop_key kerak.

API hujjati: https://tolov.run.place/tolov/api/index.php
    action=check  — shop_id, shop_key, amount, since (unix vaqt)
                    → {"ok": true, "found": true/false}
    action=status — shop_id, shop_key
                    → {"ok": true, "connected": true/false}
"""

import asyncio
import aiohttp

TOLOV_API_URL_DEFAULT = "https://tolov.run.place/tolov/api/index.php"


async def _api_call(api_url: str, params: dict, timeout: int = 15) -> dict:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                return await resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def check_amount(api_url: str, shop_id: str, shop_key: str, amount: int, since_ts: int) -> dict:
    """Berilgan summadagi to'lov `since_ts` (unix vaqt) dan keyin
    kelganmi tekshiradi. Qaytadi: {"ok": bool, "found": bool, ...}"""
    params = {"action": "check", "shop_id": shop_id, "shop_key": shop_key,
              "amount": amount, "since": since_ts}
    return await _api_call(api_url, params)


async def check_status(api_url: str, shop_id: str, shop_key: str) -> dict:
    """Do'konning (shop) TolovAPI'dagi ulanish holatini tekshiradi.
    Qaytadi: {"ok": bool, "connected": bool, ...}"""
    params = {"action": "status", "shop_id": shop_id, "shop_key": shop_key}
    return await _api_call(api_url, params)


async def tolov_polling_loop(db, bot, admin_id: int, E, get_config, process_deposit_amount, log_event=None,
                              poll_interval_sec: int = 10):
    """
    Fonda doim ishlaydigan sikl — har `poll_interval_sec` soniyada barcha
    kutilayotgan (pending) avtomatik to'lovlarni TolovAPI orqali
    tekshiradi. Mos kelgan har bir to'lov `process_deposit_amount()`
    (bot.py / humo_listener.py dagi umumiy funksiya) orqali hisoblanadi —
    shu bilan Telethon va webhook bilan bir xil xavfsizlik (ikki marta
    hisoblanib ketmaslik) ta'minlanadi.

    get_config — async funksiya, {"enabled", "api_url", "shop_id",
    "shop_key"} lug'atini qaytaradi (bot.py dagi sozlamalardan o'qiydi,
    shunda admin panelda o'zgartirilgan sozlama darhol qo'llaniladi).
    """
    import logging
    logger = logging.getLogger("tolov_api")

    while True:
        try:
            cfg = await get_config()
            if not cfg.get("enabled") or not (cfg.get("shop_id") and cfg.get("shop_key")):
                await asyncio.sleep(poll_interval_sec)
                continue

            pending = await db.get_pending_auto_payments()
            for payment in pending:
                since_ts = int(payment["created_at"].timestamp())
                result = await check_amount(
                    cfg["api_url"], cfg["shop_id"], cfg["shop_key"],
                    amount=payment["final_amount"], since_ts=since_ts
                )
                if result.get("ok") and result.get("found"):
                    try:
                        await process_deposit_amount(
                            db, bot, admin_id, E,
                            amount=payment["final_amount"], card_last="",
                            source="tolovapi", log_event=log_event
                        )
                    except Exception as e:
                        logger.exception("TolovAPI orqali to'lovni hisoblashda xato: %s", e)
        except Exception as e:
            logger.exception("TolovAPI polling siklida xato: %s", e)

        await asyncio.sleep(poll_interval_sec)
