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
import re
import sys
import time
import base64
import requests
import subprocess
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telegram_date_utils import report_yyyymmdd

TW = timezone(timedelta(hours=8))
TW_ZONE = ZoneInfo("Asia/Taipei")
MAX_RUNTIME_SEC = 5 * 3600  # 每次 GitHub Actions Job 最多跑 5 小時

# 開始抓取前停頓（讓使用者先看到「正在抓取」訊息）
PRE_FETCH_DELAY_SEC = int(os.environ.get("BOT_PRE_FETCH_DELAY_SEC", "4"))
# 抓取完成後停頓再讀檔／回傳（緩衝檔案寫入與 API）
POST_FETCH_DELAY_SEC = int(os.environ.get("BOT_POST_FETCH_DELAY_SEC", "6"))
# 無新訊息時避免狂打 getUpdates
IDLE_SLEEP_SEC = 0.8

# getUpdates 衝突次數（多實例互砍時會狂噴，只在特定次數輸出）
_conflict_count = 0


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
    """優先本次報告日期之 complete_report，否則取 outputs/daily 最新一份。"""
    # 必須與 main.py fetch 寫檔時用的日期一致（含跨午夜回退），否則凌晨會對不到檔案
    today = report_yyyymmdd()
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


def report_generated_at(report: str) -> str:
    """從報告標頭取出「生成時間」，取不到回空字串。"""
    m = re.search(r"生成時間：\s*([\d\-]+\s+[\d:]+)", report or "")
    return m.group(1) if m else ""


def load_report_text(github_token: str, repo: str) -> tuple[str | None, str]:
    """
    回傳 (報告內容, 來源標籤)。來源：'local' = runner 上剛產生的，'github' = repo 存檔。

    抓取失敗時會退回 repo 裡的存檔，那份可能是好幾天前的。舊版直接回傳、不做
    區分，使用者看到的「生成時間」就一直卡在同一個值，看起來像時鐘壞掉，
    實際上是報告根本沒更新。
    """
    local = read_local_complete_report()
    if local:
        return local, "local"
    return get_report_from_github(github_token, repo), "github"


def staleness_note(report: str, source: str) -> str:
    """報告不是剛剛產生的話，回傳一段要附在訊息前面的提醒；夠新則回空字串。"""
    gen = report_generated_at(report)
    if not gen:
        return ""
    try:
        gen_dt = datetime.strptime(gen, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW_ZONE)
    except ValueError:
        return ""
    age_min = (datetime.now(TW_ZONE) - gen_dt).total_seconds() / 60
    if age_min <= 10:
        return ""
    if age_min < 120:
        age = f"{int(age_min)} 分鐘前"
    else:
        age = f"{age_min / 60:.1f} 小時前"
    where = "repo 內的存檔" if source == "github" else "先前產生的檔案"
    return (
        f"⚠️ 這份是{where}，生成於 {gen}（{age}），不是剛剛抓的。\n"
        f"即時抓取未成功，請看 Actions log 或稍後再試。\n"
        + "─" * 20 + "\n\n"
    )


# ─────────────────────────────────────────
# 過濾：只保留轉換公司債公告
# ─────────────────────────────────────────
def filter_cb_only(full_report: str) -> str:
    # 用報告日期而非「現在」：凌晨回退時內容是前一日的，標題不能寫成今天
    _d = report_yyyymmdd()
    date_str = f"{_d[:4]}-{_d[4:6]}-{_d[6:8]}"
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


# 啟動時，停機期間內下的指令只要不超過這個秒數就補處理；更舊的視為過期丟棄
FRESH_COMMAND_MAX_AGE_SEC = int(os.environ.get("BOT_FRESH_COMMAND_MAX_AGE_SEC", "900"))
KNOWN_COMMANDS = {"/all", "/cb", "/status", "/help", "/start"}


def extract_message(upd: dict) -> dict:
    """從一則 update 取出訊息本體。頻道貼文（channel_post）也要認，否則在頻道下的指令會被默默忽略。"""
    return (
        upd.get("message")
        or upd.get("edited_message")
        or upd.get("channel_post")
        or upd.get("edited_channel_post")
        or {}
    )


def collect_pending_updates(token: str) -> tuple[int, list]:
    """
    啟動時取回積壓更新，回傳 (新的 offset, 需要補處理的更新)。

    getUpdates 的 offset=0 會倒出所有「尚未確認」的更新，包含 bot 停機期間累積的
    （Telegram 保留 24 小時）。兩種做法都有問題：
      - 全部重跑：每次 5 小時換班都會把舊的 /cb、/all 重跑一次（使用者看到「抓兩次」）。
      - 全部丟掉（上一版）：bot 空窗期間使用者下的 /cb 被默默吃掉，上線後也不回應，
        使用者只會覺得「指令不能用」。
    折衷：只補處理「夠新」的指令（預設 15 分鐘內），且同一個聊天室重複下同一個指令只做一次；
    其餘全部確認掉但不執行。
    """
    offset = 0
    collected: list = []
    try:
        for _ in range(10):  # 每頁最多 100 則，最多翻 10 頁
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 0},
                timeout=20,
            )
            data = resp.json()
            if not data.get("ok"):
                safe_print(f"⚠️ 啟動取回積壓更新失敗：{data.get('description', data)}")
                return offset, []
            page = data.get("result", [])
            if not page:
                break
            collected.extend(page)
            offset = page[-1]["update_id"] + 1
            if len(page) < 100:
                break
    except Exception as e:
        safe_print(f"⚠️ 啟動取回積壓更新例外：{e}")
        return offset, []

    if not collected:
        safe_print("🧹 啟動時沒有積壓更新")
        return offset, []

    now = time.time()
    fresh: list = []
    seen: set = set()
    stale = dup = 0
    for upd in collected:
        msg = extract_message(upd)
        text = (msg.get("text") or "").strip()
        cmd = parse_command(text) if text else ""
        age = now - float(msg.get("date") or 0)
        if cmd not in KNOWN_COMMANDS or age > FRESH_COMMAND_MAX_AGE_SEC:
            stale += 1
            continue
        key = (str(msg.get("chat", {}).get("id", "")), cmd)
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        fresh.append(upd)

    safe_print(
        f"🧹 啟動取回 {len(collected)} 則積壓更新：補處理 {len(fresh)} 則"
        f"（{FRESH_COMMAND_MAX_AGE_SEC // 60} 分鐘內的指令）、過期或非指令 {stale} 則、重複 {dup} 則"
    )
    return offset, fresh


def ack_updates(token: str, offset: int) -> None:
    """結束前確認已處理的更新，避免下一棒 bot 把最後那幾則再取一次。"""
    try:
        requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 0},
            timeout=15,
        )
    except Exception:
        pass


def get_updates(token: str, offset: int) -> list:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35,
        )
        data = resp.json()
        if not data.get("ok"):
            desc = str(data.get("description", data))
            if "Conflict" in desc:
                # 同一個 token 不能有兩個 poller：Telegram 會互砍雙方的請求，
                # offset 永遠推不動，指令被反覆重送、報告也更新不了。
                # 這是設定問題不是暫時性錯誤，狂 retry 沒用，拉長間隔並講清楚。
                global _conflict_count
                _conflict_count += 1
                if _conflict_count in (1, 10) or _conflict_count % 100 == 0:
                    safe_print(
                        f"❌ getUpdates 衝突（第 {_conflict_count} 次）：有另一個 bot 實例在跑。"
                        " 請確認 bot.yml 的 concurrency 生效、並取消多餘的 workflow run。"
                    )
                time.sleep(5)
                return []
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

    report, source = load_report_text(github_token, github_repo)
    if report:
        note = staleness_note(report, source)
        body = report if mode == "all" else filter_cb_only(report)
        send_message(bot_token, chat_id, note + body)
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
    offset, pending = collect_pending_updates(bot_token)
    polls = received = 0
    last_beat = time.time()

    now_str = datetime.now(TW).strftime("%Y-%m-%d %H:%M:%S")
    end_str = datetime.fromtimestamp(deadline, TW).strftime("%H:%M:%S")
    safe_print(f"🤖 Bot 啟動  {now_str} 台灣時間（運行至 {end_str}）")
    safe_print(f"   即時抓取：{'開啟' if can_run_fetch() else '關閉（僅讀 GitHub）'}")
    safe_print(f"   抓取前後停頓：{PRE_FETCH_DELAY_SEC}s / {POST_FETCH_DELAY_SEC}s")

    while time.time() < deadline:
        if pending:
            updates, pending = pending, []
        else:
            updates = get_updates(bot_token, offset)
            polls += 1

        # 心跳：每 30 分鐘一行。沒有它，bot 靜悄悄跑 5 小時的 log 只有兩行，
        # 完全分不出「沒人下指令」和「指令沒送進來」。
        if time.time() - last_beat >= 1800:
            last_beat = time.time()
            safe_print(
                f"💓 bot 運作中：已運行 {int((time.time() - start_time) / 60)} 分鐘、"
                f"輪詢 {polls} 次、收到 {received} 則更新"
            )

        if not updates:
            time.sleep(IDLE_SLEEP_SEC)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            received += 1
            msg = extract_message(upd)
            if not msg:
                safe_print(f"ℹ️ 略過非訊息更新：{[k for k in upd if k != 'update_id']}")
                continue
            text = (msg.get("text") or "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if not text or not chat_id:
                safe_print(f"ℹ️ 略過無文字訊息（chat={chat_id or '?'}）")
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
                    f"📅 自動排程：每日台北 18:23 起，另有 19:37／20:53／22:47 三班備援（GitHub 排程常延遲數小時，偶爾會整個略過，先成功的那班會發送、其餘略過）",
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
                    "📅 自動排程：每日台北 18:23 起（備援 19:37／20:53／22:47）。GitHub 排程常延遲數小時；若延遲跨過午夜，報告會自動改抓前一日。",
                )

    ack_updates(bot_token, offset)
    safe_print("⏹️  Bot 運行時間到，正常退出（GitHub Actions 將自動重新啟動）")


if __name__ == "__main__":
    main()
