#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【一次性工具】產生 Telethon StringSession

只需在本機執行一次，產生的字串貼到 GitHub Secrets -> TELEGRAM_SESSION_STRING
之後 GitHub Actions 就可以免互動登入抓取 Telegram 訊息。

使用方式：
    python get_session_string.py
"""

import asyncio
import json
import os
import re


def normalize_phone(phone: str) -> str:
    """確保電話號碼格式正確（含 + 國碼，無空格）"""
    phone = re.sub(r"\s+", "", phone)          # 移除所有空格
    if phone.startswith("09") and len(phone) == 10:
        # 台灣手機：09xxxxxxxx → +88690xxxxxxxx → 去掉開頭 0
        phone = "+886" + phone[1:]
    elif phone.startswith("886") and not phone.startswith("+"):
        phone = "+" + phone
    return phone


async def main() -> None:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        from telethon.errors import SessionPasswordNeededError
    except ImportError:
        print("請先安裝 telethon：pip install telethon")
        return

    # ── 讀取本機 configs ──
    cfg_path = os.path.join("configs", "telegram_api.json")
    api_id: int = 0
    api_hash: str = ""
    phone: str = ""

    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        api_id = int(cfg.get("api_id", 0))
        api_hash = cfg.get("api_hash", "")
        phone = cfg.get("phone", "")
        print(f"✅ 已從 {cfg_path} 讀取憑證（api_id={api_id}）")
    else:
        api_id = int(input("請輸入 api_id（從 https://my.telegram.org 取得）: ").strip())
        api_hash = input("請輸入 api_hash: ").strip()
        phone = input("請輸入電話號碼（如 0912345678 或 +886912345678）: ").strip()

    phone = normalize_phone(phone)
    print(f"📱 使用電話號碼：{phone}")

    # ── 建立 client（用 StringSession 讓 session 存在記憶體，不寫檔）──
    client = TelegramClient(StringSession(), api_id, api_hash)

    await client.connect()
    print("🔗 已連線至 Telegram 伺服器")

    if not await client.is_user_authorized():
        print(f"\n📲 正在發送驗證碼到 {phone} ...")
        await client.send_code_request(phone)

        code = input("請輸入收到的 Telegram 驗證碼: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except SessionPasswordNeededError:
            pw = input("帳號啟用了兩步驗證，請輸入密碼: ").strip()
            await client.sign_in(password=pw)

    me = await client.get_me()
    print(f"✅ 登入成功！帳號：{me.first_name} ({me.phone})")

    session_str = client.session.save()
    await client.disconnect()

    print("\n" + "=" * 60)
    print("✅ StringSession 產生成功！")
    print("=" * 60)
    print("\n請將以下字串複製，貼到 GitHub 儲存庫的 Secrets：")
    print("  路徑：Settings → Secrets and variables → Actions")
    print("         → New repository secret")
    print("\n  名稱：TELEGRAM_SESSION_STRING")
    print("  值（請完整複製下方這一行）：\n")
    print(session_str)
    print("\n" + "=" * 60)
    print("⚠️  注意：此 Session 字串等同於帳號登入權限，請勿外洩！")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
