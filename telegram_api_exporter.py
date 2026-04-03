#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram API 匯出工具（使用 Telethon）

功能：
- 由聊天名稱搜尋並抓取最近 N 則訊息
- 輸出 CSV（UTF-8-SIG）與 TXT 到 outputs/daily
- 欄位：date, from, text, links

首次使用：
- 需要 Telegram API 憑證（api_id, api_hash）與手機號驗證
- 可透過互動方式輸入，資料會保存在本機 session 檔（.session）

命令列：
  python telegram_api_exporter.py --chat "📢 [非官方] 公開資訊觀測站" --limit 3000
可選：
  --api-id 12345 --api-hash abcdef... --phone +8869...
"""

import os
import re
import sys
import csv
import time
import json
import argparse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict

# 與 MOPS／台股情境一致，「今日」與顯示時間一律用亞洲／台北
_TW = ZoneInfo("Asia/Taipei")


def _msg_date_to_tw_str(msg_date) -> str:
    """Telethon 的 msg.date 為 UTC，轉成台北時間字串，供 --today-only 與後續 pipeline 一致。"""
    if not msg_date:
        return ""
    if msg_date.tzinfo is None:
        dt_utc = msg_date.replace(tzinfo=timezone.utc)
    else:
        dt_utc = msg_date.astimezone(timezone.utc)
    return dt_utc.astimezone(_TW).strftime("%Y-%m-%d %H:%M:%S")


def _msg_date_to_tw_datetime(msg_date):
    """msg.date → 台北 timezone-aware datetime（供分頁停止條件）。"""
    if not msg_date:
        return None
    if msg_date.tzinfo is None:
        dt_utc = msg_date.replace(tzinfo=timezone.utc)
    else:
        dt_utc = msg_date.astimezone(timezone.utc)
    return dt_utc.astimezone(_TW)


def _message_to_row(msg) -> Dict:
    text = getattr(msg, "message", "") or ""
    links = extract_links(text)
    sender = ""
    try:
        if msg.sender:
            if getattr(msg.sender, "username", None):
                sender = f"@{msg.sender.username}"
            elif getattr(msg.sender, "first_name", None) or getattr(msg.sender, "last_name", None):
                sender = f"{getattr(msg.sender, 'first_name', '')} {getattr(msg.sender, 'last_name', '')}".strip()
    except Exception:
        sender = ""
    return {
        "date": _msg_date_to_tw_str(msg.date) if msg.date else "",
        "from": sender,
        "text": str(text).replace("\r", " ").replace("\n", " ").strip(),
        "links": " ".join(links),
    }


async def _fetch_today_paginated(entity, client, page_size: int, max_total: int) -> List[Dict]:
    """由最新訊息往舊分頁，直到本頁最舊一則已早於台北「今日」或達上限（避免只抓 N 則漏掉清晨公告）。"""
    from telethon.errors import FloodWaitError

    today_date = datetime.now(_TW).date()
    rows: List[Dict] = []
    offset_id = 0
    backoff = 2.0
    page_idx = 0

    while len(rows) < max_total:
        batch_msgs = []
        while True:
            try:
                async for msg in client.iter_messages(entity, limit=page_size, offset_id=offset_id):
                    batch_msgs.append(msg)
                break
            except FloodWaitError as fe:
                wait = int(getattr(fe, "seconds", 5) or 5)
                safe_print(f"⚠️ 頻率限制，等待 {wait} 秒後重試（分頁）...")
                time.sleep(wait)
            except Exception as e:
                safe_print(f"⚠️ 分頁讀取錯誤：{e}，{backoff:.1f}s 後重試...")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

        if not batch_msgs:
            break

        page_idx += 1
        for msg in batch_msgs:
            if not msg.date:
                continue
            dt_tw = _msg_date_to_tw_datetime(msg.date)
            if dt_tw and dt_tw.date() == today_date:
                rows.append(_message_to_row(msg))

        oldest = batch_msgs[-1]
        offset_id = oldest.id
        oldest_tw = _msg_date_to_tw_datetime(oldest.date) if oldest.date else None

        safe_print(
            f"ℹ️ 分頁第 {page_idx} 頁：本頁 {len(batch_msgs)} 則，累計今日 {len(rows)} 則"
        )

        if oldest_tw and oldest_tw.date() < today_date:
            break
        backoff = 2.0

    return rows


def safe_print(msg: str) -> None:
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('ascii', 'ignore').decode('ascii'))


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def extract_links(text: str) -> List[str]:
    if not text:
        return []
    try:
        text_str = str(text)
        pattern = r"https?://\S+"
        return re.findall(pattern, text_str)
    except Exception:
        return []


def write_outputs(rows: List[Dict], out_dir: str) -> Dict[str, str]:
    ensure_dir(out_dir)
    today = datetime.now(_TW).strftime('%Y%m%d')
    csv_path = os.path.join(out_dir, f'telegram_messages_{today}.csv')
    txt_path = os.path.join(out_dir, f'telegram_messages_{today}.txt')

    # CSV（UTF-8-SIG）
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=['date', 'from', 'text', 'links'])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)

    # TXT
    with open(txt_path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(f"{r['date']} | {r['from']}\n")
            f.write(f"{r['text']}\n")
            if r['links']:
                f.write(f"links: {r['links']}\n")
            f.write('-' * 80 + '\n')

    return {'csv': csv_path, 'txt': txt_path}


def get_config_defaults() -> Dict:
    cfg_path = os.path.join('configs', 'telegram_api.json')
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_config(api_id: int, api_hash: str, phone: str) -> None:
    ensure_dir('configs')
    cfg_path = os.path.join('configs', 'telegram_api.json')
    with open(cfg_path, 'w', encoding='utf-8') as f:
        json.dump({'api_id': api_id, 'api_hash': api_hash, 'phone': phone}, f, ensure_ascii=False, indent=2)


async def connect_and_fetch(
    chat: str,
    limit: int,
    api_id: int | None,
    api_hash: str | None,
    phone: str | None,
    *,
    paginate_today: bool = False,
    page_size: int = 400,
    max_total: int = 25000,
) -> List[Dict]:
    from telethon import TelegramClient
    from telethon.errors import FloodWaitError, SessionPasswordNeededError

    # ── 優先使用環境變數中的 StringSession（GitHub Actions 使用）──
    session_string = os.environ.get('TELEGRAM_SESSION_STRING', '').strip()
    env_api_id = os.environ.get('TELEGRAM_API_ID', '').strip()
    env_api_hash = os.environ.get('TELEGRAM_API_HASH', '').strip()

    if env_api_id:
        api_id = int(env_api_id)
    if env_api_hash:
        api_hash = env_api_hash

    if session_string:
        # 使用 StringSession（免互動登入）
        from telethon.sessions import StringSession
        safe_print('ℹ️ 使用 StringSession（GitHub Actions 模式）')
        client = TelegramClient(StringSession(session_string), api_id, api_hash)
        await client.connect()
        # StringSession 沒有本機快取，必須先載入對話清單才能用名稱找到頻道
        safe_print('📋 載入對話清單（首次連線必要）...')
        await client.get_dialogs()
        safe_print('✅ 對話清單載入完成')
    else:
        # 本機模式：使用 session 檔案
        session_name = 'test_session'

        if api_id is None or api_hash is None:
            safe_print('首次使用需要 Telegram API 憑證，可在 https://my.telegram.org 取得')
            api_id = int(input('請輸入 api_id: ').strip())
            api_hash = input('請輸入 api_hash: ').strip()
        if not phone or phone == "+8869xxxxxxxx":
            phone = input('請輸入電話號碼（含國碼，如 +8869...）: ').strip()

        try:
            save_config(api_id, api_hash, phone)
        except Exception:
            pass

        client = TelegramClient(session_name, api_id, api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.send_code_request(phone)
            code = input('請輸入 Telegram 驗證碼: ').strip()
            try:
                await client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                pw = input('啟用兩步驗證，請輸入密碼: ').strip()
                await client.sign_in(password=pw)

    # 尋找聊天
    try:
        entity = await client.get_input_entity(chat)
    except Exception as e:
        safe_print(f"無法找到聊天 '{chat}'，嘗試使用已知的聊天名稱...")
        # 嘗試使用已知的聊天名稱
        known_chats = [
            "📢 [非官方] 公開資訊觀測站 即時重大訊息 (媒體/報導/澄清/注意交易)",
            "📢 [非官方] 公開資訊觀測站 即時重大訊息",
            "mops imformation catcher"
        ]
        for known_chat in known_chats:
            try:
                entity = await client.get_input_entity(known_chat)
                safe_print(f"✅ 使用聊天: {known_chat}")
                break
            except Exception:
                continue
        else:
            raise e

    if paginate_today:
        safe_print(f"ℹ️ 分頁抓取台北「今日」訊息（每頁 {page_size}，累計上限 {max_total}）")
        rows = await _fetch_today_paginated(entity, client, page_size, max_total)
        await client.disconnect()
        rows.sort(key=lambda r: r["date"], reverse=True)
        return rows

    rows: List[Dict] = []
    fetched = 0
    backoff = 2.0
    while fetched < limit:
        try:
            async for msg in client.iter_messages(entity, limit=limit, offset_id=0):
                rows.append(_message_to_row(msg))
                fetched += 1
                if fetched >= limit:
                    break
            break
        except FloodWaitError as fe:
            wait = int(getattr(fe, 'seconds', 5) or 5)
            safe_print(f'⚠️ 頻率限制，等待 {wait} 秒後重試...')
            time.sleep(wait)
        except Exception as e:
            safe_print(f'⚠️ 讀取錯誤：{e}，{backoff:.1f}s 後重試...')
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)

    await client.disconnect()
    # 依時間排序（新到舊）
    rows.sort(key=lambda r: r['date'], reverse=True)
    return rows


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--chat', required=True, help='聊天/群組/頻道名稱，例如：📢 [非官方] 公開資訊觀測站')
    ap.add_argument('--limit', type=int, default=3000, help='最多抓取訊息數（未使用 --paginate-today 時）')
    ap.add_argument('--today-only', action='store_true', help='只保留今日訊息（未使用 --paginate-today 時；若只用 --limit 可能漏掉清晨公告）')
    ap.add_argument(
        '--paginate-today',
        action='store_true',
        help='分頁往舊抓取直到跨過台北「今日」或達上限，單日公告再多也盡量不漏',
    )
    ap.add_argument('--page-size', type=int, default=400, help='--paginate-today 每批則數')
    ap.add_argument('--max-total-messages', type=int, default=25000, help='--paginate-today 今日累計上限')
    ap.add_argument('--api-id', type=int, default=None)
    ap.add_argument('--api-hash', default=None)
    ap.add_argument('--phone', default=None)
    args = ap.parse_args()

    cfg = get_config_defaults()
    api_id = args.api_id if args.api_id is not None else cfg.get('api_id')
    api_hash = args.api_hash if args.api_hash is not None else cfg.get('api_hash')
    phone = args.phone if args.phone is not None else cfg.get('phone')

    try:
        rows = await connect_and_fetch(
            args.chat,
            args.limit,
            api_id,
            api_hash,
            phone,
            paginate_today=args.paginate_today,
            page_size=args.page_size,
            max_total=args.max_total_messages,
        )

        if args.today_only and not args.paginate_today:
            today_str = datetime.now(_TW).strftime('%Y-%m-%d')
            original_count = len(rows)
            rows = [r for r in rows if r.get('date', '').startswith(today_str)]
            safe_print(f'ℹ️ 今日訊息篩選：{original_count} → {len(rows)} 則')
        
        paths = write_outputs(rows, os.path.join('outputs', 'daily'))
        safe_print('✅ Telegram API 匯出完成：')
        for k, v in paths.items():
            safe_print(f'  {k}: {v}')
    except KeyboardInterrupt:
        safe_print('⏹️ 已中止')
        sys.exit(1)
    except Exception as e:
        safe_print(f'❌ 匯出失敗：{e}')
        sys.exit(2)


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())


