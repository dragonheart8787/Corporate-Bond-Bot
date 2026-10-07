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
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import bot_relay as relay
from telegram_date_utils import report_yyyymmdd

TW = timezone(timedelta(hours=8))
TW_ZONE = ZoneInfo("Asia/Taipei")
MAX_RUNTIME_SEC = int(os.environ.get("BOT_MAX_RUNTIME_SEC", str(5 * 3600)))  # 每次 GitHub Actions Job 最多跑 5 小時
# 本棒結束前多久開始觸發下一棒。留足夠的重試時間，也讓後繼 run 在本棒結束前就已排隊，
# 本棒一結束它就立刻接手，交接不再依賴那一刻 GitHub 還肯不肯建立 run。
RELAY_LEAD_SEC = int(os.environ.get("BOT_RELAY_LEAD_SEC", "900"))

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
def send_message(token: str, chat_id: str, text: str) -> bool:
    """送出訊息（自動分段、重試）。回傳是否全部送出；失敗一律留下遮罩後的 log，不再靜默吞掉。"""
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    total = len(chunks)
    who = relay.mask_chat_id(chat_id)
    all_ok = True
    for idx, chunk in enumerate(chunks, 1):
        header = f"[{idx}/{total}]\n" if total > 1 else ""
        sent = False
        for attempt in range(1, 4):
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": header + chunk},
                    timeout=30,
                )
                if resp.ok:
                    sent = True
                    break
                if resp.status_code == 429:
                    wait = resp.json().get("parameters", {}).get("retry_after", 5)
                    safe_print(f"⏳ Telegram 限流（{who}），等待 {wait} 秒（第 {attempt}/3 次）")
                    time.sleep(wait)
                else:
                    safe_print(f"⚠️ Telegram sendMessage 失敗：HTTP {resp.status_code}（{who}，第 {attempt}/3 次）")
                    time.sleep(2)
            except Exception as e:
                safe_print(f"⚠️ Telegram sendMessage 例外（{who}，第 {attempt}/3 次）：{relay.redact(e)}")
                time.sleep(3)
        if not sent:
            all_ok = False
            safe_print(f"❌ 訊息第 {idx}/{total} 段最終送出失敗（{who}）")
        time.sleep(0.3)
    return all_ok


# ─────────────────────────────────────────
# 啟動時的積壓指令
# ─────────────────────────────────────────
# 啟動時，停機期間內下的指令只要不超過這個秒數就補執行；更舊的不執行，改為通知使用者重發
FRESH_COMMAND_MAX_AGE_SEC = int(os.environ.get("BOT_FRESH_COMMAND_MAX_AGE_SEC", "900"))
# Telegram 只保留未確認的更新約 24 小時，超過這個年齡的指令就算想通知也找不到了
EXPIRED_NOTICE_MAX_AGE_SEC = int(os.environ.get("BOT_EXPIRED_NOTICE_MAX_AGE_SEC", str(24 * 3600)))
KNOWN_COMMANDS = {"/all", "/cb", "/status", "/help", "/start"}
# 只有「要資料」的指令才值得通知過期（/status、/help 過期了回覆也沒意義）
NOTICE_COMMANDS = {"/cb", "/all"}
MAX_EXPIRED_NOTICE_CHATS = 5


def extract_message(upd: dict) -> dict:
    """從一則 update 取出訊息本體。頻道貼文（channel_post）也要認，否則在頻道下的指令會被默默忽略。"""
    return (
        upd.get("message")
        or upd.get("edited_message")
        or upd.get("channel_post")
        or upd.get("edited_channel_post")
        or {}
    )


def parse_command(text: str) -> str:
    """只取第一個詞並去掉 @bot，例如 '/all@x' '/cb  ' → /all /cb"""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return ""
    return parts[0].split("@")[0].lower()


@dataclass
class PendingResult:
    fresh: list = field(default_factory=list)        # 夠新，啟動後補執行
    expired: dict = field(default_factory=dict)      # chat_id -> {cmd: 最近一次的 unix ts}，只通知不執行
    counts: dict = field(default_factory=dict)


def classify_pending(
    collected: list,
    now: float,
    *,
    min_update_id: int = 0,
    fresh_age: float = FRESH_COMMAND_MAX_AGE_SEC,
    notice_age: float = EXPIRED_NOTICE_MAX_AGE_SEC,
) -> PendingResult:
    """
    把啟動時取回的積壓更新分類。每一則恰好落進一個桶，所以各項加總 == total，
    log 才對得起來（丟棄了幾則、為什麼）。

    選用「過期不執行、改通知」而不是「放寬窗口一律執行」的理由：
      1. 重放風險：放寬到數小時，一次空窗後上線會一口氣補跑 /cb、/all，每個都會用同一組
         Telegram session 抓取。這組 session 曾因多處同時使用被 Telegram 撤銷，啟動瞬間
         又正好是每日報告、接力交接最容易重疊的時候，不值得冒這個險。
      2. 過期指令的語意已經變了：使用者可能早就用別的方式拿到資料，幾小時後才冒出一則
         回覆反而造成困惑。通知他「已過期、請重發」成本最低、結果可預期。
    update_id 去重（min_update_id 與 processed_ids）仍然保留，用來防止交接時重複處理。
    """
    counts = dict.fromkeys(
        (
            "total", "fresh", "below_floor", "non_message", "non_command",
            "duplicate", "expired_notified", "expired_dropped",
        ),
        0,
    )
    result = PendingResult(counts=counts)
    seen_fresh: set = set()
    expired: dict = {}

    for upd in collected:
        counts["total"] += 1
        uid = int(upd.get("update_id", 0))
        if min_update_id and uid < min_update_id:
            counts["below_floor"] += 1          # 前一棒已處理（交接水位線以下）
            continue
        msg = extract_message(upd)
        text = (msg.get("text") or "").strip()
        if not msg or not text:
            counts["non_message"] += 1
            continue
        cmd = parse_command(text)
        if cmd not in KNOWN_COMMANDS:
            counts["non_command"] += 1
            continue

        chat = str(msg.get("chat", {}).get("id", ""))
        ts = float(msg.get("date") or 0)
        age = now - ts
        if age <= fresh_age:
            key = (chat, cmd)
            if key in seen_fresh:
                counts["duplicate"] += 1        # 同聊天室連按好幾次同一個指令，只做一次
                continue
            seen_fresh.add(key)
            result.fresh.append(upd)
            counts["fresh"] += 1
        elif cmd in NOTICE_COMMANDS and chat and age <= notice_age:
            per_chat = expired.setdefault(chat, {})
            if cmd in per_chat:
                counts["duplicate"] += 1
            per_chat[cmd] = max(ts, per_chat.get(cmd, 0.0))
        else:
            counts["expired_dropped"] += 1      # /status、/help 過期，或超過 Telegram 保留期

    # 使用者後來又重發了（fresh 裡有同聊天室同指令）→ 不必再通知舊的那則
    for chat, cmd in seen_fresh:
        if expired.get(chat, {}).pop(cmd, None) is not None:
            counts["duplicate"] += 1
    result.expired = {c: v for c, v in expired.items() if v}
    counts["expired_notified"] = sum(len(v) for v in result.expired.values())
    return result


def build_expired_notice(cmds: dict, gap_minutes: float | None) -> str:
    lines = ["🤖 bot 剛重新上線" + (f"（先前約 {int(gap_minutes)} 分鐘沒有在線上）。" if gap_minutes and gap_minutes >= 1 else "。")]
    lines.append(
        f"偵測到你在 bot 離線期間發出的指令。因為已超過 {FRESH_COMMAND_MAX_AGE_SEC // 60} 分鐘，"
        "為避免送出過期的結果，沒有自動執行："
    )
    for cmd, ts in sorted(cmds.items(), key=lambda kv: kv[1]):
        when = datetime.fromtimestamp(ts, TW_ZONE).strftime("%m/%d %H:%M")
        lines.append(f"  • {when}  {cmd}")
    lines.append("請重新發送一次。")
    return "\n".join(lines)


def send_expired_notices(token: str, expired: dict, gap_minutes: float | None) -> int:
    """對每個有過期指令的聊天室各送一則通知，回傳成功通知的聊天室數。"""
    sent = 0
    for n, (chat, cmds) in enumerate(expired.items()):
        if n >= MAX_EXPIRED_NOTICE_CHATS:
            safe_print(f"⚠️ 過期指令通知上限 {MAX_EXPIRED_NOTICE_CHATS} 個聊天室，其餘 {len(expired) - n} 個略過")
            break
        if send_message(token, chat, build_expired_notice(cmds, gap_minutes)):
            sent += 1
            safe_print(f"📣 已通知 {relay.mask_chat_id(chat)}：{len(cmds)} 則過期指令請重發")
        else:
            safe_print(f"❌ 通知 {relay.mask_chat_id(chat)} 過期指令失敗（使用者不會知道他的指令被略過）")
    return sent


def fetch_pending_updates(token: str) -> tuple[int, list]:
    """
    啟動時取回所有未確認的更新並全部確認（offset 推進到最後一則之後），回傳 (新的 offset, 更新清單)。
    取回失敗時回傳已取到的部分並留下明確 log，不阻擋 bot 啟動。
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
                safe_print(f"❌ 啟動取回積壓更新失敗：{relay.redact(data.get('description', data))}")
                return offset, collected
            page = data.get("result", [])
            if not page:
                break
            collected.extend(page)
            offset = page[-1]["update_id"] + 1
            if len(page) < 100:
                break
    except Exception as e:
        safe_print(f"❌ 啟動取回積壓更新例外：{relay.redact(e)}")
    return offset, collected


def ack_updates(token: str, offset: int, attempts: int = 3) -> bool:
    """結束前確認已處理的更新，避免下一棒把最後那幾則再取一次。失敗會重試，最終失敗明確記錄。"""
    for i in range(1, attempts + 1):
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"offset": offset, "timeout": 0},
                timeout=15,
            )
            data = resp.json()
            if data.get("ok"):
                return True
            safe_print(f"⚠️ 結束前確認 offset 失敗（第 {i}/{attempts} 次）：{relay.redact(data.get('description', data))}")
        except Exception as e:
            safe_print(f"⚠️ 結束前確認 offset 例外（第 {i}/{attempts} 次）：{relay.redact(e)}")
        time.sleep(2 * i)
    safe_print(
        f"❌ 結束前確認 offset={offset} 最終失敗：下一棒可能重複取得最後幾則更新"
        "（已由交接水位線 handoff_min_update_id 與指令年齡窗口降低風險）"
    )
    return False


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
            safe_print(f"⚠️ getUpdates 失敗：{relay.redact(desc)}")
            return []
        return data.get("result", [])
    except Exception as e:
        safe_print(f"⚠️ getUpdates 例外：{relay.redact(e)}")
        return []


def notify_alert(bot_token: str, text: str) -> None:
    """盡可能通知維運者。沒設定 BOT_ALERT_CHAT_ID 就只寫 log（::error:: 註解仍會顯示在 run 頁面）。"""
    chat = os.environ.get("BOT_ALERT_CHAT_ID", "").strip()
    if not chat:
        safe_print("ℹ️ 未設定 BOT_ALERT_CHAT_ID，告警只寫入 log")
        return
    if not send_message(bot_token, chat, "🚨 " + text):
        safe_print("❌ 告警訊息也送不出去，請直接查看 Actions 頁面")


DAILY_IDLE_MAX_WAIT_SEC = int(os.environ.get("BOT_DAILY_IDLE_MAX_WAIT_SEC", "120"))
DAILY_HOLD_AFTER_FETCH_SEC = 120


def wait_for_daily_idle(github_repo: str, github_token: str, max_wait: float = DAILY_IDLE_MAX_WAIT_SEC, poll: float = 10.0) -> bool:
    """
    抓取前先確認每日報告 workflow 沒在跑（它也會用同一組 Telegram session 抓取）。
    兩台機器同時使用同一組 session 會被 Telegram 撤銷（曾造成整個系統癱瘓），所以寧可多等一會兒。
    查詢失敗或等太久都只記 log 並照常繼續（指令回應不能被卡死），回傳是否確定為閒置。
    """
    if not github_token or os.environ.get("BOT_DAILY_TRIGGER", "1") == "0":
        return True
    waited = 0.0
    while True:
        try:
            active = relay.daily_run_active(github_repo, github_token)
        except relay.GitHubApiError as e:
            safe_print(f"⚠️ 無法確認每日報告是否在執行，照常抓取（有小機率與日報同時使用 session）：{relay.redact(e)}")
            return False
        if not active:
            return True
        if waited >= max_wait:
            safe_print(f"⚠️ 每日報告已執行超過 {int(max_wait)} 秒，不再等待，照常抓取（有小機率與日報同時使用 session）")
            return False
        if waited == 0:
            safe_print("⏳ 每日報告正在執行，等它結束再抓取，避免同一組 Telegram session 同時從兩台機器連線")
        time.sleep(poll)
        waited += poll


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
        wait_for_daily_idle(github_repo, github_token)
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
def _int_env(name: str, default: int = 0) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _fmt_tw(ts: float) -> str:
    return datetime.fromtimestamp(ts, TW_ZONE).strftime("%m-%d %H:%M:%S")


def main() -> int:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    github_repo = os.environ.get("GITHUB_REPOSITORY", "dragonheart8787/Corporate-Bond-Bot")

    if not bot_token:
        safe_print("❌ 缺少 TELEGRAM_BOT_TOKEN")
        return 1

    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    event = os.environ.get("GITHUB_EVENT_NAME", "local")
    prev_run_id = os.environ.get("BOT_PREV_RUN_ID", "").strip()
    min_update_id = _int_env("BOT_MIN_UPDATE_ID", 0)
    relay_ref = os.environ.get("BOT_RELAY_REF") or os.environ.get("GITHUB_REF_NAME") or "master"
    dry_run = os.environ.get("BOT_RELAY_DRY_RUN", "") == "1"

    relay_enabled = os.environ.get("BOT_RELAY_ENABLED", "1") != "0"
    daily_enabled = os.environ.get("BOT_DAILY_TRIGGER", "1") != "0"
    if (relay_enabled or daily_enabled) and not github_token and not dry_run:
        safe_print("⚠️ 缺少 GITHUB_TOKEN：接力與每日報告自動觸發都無法運作，只剩 cron 備援")
        relay_enabled = daily_enabled = False

    start_time = time.time()
    deadline = start_time + MAX_RUNTIME_SEC
    now_str = datetime.fromtimestamp(start_time, TW_ZONE).strftime("%Y-%m-%d %H:%M:%S")

    safe_print(f"🤖 Bot 啟動  {now_str} 台灣時間（運行至 {_fmt_tw(deadline)}）")
    safe_print(f"   run={run_id}  觸發方式={event}  ref={relay_ref}")
    safe_print(f"   即時抓取：{'開啟' if can_run_fetch() else '關閉（僅讀 GitHub）'}")
    safe_print(f"   抓取前後停頓：{PRE_FETCH_DELAY_SEC}s / {POST_FETCH_DELAY_SEC}s")
    safe_print(
        f"   接力：{'開啟' if relay_enabled else '關閉'}"
        + (f"（本棒 {_fmt_tw(deadline - RELAY_LEAD_SEC)} 起觸發下一棒）" if relay_enabled else "")
        + f"  每日報告觸發：{'開啟' if daily_enabled else '關閉'}"
        + ("  🧪 DRY-RUN" if dry_run else "")
    )

    # 空窗可觀測性：上一棒什麼時候結束、本棒什麼時候開始
    gap_minutes: float | None = None
    if github_token:
        prev = relay.fetch_prev_run_info(github_repo, github_token, run_id, prev_run_id)
        if prev:
            gap_minutes = max(0.0, (datetime.now(timezone.utc) - prev["ended_at"]).total_seconds() / 60)
            ended_tw = prev["ended_at"].astimezone(TW_ZONE).strftime("%m-%d %H:%M:%S")
            safe_print(
                f"⏱ 上一棒 run={prev['run_id']}（{prev['conclusion']}）結束於 {ended_tw}；"
                f"本棒開始於 {now_str}；空窗 {gap_minutes:.1f} 分鐘"
            )
            if gap_minutes > 5:
                safe_print(f"⚠️ 空窗超過 5 分鐘：這段時間內發出的指令沒有 bot 在聽（下面會列出補處理／通知了幾則）")
    if min_update_id:
        safe_print(f"   交接水位線：update_id < {min_update_id} 視為前一棒已處理")

    # 啟動：取回並分類積壓指令
    offset, collected = fetch_pending_updates(bot_token)
    pending_result = classify_pending(collected, time.time(), min_update_id=min_update_id)
    c = pending_result.counts
    safe_print(
        f"🧹 啟動取回 {c['total']} 則積壓更新：補執行 {c['fresh']}（{FRESH_COMMAND_MAX_AGE_SEC // 60} 分鐘內）、"
        f"過期已通知 {c['expired_notified']}、過期丟棄 {c['expired_dropped']}、重複 {c['duplicate']}、"
        f"非指令 {c['non_command']}、非訊息 {c['non_message']}、前一棒已處理 {c['below_floor']}"
    )
    if pending_result.expired:
        send_expired_notices(bot_token, pending_result.expired, gap_minutes)
    pending = pending_result.fresh

    relay_state = relay.RelayState(
        start_time,
        MAX_RUNTIME_SEC,
        RELAY_LEAD_SEC,
        retry_interval_sec=float(os.environ.get("BOT_RELAY_RETRY_SEC", "120")),   # 正式環境 120 秒；僅測試會縮短
    )
    daily = relay.DailyTrigger(enabled=daily_enabled)
    processed_ids: set = set()
    polls = received = handled = skipped = 0
    last_beat = time.time()
    last_daily_action = "尚未檢查"

    def _do_relay() -> None:
        relay.dispatch_workflow(
            github_repo,
            "bot.yml",
            relay_ref,
            {
                "handoff_prev_run_id": run_id,
                "handoff_min_update_id": str(offset),   # 呼叫當下的 offset（closure），後繼只處理它之後的
                "handoff_fail_count": "0",
            },
            github_token,
            dry_run=dry_run,
        )

    def _do_daily() -> None:
        relay.dispatch_workflow(
            github_repo,
            "daily.yml",
            relay_ref,
            {"mode": "all", "skip_if_sent": "true"},
            github_token,
            dry_run=dry_run,
        )

    def _relay_step() -> None:
        if not relay_enabled:
            return
        res = relay_state.step(time.time(), _do_relay)
        if res == "ok":
            safe_print(
                f"🔁 已觸發下一棒（本棒 run={run_id} 將於 {_fmt_tw(deadline)} 結束；"
                "後繼已排隊，本棒結束後立即接手）"
            )
        elif res == "retry_later":
            safe_print(
                f"⚠️ 接力觸發失敗（累計 {relay_state.failures} 次），"
                f"{int(relay_state.retry_interval_sec)} 秒後重試：{relay_state.last_error}"
            )
        elif res == "gave_up":
            msg = (
                f"接力觸發最終失敗（{relay_state.failures} 次）：{relay_state.last_error}。"
                "下一棒沒有排上，bot 將在本棒結束後離線，直到 cron 備援拉起。"
            )
            relay.gha_error("bot 接力失敗", msg)
            notify_alert(bot_token, "bot " + msg)

    while time.time() < deadline:
        _relay_step()

        action, rdate = daily.tick(
            time.time(),
            datetime.now(TW_ZONE),   # 明確指定台北時區，不依賴 runner 本地時區（TZ 環境變數）
            read_sent=lambda: relay.read_last_sent(github_repo, github_token, relay_ref),
            run_active=lambda: relay.daily_run_active(github_repo, github_token),
            dispatch=_do_daily,
        )
        if action not in ("throttled", "outside_window", "disabled", "cooldown", "holding"):
            last_daily_action = f"{action}({rdate})"
        if action == "dispatch_failed" and rdate not in daily.alerted_dates:
            daily.alerted_dates.add(rdate)
            notify_alert(bot_token, f"bot 觸發每日報告失敗（報告日期 {rdate}），請檢查 Actions。")

        if pending:
            updates, pending = pending, []
        else:
            updates = get_updates(bot_token, offset)
            polls += 1

        # 心跳：每 30 分鐘一行。沒有它，bot 靜悄悄跑 5 小時的 log 只有兩行，
        # 完全分不出「沒人下指令」和「指令沒送進來」。
        if time.time() - last_beat >= 1800:
            last_beat = time.time()
            relay_txt = (
                "已觸發" if relay_state.done
                else f"失敗{relay_state.failures}次" if relay_state.failures
                else f"待命（{_fmt_tw(relay_state.relay_at)}）" if relay_enabled
                else "關閉"
            )
            safe_print(
                f"💓 bot 運作中：已運行 {int((time.time() - start_time) / 60)} 分鐘、"
                f"輪詢 {polls} 次、收到 {received} 則更新、處理指令 {handled}、略過 {skipped}"
                f"｜接力 {relay_txt}｜每日報告 {last_daily_action}"
            )

        if not updates:
            time.sleep(IDLE_SLEEP_SEC)
            continue

        for upd in updates:
            uid = int(upd["update_id"])
            offset = max(offset, uid + 1)
            received += 1

            # update_id 去重：同一個 update 不論從哪裡再出現都只處理一次
            if uid in processed_ids:
                skipped += 1
                safe_print(f"ℹ️ 略過已處理過的 update_id={uid}")
                continue
            processed_ids.add(uid)
            if min_update_id and uid < min_update_id:
                skipped += 1
                safe_print(f"ℹ️ 略過交接水位線以下的 update_id={uid}（前一棒已處理）")
                continue

            msg = extract_message(upd)
            if not msg:
                skipped += 1
                safe_print(f"ℹ️ 略過非訊息更新：{[k for k in upd if k != 'update_id']}")
                continue
            text = (msg.get("text") or "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if not text or not chat_id:
                skipped += 1
                safe_print(f"ℹ️ 略過無文字訊息（{relay.mask_chat_id(chat_id) if chat_id else '?'}）")
                continue

            cmd = parse_command(text)
            # log 只留「聊天室雜湊 + 已知指令名」。chat id 與使用者原文都不輸出（public repo 的 log 人人可見）
            safe_print(f"📨 [{relay.mask_chat_id(chat_id)}] cmd={cmd if cmd in KNOWN_COMMANDS else '(非指令)'}")

            if cmd == "/all":
                handled += 1
                handle_report_command(bot_token, chat_id, github_token, github_repo, "all")
                daily.hold_until_ts = time.time() + DAILY_HOLD_AFTER_FETCH_SEC

            elif cmd == "/cb":
                handled += 1
                handle_report_command(bot_token, chat_id, github_token, github_repo, "cb")
                daily.hold_until_ts = time.time() + DAILY_HOLD_AFTER_FETCH_SEC

            elif cmd == "/status":
                handled += 1
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
                handled += 1
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

    # ── 收尾 ──
    exit_code = 0
    if relay_enabled and not relay_state.done:
        _relay_step()   # 時間到了還沒成功接力：最後再試一次（讓 gave_up 的告警走同一條路）
        if not relay_state.done:
            if not relay_state.gave_up:
                relay_state.gave_up = True
                msg = f"本棒時間已用完仍未觸發下一棒：{relay_state.last_error or '未曾成功'}"
                relay.gha_error("bot 接力失敗", msg)
                notify_alert(bot_token, "bot " + msg)
            exit_code = 1   # 讓這個 run 顯示紅色：接力沒成功是需要被看見的事

    ack_updates(bot_token, offset)
    safe_print(
        f"📊 本棒結算：運行 {int((time.time() - start_time) / 60)} 分鐘、輪詢 {polls} 次、收到 {received} 則更新、"
        f"處理指令 {handled}、略過 {skipped}、接力={'成功' if relay_state.done else '未成功' if relay_enabled else '關閉'}"
    )
    safe_print("⏹️  Bot 運行時間到，" + ("正常退出" if exit_code == 0 else "以失敗狀態退出（接力未成功）") + "（GitHub Actions 將自動重新啟動）")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
