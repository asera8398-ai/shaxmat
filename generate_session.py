"""
generate_session.py — Bir martalik skript. Buni FAQAT o'zingiz shaxsan,
o'z kompyuteringizda yoki serveringizda ishga tushiring (boshqa hech kim
ko'rmasin). Telefon raqamingiz va SMS kod so'raladi, oxirida uzun
SESSION STRING chiqadi — shuni .env fayliga HUMO_SESSION_STRING qilib
joylashtiring.

Ishga tushirish:
    pip install telethon
    python3 generate_session.py
"""

import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=== HUMOcard uchun session yaratish ===\n")
    api_id = int(input("API_ID (my.telegram.org dan): ").strip())
    api_hash = input("API_HASH (my.telegram.org dan): ").strip()

    async with TelegramClient(StringSession(), api_id, api_hash) as client:
        me = await client.get_me()
        print(f"\n✅ Muvaffaqiyatli kirdingiz: {me.first_name} (@{me.username})\n")
        session_string = client.session.save()
        print("=" * 60)
        print("SESSION STRING (buni .env ga HUMO_SESSION_STRING sifatida qo'ying):")
        print("=" * 60)
        print(session_string)
        print("=" * 60)
        print("\n⚠️  DIQQAT: bu qatorni hech kimga bermang — bu akkauntingizga")
        print("to'liq kirish huquqi beradi!")


if __name__ == "__main__":
    asyncio.run(main())
