#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot 指令監聽器（部署於 GitHub Actions）

指令：
  /cb     → 傳送今日轉換公司債公告
  /all    → 傳送今日完整報告（所有公告）
  /status → 顯示 Bot 狀態與更新時間
  /help   → 顯示指令說明
"""

import os
import sys
import time
import base64
import requests
from datetime import datetime, timezone, timedelta

TW = timezone(timedelta(hours=8))
MAX_RUNTIME_SEC = 5 * 3600   # 每次 GitHub Actions Job 最多跑 5 小時


def safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("utf-8"), flush=True)


# ─────────────────────────────────────────
# 讀取 GitHub 倉庫中的最新報告
# ─────────────────────────────────────────
def get_report_from_github(github_token: str, repo: str) -> str | None:
    """從 reports/complete_report_latest.txt 讀取最新報告"""
    url = f"https://api.github.com/repos/{repo}/contents/reports/complete_report_latest.txt"
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.ok:
            content = base64.b64decode(resp.json()["content"]).decode("utf-8")
            safe_print(f"✅ 讀取報告成功（{len(content):,} 字元）")
            return content
        safe_print(f"⚠️ GitHub API 回應：{resp.status_code}")
    except Exception as e:
        safe_print(f"❌ 讀取報告失敗：{e}")
    return None


def get_report_updated_time(github_token: str, repo: str) -> str:
    """取得報告最後更新時間"""
    url = f"https://api.github.com/repos/{repo}/commits?path=reports/complete_report_latest.txt&per_page=1"
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.ok and resp.json():
            utc_str = resp.json()[0]["commit"]["committer"]["date"]
            utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
            tw_dt = utc_dt.astimezone(TW)
            return tw_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return "未知"


# ─────────────────────────────────────────
# 過濾：只保留轉換公司債公告
# ─────────────────────────────────────────
def filter_cb_only(full_report: str) -> str:
    date_str = datetime.now(TW).strftime("%Y-%m-%d")
    CB_START = [
        "🔥 轉換公司債相關公告",
        "轉換公司債相關公告",
        "【轉換公司債 - 第 1 則】",
    ]
    CB_END = [
        "📢 澄清媒體報導", "💰 財務資訊", "👥 人事異動",
        "⚠️ 注意交易", "📋 重大訊息", "📄 其他", "── 其他公告",
    ]

    cb_start = -1
    for m in CB_START:
        idx = full_report.find(m)
        if idx != -1:
            cb_start = idx
            break

    if cb_start == -1:
        return f"📊 公司債報告 {date_str}\n\n今日無轉換公司債相關公告。"

    cb_end = len(full_report)
    for m in CB_END:
        idx = full_report.find(m, cb_start + 50)
        if idx != -1 and idx < cb_end:
            cb_end = idx

    cb_content = full_report[cb_start:cb_end].strip()
    count = cb_content.count("【轉換公司債")
    return (
        f"🔴 轉換公司債公告  {date_str}\n"
        f"共 {count} 則\n"
        + "=" * 40 + "\n\n"
        + cb_content
    )


# ─────────────────────────────────────────
# Telegram API 工具函式
# ─────────────────────────────────────────
def send_message(token: str, chat_id: str, text: str) -> None:
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        header = f"[{idx}/{total}]\n" if total > 1 else ""
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": header + chunk},
                    timeout=30,
                )
                if resp.ok:
                    break
                elif resp.status_code == 429:
                    wait = resp.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(wait)
                else:
                    time.sleep(2)
            except Exception:
                time.sleep(3)
        time.sleep(0.3)


def get_updates(token: str, offset: int = 0) -> list:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        return resp.json().get("result", [])
    except Exception:
        return []


# ─────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────
def main() -> None:
    bot_token    = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    github_repo  = os.environ.get("GITHUB_REPOSITORY", "dragonheart8787/Corporate-Bond-Bot")

    if not bot_token:
        safe_print("❌ 缺少 TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    start_time = time.time()
    deadline   = start_time + MAX_RUNTIME_SEC
    offset     = 0

    now_str = datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S")
    end_str = datetime.fromtimestamp(deadline, TW).strftime("%H:%M:%S")
    safe_print(f"🤖 Bot 啟動  {now_str} 台灣時間（運行至 {end_str}）")

    while time.time() < deadline:
        updates = get_updates(bot_token, offset)

        for upd in updates:
            offset = upd["update_id"] + 1
            msg     = upd.get("message") or upd.get("edited_message", {})
            if not msg:
                continue
            text    = (msg.get("text") or "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if not text or not chat_id:
                continue

            # 只取指令部分（@botname 形式）
            cmd = text.split("@")[0].lower()
            safe_print(f"📨 [{chat_id}] {text}")

            if cmd == "/all":
                send_message(bot_token, chat_id, "⏳ 讀取完整報告中...")
                report = get_report_from_github(github_token, github_repo)
                if report:
                    send_message(bot_token, chat_id, report)
                else:
                    send_message(bot_token, chat_id,
                        "❌ 找不到報告。\n請等待每日 workflow 完成（約 23:00～23:10 台灣時間）。")

            elif cmd == "/cb":
                send_message(bot_token, chat_id, "⏳ 讀取轉換公司債公告中...")
                report = get_report_from_github(github_token, github_repo)
                if report:
                    send_message(bot_token, chat_id, filter_cb_only(report))
                else:
                    send_message(bot_token, chat_id, "❌ 找不到報告。")

            elif cmd == "/status":
                updated = get_report_updated_time(github_token, github_repo)
                now_tw  = datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S")
                uptime  = int((time.time() - start_time) / 60)
                send_message(bot_token, chat_id,
                    f"🤖 Bot 狀態\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🕐 現在時間：{now_tw}\n"
                    f"⏱  已運行：{uptime} 分鐘\n"
                    f"📊 報告更新：{updated}\n"
                    f"📡 每日抓取：約 23:00\n"
                    f"📤 每日傳送：約 23:05"
                )

            elif cmd in ("/help", "/start"):
                send_message(bot_token, chat_id,
                    "📋 指令說明\n"
                    "━━━━━━━━━━━━━━━\n"
                    "/cb     → 今日轉換公司債公告\n"
                    "/all    → 今日完整報告（所有公告）\n"
                    "/status → Bot 狀態與報告更新時間\n"
                    "/help   → 顯示此說明\n\n"
                    "📅 每日自動傳送：約 23:05（台灣時間）"
                )

    safe_print("⏹️  Bot 運行時間到，正常退出（GitHub Actions 將自動重新啟動）")


if __name__ == "__main__":
    main()
