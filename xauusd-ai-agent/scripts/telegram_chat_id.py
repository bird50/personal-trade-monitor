"""List chats that recently messaged the bot, to find TELEGRAM_CHAT_ID.

Reads TELEGRAM_BOT_TOKEN from .env (never prints it). Send any message to the bot (or add it to
a group and post there) first, then run:

  .venv/bin/python scripts/telegram_chat_id.py

This calls getUpdates once; it does not reply, does not change the bot and does not write .env.
getUpdates only works while the bot has no webhook set and no other reader is polling: switch
off Telegram chat commands in XM Control ("ปิดรับคำสั่ง /status") before running this.
"""
import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.telegram_notifier import API_BASE, TOKEN_RE  # noqa: E402


def main():
    load_dotenv(ROOT / ".env", override=False)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not TOKEN_RE.fullmatch(token):
        print("ใส่ TELEGRAM_BOT_TOKEN ที่ได้จาก @BotFather ใน .env ก่อน (รูปแบบ 123456789:ABC...)")
        return 2
    try:
        response = httpx.get(f"{API_BASE}/bot{token}/getUpdates", params={"timeout": 0}, timeout=10, trust_env=False)
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print("เรียก Telegram ไม่สำเร็จ:", type(exc).__name__)
        return 1
    if not data.get("ok"):
        if response.status_code == 409:
            print("Telegram ตอบ 409: มีผู้อ่านข้อความอื่นอยู่ (เช่นคำสั่ง /status ของ XM Control เปิดอยู่) ปิดรับคำสั่งก่อนแล้วลองใหม่")
            return 1
        description = re.sub(r"bot\d+:[A-Za-z0-9_-]+", "bot<token>", str(data.get("description", "")))
        print(f"Telegram ตอบ HTTP {response.status_code}: {description}")
        return 1
    chats = {}
    for update in data.get("result", []):
        message = update.get("message") or update.get("channel_post") or update.get("my_chat_member") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or ""
            chats[chat["id"]] = (chat.get("type", "?"), name)
    if not chats:
        print("ยังไม่พบแชท: ส่งข้อความหา bot ก่อน แล้วรันสคริปต์นี้อีกครั้ง")
        return 1
    print("แชทที่พบ (ใส่ค่า chat_id ที่ต้องการใน .env เป็น TELEGRAM_CHAT_ID):")
    for chat_id, (kind, name) in chats.items():
        print(f"  {chat_id}\t{kind}\t{name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
