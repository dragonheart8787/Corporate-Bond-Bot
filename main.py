#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公司債每日自動化主程式

用法：
  python main.py fetch   → 9:50 抓取 Telegram 訊息並生成 complete_report
  python main.py send    → 10:00 讀取 complete_report 並傳送至 Telegram Bot
  python main.py all     → 抓取 + 直接發送（測試用）
"""

import os
import sys
import asyncio
import csv
import re
import subprocess
from datetime import datetime, timezone, timedelta
from typing import List, Dict

TW = timezone(timedelta(hours=8))


def safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("utf-8"), flush=True)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def extract_links(text: str) -> str:
    if not text:
        return ""
    return " ".join(re.findall(r"https?://\S+", str(text)))


# ─────────────────────────────────────────
# 1. 抓取 Telegram 訊息
# ─────────────────────────────────────────
async def fetch_telegram_messages(
    api_id: int,
    api_hash: str,
    session_string: str,
    chat_name: str,
    limit: int = 500,
) -> List[Dict]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    rows: List[Dict] = []
    today_tw = datetime.now(TW).date()

    async with TelegramClient(StringSession(session_string), api_id, api_hash) as client:
        entity = None
        candidates = [
            chat_name,
            "📢 [非官方] 公開資訊觀測站 即時重大訊息 (媒體/報導/澄清/注意交易)",
            "📢 [非官方] 公開資訊觀測站 即時重大訊息",
            "mops imformation catcher",
        ]
        for name in candidates:
            try:
                entity = await client.get_input_entity(name)
                safe_print(f"✅ 連線頻道：{name}")
                break
            except Exception:
                continue

        if entity is None:
            safe_print("❌ 找不到任何已知頻道，請確認 TELEGRAM_CHAT_NAME 設定")
            return rows

        async for msg in client.iter_messages(entity, limit=limit):
            if not msg.message:
                continue
            msg_date_tw = msg.date.astimezone(TW).date()
            if msg_date_tw < today_tw:
                break
            rows.append({
                "date": msg.date.astimezone(TW).strftime("%Y-%m-%d %H:%M:%S"),
                "from": getattr(msg.sender, "first_name", "") or "",
                "text": msg.message,
                "links": extract_links(msg.message),
            })

    safe_print(f"📨 今日訊息共 {len(rows)} 則")
    return rows


# ─────────────────────────────────────────
# 2. 儲存 CSV
# ─────────────────────────────────────────
def save_csv(rows: List[Dict], out_dir: str) -> str:
    ensure_dir(out_dir)
    today = datetime.now(TW).strftime("%Y%m%d")
    csv_path = os.path.join(out_dir, f"telegram_messages_{today}.csv")

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "from", "text", "links"])
        writer.writeheader()
        writer.writerows(rows)

    safe_print(f"📄 CSV 已儲存：{csv_path}")
    return csv_path


# ─────────────────────────────────────────
# 3. 呼叫 complete_formatter.py 生成完整報告
# ─────────────────────────────────────────
def run_complete_formatter() -> str | None:
    today = datetime.now(TW).strftime("%Y%m%d")
    report_path = os.path.join("outputs", "daily", f"complete_report_{today}.txt")

    safe_print("▶ 執行 complete_formatter.py ...")
    result = subprocess.run(
        [sys.executable, "complete_formatter.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        timeout=120,
    )
    if result.stdout:
        safe_print(result.stdout)
    if result.returncode != 0:
        safe_print(f"⚠️ complete_formatter 回傳碼：{result.returncode}")
        if result.stderr:
            safe_print(result.stderr[:500])

    if os.path.exists(report_path):
        safe_print(f"✅ complete_report 已生成：{report_path}")
        return report_path
    else:
        safe_print(f"❌ 找不到報告：{report_path}")
        return None


# ─────────────────────────────────────────
# 4. 讀取 complete_report 內容
# ─────────────────────────────────────────
def read_report(report_path: str) -> str:
    with open(report_path, "r", encoding="utf-8") as f:
        return f.read()


# ─────────────────────────────────────────
# 5. Telegram Bot 發送
# ─────────────────────────────────────────
def send_telegram(token: str, chat_id: str, text: str) -> bool:
    import requests

    # 單則訊息最多 4096 字元，超過則分段
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    total = len(chunks)
    success = True
    for idx, chunk in enumerate(chunks, 1):
        header = f"[{idx}/{total}]\n" if total > 1 else ""
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": header + chunk},
                timeout=30,
            )
            if resp.ok:
                safe_print(f"✅ 已發送第 {idx}/{total} 段")
            else:
                safe_print(f"⚠️ 發送第 {idx} 段失敗：{resp.status_code} {resp.text[:200]}")
                success = False
        except Exception as e:
            safe_print(f"❌ 發送錯誤：{e}")
            success = False
    return success


# ─────────────────────────────────────────
# 讀取環境變數
# ─────────────────────────────────────────
def load_env() -> dict:
    env = {
        "api_id":         os.environ.get("TELEGRAM_API_ID", ""),
        "api_hash":       os.environ.get("TELEGRAM_API_HASH", ""),
        "session_string": os.environ.get("TELEGRAM_SESSION_STRING", ""),
        "bot_token":      os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        "chat_id":        os.environ.get("TELEGRAM_CHAT_ID", ""),
        "chat_name":      os.environ.get("TELEGRAM_CHAT_NAME",
                          "📢 [非官方] 公開資訊觀測站 即時重大訊息"),
    }

    # 若 api 憑證缺失，嘗試本機 config
    if not env["api_id"] or not env["api_hash"]:
        cfg_path = os.path.join("configs", "telegram_api.json")
        if os.path.exists(cfg_path):
            import json
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            env["api_id"]   = str(cfg.get("api_id", ""))
            env["api_hash"] = cfg.get("api_hash", "")
            safe_print("ℹ️ 使用本機 configs/telegram_api.json")

    return env


# ─────────────────────────────────────────
# MODE: fetch — 9:50 執行
# ─────────────────────────────────────────
async def mode_fetch() -> None:
    safe_print("\n" + "=" * 50)
    safe_print(f"📥 [FETCH] 抓取開始 {datetime.now(TW).strftime('%Y-%m-%d %H:%M:%S')}")
    safe_print("=" * 50)

    env = load_env()
    if not env["api_id"] or not env["api_hash"] or not env["session_string"]:
        safe_print("❌ 缺少 TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_SESSION_STRING")
        sys.exit(1)

    # 步驟 1：抓取訊息
    safe_print("\n▶ 步驟 1：抓取 Telegram 今日訊息")
    rows = await fetch_telegram_messages(
        api_id=int(env["api_id"]),
        api_hash=env["api_hash"],
        session_string=env["session_string"],
        chat_name=env["chat_name"],
        limit=500,
    )

    if not rows:
        safe_print("⚠️ 今日無訊息，建立空報告")
        today = datetime.now(TW).strftime("%Y%m%d")
        ensure_dir("outputs/daily")
        empty_report = os.path.join("outputs", "daily", f"complete_report_{today}.txt")
        with open(empty_report, "w", encoding="utf-8") as f:
            f.write(f"📊 公司債每日報告 {datetime.now(TW).strftime('%Y-%m-%d')}\n\n今日無新訊息。\n")
        return

    # 步驟 2：儲存 CSV
    safe_print("\n▶ 步驟 2：儲存原始資料")
    save_csv(rows, "outputs/daily")

    # 步驟 3：生成完整報告
    safe_print("\n▶ 步驟 3：生成 complete_report")
    run_complete_formatter()

    safe_print("\n✅ 抓取與格式化完成，等待 10:00 傳送")


# ─────────────────────────────────────────
# MODE: send — 10:00 執行
# ─────────────────────────────────────────
def mode_send() -> None:
    safe_print("\n" + "=" * 50)
    safe_print(f"📤 [SEND] 發送開始 {datetime.now(TW).strftime('%Y-%m-%d %H:%M:%S')}")
    safe_print("=" * 50)

    env = load_env()
    if not env["bot_token"] or not env["chat_id"]:
        safe_print("❌ 缺少 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        sys.exit(1)

    today = datetime.now(TW).strftime("%Y%m%d")
    report_path = os.path.join("outputs", "daily", f"complete_report_{today}.txt")

    if not os.path.exists(report_path):
        safe_print(f"❌ 找不到報告：{report_path}（請先執行 fetch 步驟）")
        sys.exit(1)

    report_text = read_report(report_path)
    safe_print(f"📄 報告長度：{len(report_text)} 字元")

    ok = send_telegram(env["bot_token"], env["chat_id"], report_text)
    if ok:
        safe_print("✅ Telegram 傳送成功")
    else:
        safe_print("⚠️ Telegram 傳送部分失敗")
        sys.exit(1)


# ─────────────────────────────────────────
# MODE: all — 測試用（抓取 + 直接發送）
# ─────────────────────────────────────────
async def mode_all() -> None:
    await mode_fetch()
    safe_print("\n▶ 直接發送（測試模式）")
    mode_send()


# ─────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────
def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode == "fetch":
        asyncio.run(mode_fetch())
    elif mode == "send":
        mode_send()
    elif mode == "all":
        asyncio.run(mode_all())
    else:
        safe_print(f"❌ 未知模式：{mode}，請使用 fetch / send / all")
        sys.exit(1)


if __name__ == "__main__":
    main()
