#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot 指令監聽器（部署於 GitHub Actions）

指令：
  /cb     → 先即時抓取（若已設定 API），再傳今日轉換公司債公告
  /all    → 先即時抓取，再傳今日完整報告
  /status → 顯示 Bot 狀態與更新時間
  /help   → 顯示指令說明
"""

import glob
import os
import sys
import time
import base64
import requests
import subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TW = timezone(timedelta(hours=8))
TW_ZONE = ZoneInfo("Asia/Taipei")
MAX_RUNTIME_SEC = 5 * 3600  # 每次 GitHub Actions Job 最多跑 5 小時

# 開始抓取前停頓（讓使用者先看到「正在抓取」訊息）
PRE_FETCH_DELAY_SEC = int(os.environ.get("BOT_PRE_FETCH_DELAY_SEC", "4"))
# 抓取完成後停頓再讀檔／回傳（緩衝檔案寫入與 API）
POST_FETCH_DELAY_SEC = int(os.environ.get("BOT_POST_FETCH_DELAY_SEC", "6"))
# 無新訊息時避免狂打 getUpdates
IDLE_SLEEP_SEC = 0.8


def safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("utf-8"), flush=True)


def repo_root() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def can_run_fetch() -> bool:
    sid = os.environ.get("TELEGRAM_SESSION_STRING", "").strip()
    aid = os.environ.get("TELEGRAM_API_ID", "").strip()
    h = os.environ.get("TELEGRAM_API_HASH", "").strip()
    return bool(sid and aid and h)


def run_fetch_subprocess() -> int:
    """執行 main.py fetch，回傳 process returncode。"""
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    safe_print("▶ 執行 main.py fetch ...")
    try:
        r = subprocess.run(
            [sys.executable, "main.py", "fetch"],
            cwd=repo_root(),
            env=env,
            timeout=600,
        )
        safe_print(f"▶ fetch 結束，returncode={r.returncode}")
        return int(r.returncode)
    except subprocess.TimeoutExpired:
        safe_print("❌ fetch 逾時")
        return 124
    except Exception as e:
        safe_print(f"❌ fetch 例外：{e}")
        return 1


def read_local_complete_report() -> str | None:
    """優先今日台北日期之 complete_report，否則取 outputs/daily 最新一份。"""
    today = datetime.now(TW_ZONE).strftime("%Y%m%d")
    exact = os.path.join("outputs", "daily", f"complete_report_{today}.txt")
    if os.path.isfile(exact):
        try:
            with open(exact, "r", encoding="utf-8") as f:
                return f.read()
        except OSError:
            pass
    candidates = sorted(
        glob.glob(os.path.join("outputs", "daily", "complete_report_*.txt")),
        key=os.path.getmtime,
        reverse=True,
    )
    if candidates:
        try:
            with open(candidates[0], "r", encoding="utf-8") as f:
                safe_print(f"ℹ️ 使用本地最新報告：{os.path.basename(candidates[0])}")
                return f.read()
        except OSError:
            pass
    return None


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
            safe_print(f"✅ 讀取 GitHub 報告成功（{len(content):,} 字元）")
            return content
        safe_print(f"⚠️ GitHub API 回應：{resp.status_code} {resp.text[:200]}")
    except Exception as e:
        safe_print(f"❌ 讀取 GitHub 報告失敗：{e}")
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


def load_report_text(github_token: str, repo: str) -> str | None:
    """本地優先，失敗再用 GitHub。"""
    local = read_local_complete_report()
    if local:
        return local
    return get_report_from_github(github_token, repo)


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


def get_updates(token: str, offset: int) -> list:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        data = resp.json()
        if not data.get("ok"):
            desc = data.get("description", data)
            safe_print(f"⚠️ getUpdates 失敗：{desc}")
            return []
        return data.get("result", [])
    except Exception as e:
        safe_print(f"⚠️ getUpdates 例外：{e}")
        return []


def parse_command(text: str) -> str:
    """只取第一個詞並去掉 @bot，例如 '/all@x' '/cb  ' → /all /cb"""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return ""
    return parts[0].split("@")[0].lower()


def handle_report_command(
    bot_token: str,
    chat_id: str,
    github_token: str,
    github_repo: str,
    mode: str,
) -> None:
    """
    mode: 'all' | 'cb'
    流程：提示 → 停頓 →（可選）fetch → 停頓 → 讀報告 → 回覆
    """
    if mode == "all":
        send_message(
            bot_token,
            chat_id,
            "⏳ 準備抓取最新資料…\n"
            f"（約 {PRE_FETCH_DELAY_SEC} 秒後開始連線 Telegram 頻道）",
        )
    else:
        send_message(
            bot_token,
            chat_id,
            "⏳ 準備更新轉換公司債公告…\n"
            f"（約 {PRE_FETCH_DELAY_SEC} 秒後開始抓取）",
        )

    time.sleep(PRE_FETCH_DELAY_SEC)

    if can_run_fetch():
        send_message(bot_token, chat_id, "📡 正在抓取頻道訊息並產生報告，請稍候（約 1～5 分鐘）…")
        rc = run_fetch_subprocess()
        if rc != 0:
            send_message(
                bot_token,
                chat_id,
                "⚠️ 即時抓取未完全成功，將改讀 GitHub 上最近一次已存檔的報告。",
            )
    else:
        send_message(
            bot_token,
            chat_id,
            "ℹ️ 未設定 TELEGRAM_SESSION_STRING 等憑證，略過即時抓取，改讀 GitHub 上的報告。",
        )

    time.sleep(POST_FETCH_DELAY_SEC)

    report = load_report_text(github_token, github_repo)
    if report:
        if mode == "all":
            send_message(bot_token, chat_id, report)
        else:
            send_message(bot_token, chat_id, filter_cb_only(report))
    else:
        send_message(
            bot_token,
            chat_id,
            "❌ 找不到報告（本地與 GitHub 皆無）。\n"
            "請確認每日 workflow 已成功執行，或檢查 Repo 內 reports/complete_report_latest.txt。",
        )


# ─────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────
def main() -> None:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    github_repo = os.environ.get("GITHUB_REPOSITORY", "dragonheart8787/Corporate-Bond-Bot")

    if not bot_token:
        safe_print("❌ 缺少 TELEGRAM_BOT_TOKEN")
        sys.exit(1)

    start_time = time.time()
    deadline = start_time + MAX_RUNTIME_SEC
    offset = 0

    now_str = datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S")
    end_str = datetime.fromtimestamp(deadline, TW).strftime("%H:%M:%S")
    safe_print(f"🤖 Bot 啟動  {now_str} 台灣時間（運行至 {end_str}）")
    safe_print(f"   即時抓取：{'開啟' if can_run_fetch() else '關閉（僅讀 GitHub）'}")
    safe_print(f"   抓取前後停頓：{PRE_FETCH_DELAY_SEC}s / {POST_FETCH_DELAY_SEC}s")

    while time.time() < deadline:
        updates = get_updates(bot_token, offset)

        if not updates:
            time.sleep(IDLE_SLEEP_SEC)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            msg = upd.get("message") or upd.get("edited_message", {})
            if not msg:
                continue
            text = (msg.get("text") or "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if not text or not chat_id:
                continue

            cmd = parse_command(text)
            safe_print(f"📨 [{chat_id}] cmd={cmd!r} raw={text[:80]!r}")

            if cmd == "/all":
                handle_report_command(bot_token, chat_id, github_token, github_repo, "all")

            elif cmd == "/cb":
                handle_report_command(bot_token, chat_id, github_token, github_repo, "cb")

            elif cmd == "/status":
                updated = get_report_updated_time(github_token, github_repo)
                now_tw = datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S")
                uptime = int((time.time() - start_time) / 60)
                send_message(
                    bot_token,
                    chat_id,
                    f"🤖 Bot 狀態\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"🕐 現在時間：{now_tw}\n"
                    f"⏱  已運行：{uptime} 分鐘\n"
                    f"📊 報告更新：{updated}\n"
                    f"📡 即時抓取：{'已設定' if can_run_fetch() else '未設定（僅 GitHub）'}\n"
                    f"📅 自動排程：約每 15 分鐘抓取並發送（GitHub 以 UTC 觸發）",
                )

            elif cmd in ("/help", "/start"):
                send_message(
                    bot_token,
                    chat_id,
                    "📋 指令說明\n"
                    "━━━━━━━━━━━━━━━\n"
                    "/cb     → 先抓取再傳今日「轉換公司債」摘要\n"
                    "/all    → 先抓取再傳今日完整報告\n"
                    "/status → 狀態與報告更新時間\n"
                    "/help   → 顯示此說明\n\n"
                    "⏱ 下指令後會先提示，再停頓數秒後才連線抓取；完成後亦會短暫停頓再回傳。\n"
                    "📅 自動排程：約每 15 分鐘（GitHub UTC；實際可能晚 1～數分鐘）",
                )

    safe_print("⏹️  Bot 運行時間到，正常退出（GitHub Actions 將自動重新啟動）")


if __name__ == "__main__":
    main()
