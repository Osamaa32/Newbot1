#!/usr/bin/env python3
"""
generate_session.py — توليد جلسة Telegram محلياً

الاستخدام:
    pip install telethon
    python generate_session.py

يطلب API ID, API Hash, رقم الهاتف، والكود
يطبع Session String — انسخه وحطه في Railway كـ ADMIN_SESSION
"""

import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError


def prompt(msg: str) -> str:
    return input(f"{msg}: ").strip()


async def main():
    print("=" * 50)
    print(" Telegram Session Generator")
    print("=" * 50)
    print()

    api_id = prompt("API ID (from my.telegram.org)")
    if not api_id.isdigit():
        print("❌ API ID must be a number!")
        sys.exit(1)

    api_hash = prompt("API Hash (from my.telegram.org)")
    if len(api_hash) < 10:
        print("❌ API Hash looks invalid!")
        sys.exit(1)

    phone = prompt("Phone number (e.g. +966500000001)")
    if not phone.startswith("+"):
        print("❌ Phone must start with +country_code")
        sys.exit(1)

    print()
    print(f"⏳ Connecting to Telegram for {phone}...")

    session = StringSession()
    client = TelegramClient(session, int(api_id), api_hash)
    await client.connect()

    if await client.is_user_authorized():
        print("✅ Already authorized!")
    else:
        print(f"📩 Requesting code for {phone}...")
        result = await client.send_code_request(phone)

        code = prompt("Enter the 5-digit code you received")
        code = code.replace(" ", "").replace("-", "")

        try:
            await client.sign_in(phone, code, phone_code_hash=result.phone_code_hash)
        except SessionPasswordNeededError:
            print("🔐 2FA is enabled on this account.")
            password = prompt("Enter your 2FA password")
            await client.sign_in(password=password)

    me = await client.get_me()
    session_string = client.session.save()

    print()
    print("=" * 50)
    print(" ✅ SUCCESS!")
    print("=" * 50)
    print()
    print(f" Name: {me.first_name} {me.last_name or ''}".strip())
    print(f" Username: @{me.username or 'N/A'}")
    print(f" Phone: {me.phone}")
    print(f" ID: {me.id}")
    print()
    print("─" * 50)
    print(" ADMIN_SESSION (copy this to Railway):")
    print("─" * 50)
    print(session_string)
    print("─" * 50)
    print()
    print(" Add this as an environment variable in Railway:")
    print("  ADMIN_SESSION = (paste the long string above)")
    print()
    print(" Then restart the bot and you're done!")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
