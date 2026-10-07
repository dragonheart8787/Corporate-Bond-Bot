#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot 接力鏈與每日報告觸發（只用標準函式庫）。

背景（2026-10 實測）：GitHub 的 cron 排程大量不觸發——bot 每 2 小時一班，一天約 11 次
機會只建立 3 個 run；每日報告 4 班，當天 UTC 一個都沒建立。bot 因此反覆空窗。

做法：不再把「bot 一定有人接手」押在 cron 上，改由 bot 自己接力：
  1. 本棒跑到尾聲前，用 workflow_dispatch 觸發下一棒（RelayState）。
  2. bot 在台北固定時段自己去觸發每日報告 workflow（DailyTrigger）。
  3. 本棒若在啟動階段就失敗（例如 pip 失敗），由 `python bot_relay.py recover` 代為排下一棒。
cron 仍保留作備援；單一 bot 由 bot.yml 的 concurrency group 保證。

為什麼只用標準函式庫：recover 要在 `pip install` 失敗之後執行，不能依賴 requests。

GITHUB_TOKEN 的限制（依 GitHub 文件，本環境無法重新驗證）：用 GITHUB_TOKEN 觸發的事件通常不會
建立新的 workflow run，但 workflow_dispatch 與 repository_dispatch 是例外。本模組只使用
workflow_dispatch。若該行為不成立，dispatch 會回非 2xx 或 run 不出現——兩者都會被明確記錄並告警，
cron 備援不受影響。

硬性規則：任何外部呼叫失敗都必須有明確的錯誤路徑與 log，不得靜默；log 中不得出現 token、
session、chat id（chat id 一律遮罩）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from telegram_date_utils import DEFAULT_ROLLOVER_HOUR, report_yyyymmdd

TW_ZONE = ZoneInfo("Asia/Taipei")
API_ROOT = "https://api.github.com"

# 本棒連續「啟動即失敗」超過這個次數就停止自救，交給 cron 備援，避免壞掉的程式無限自我重啟
MAX_CONSECUTIVE_START_FAILURES = 4

# bot 單棒可執行時間（分鐘）的合法範圍與預設。預設 300 = 5 小時，接力點 = 300 - 15 = 285 分鐘。
DEFAULT_RUNTIME_MIN = 300
MIN_RUNTIME_MIN = 5
MAX_RUNTIME_MIN = 300
DEFAULT_RELAY_LEAD_SEC = 900


# ─────────────────────────────────────────
# 紀錄與遮罩
# ─────────────────────────────────────────
_SECRET_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN",
    "GITHUB_TOKEN",
    "TELEGRAM_SESSION_STRING",
    "TELEGRAM_API_HASH",
)
_SECRET_PATTERNS = [
    re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"),   # Telegram bot token（常出現在 requests 的例外訊息裡）
    re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),    # GitHub token
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
]


def redact(value: Any) -> str:
    """把任何可能含機密的字串遮罩後再輸出。例外訊息請一律先過這個。"""
    text = str(value)
    for name in _SECRET_ENV_NAMES:
        secret = os.environ.get(name, "")
        if len(secret) >= 8:
            text = text.replace(secret, "***")
    for pat in _SECRET_PATTERNS:
        text = pat.sub("***", text)
    return text


def mask_chat_id(chat_id: Any) -> str:
    """chat id 不可進 log（本 repo 是 public，Actions log 任何人都看得到）。用雜湊前綴代替，仍可區分不同聊天室。"""
    digest = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:6]
    return f"chat#{digest}"


def log(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("utf-8"), flush=True)


def gha_error(title: str, message: str) -> None:
    """輸出 GitHub Actions 的 ::error:: 註解，會顯示在 run 摘要頁，比埋在 log 裡更容易被看見。"""
    log(f"::error title={title}::{redact(message)}")


# ─────────────────────────────────────────
# GitHub API（標準函式庫）
# ─────────────────────────────────────────
class GitHubApiError(Exception):
    def __init__(self, status: Optional[int], message: str):
        super().__init__(message)
        self.status = status


class GitHubConfigError(GitHubApiError):
    """設定錯誤（例如缺少 token）。重試沒有意義，with_retry 不會重試它。"""


def _hint_for(status: Optional[int]) -> str:
    if status in (401, 403):
        return "（權限不足：請確認 workflow 有 `permissions: actions: write`，以及 repo 的 Actions 權限設定未禁止）"
    if status == 404:
        return "（找不到：workflow 檔名、ref 或 repo 名稱有誤，或該 workflow 沒有 workflow_dispatch 觸發）"
    if status == 422:
        return "（參數被拒：ref 不存在或 inputs 與 workflow 宣告不符）"
    return ""


def gh_request(
    method: str,
    path: str,
    token: str,
    body: Optional[dict] = None,
    *,
    timeout: float = 20.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Tuple[int, Any]:
    """呼叫 GitHub REST API。非 2xx 與網路錯誤一律拋 GitHubApiError（訊息已遮罩）。"""
    if not token:
        raise GitHubConfigError(None, "缺少 GITHUB_TOKEN，無法呼叫 GitHub API")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "corporate-bond-bot-relay",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(API_ROOT + path, data=data, method=method, headers=headers)
    try:
        with opener(req, timeout=timeout) as resp:
            status = int(resp.status)
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            detail = e.read()[:200].decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise GitHubApiError(e.code, f"HTTP {e.code} {redact(detail)} {_hint_for(e.code)}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GitHubApiError(None, f"網路錯誤：{redact(e)}") from None

    if not 200 <= status < 300:
        raise GitHubApiError(status, f"HTTP {status} {_hint_for(status)}".strip())
    parsed: Any = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError:
            parsed = None
    return status, parsed


def with_retry(
    fn: Callable[[], Any],
    *,
    attempts: int,
    base_delay: float,
    factor: float = 3.0,
    max_delay: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
    label: str = "",
) -> Any:
    """
    指數退避重試。GitHubApiError 一律重試（含權限不足，使用者明確要求），
    GitHubConfigError 不重試。最後一次仍失敗就把最後的例外拋出去，絕不吞掉。
    """
    delay = base_delay
    last: Optional[Exception] = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except GitHubConfigError:
            raise
        except GitHubApiError as e:
            last = e
            log(f"⚠️ {label}失敗（第 {i}/{attempts} 次）：{redact(e)}")
            if i < attempts:
                sleep(min(delay, max_delay))
                delay *= factor
    assert last is not None
    raise last


def dispatch_workflow(
    repo: str,
    workflow_file: str,
    ref: str,
    inputs: Dict[str, str],
    token: str,
    *,
    dry_run: bool = False,
    attempts: int = 3,
    base_delay: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    """觸發 workflow_dispatch（成功 = 2xx，GitHub 實際回 204）。失敗會重試，最終仍失敗則拋 GitHubApiError。"""
    if dry_run:
        # dry-run：只印出「將會送出什麼」，絕不發出請求。inputs 都是非機密的 run id / 計數，可印。
        log(f"🧪 [dry-run] 不會真的呼叫 API：dispatch {workflow_file} ref={ref} inputs={inputs}")
        return

    def _once() -> None:
        gh_request(
            "POST",
            f"/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
            token,
            {"ref": ref, "inputs": inputs},
            opener=opener,
        )

    with_retry(_once, attempts=attempts, base_delay=base_delay, sleep=sleep, label=f"觸發 {workflow_file} ")
    log(f"✅ 已觸發 {workflow_file}（ref={ref}）")


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_prev_run_info(
    repo: str,
    token: str,
    self_run_id: str,
    prev_run_id: str = "",
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Optional[Dict[str, Any]]:
    """
    取得「上一棒」的結束時間，供啟動 log 顯示空窗。純觀測用途，失敗只記 log 不影響 bot 運作。
    有 prev_run_id（接力啟動）就直接查那一棒；否則（cron / 手動啟動）查最近一個已結束且不是自己的 run。
    """
    try:
        if prev_run_id:
            _, data = gh_request("GET", f"/repos/{repo}/actions/runs/{prev_run_id}", token, opener=opener)
            run = data or {}
        else:
            _, data = gh_request(
                "GET",
                f"/repos/{repo}/actions/workflows/bot.yml/runs?status=completed&per_page=5",
                token,
                opener=opener,
            )
            run = next((r for r in (data or {}).get("workflow_runs", []) if str(r.get("id")) != str(self_run_id)), {})
        ended = _parse_ts(run.get("updated_at", "")) if run else None
        if not ended:
            log("ℹ️ 查無上一棒 bot run 的結束時間（可能是第一次啟動）")
            return None
        return {"run_id": run.get("id"), "ended_at": ended, "conclusion": run.get("conclusion")}
    except GitHubApiError as e:
        log(f"⚠️ 查詢上一棒 bot run 失敗（僅影響 log，不影響運作）：{redact(e)}")
        return None


def read_last_sent(
    repo: str,
    token: str,
    ref: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> str:
    """讀取 reports/.last_sent（最後一次成功發送日報的日期）。檔案不存在（404）視為尚未送過，回空字串。"""
    try:
        _, data = gh_request(
            "GET", f"/repos/{repo}/contents/reports/.last_sent?ref={ref}", token, opener=opener
        )
    except GitHubApiError as e:
        if e.status == 404:
            return ""
        raise
    content = (data or {}).get("content", "")
    try:
        return base64.b64decode(content).decode("utf-8").strip()
    except Exception as e:
        raise GitHubApiError(None, f".last_sent 內容無法解碼：{redact(e)}") from None


_ACTIVE_STATUSES = {"queued", "in_progress", "pending", "waiting", "requested"}


def daily_run_active(
    repo: str,
    token: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    """每日報告 workflow 目前是否已有 run 在排隊或執行中，有就不再重複觸發。"""
    _, data = gh_request(
        "GET", f"/repos/{repo}/actions/workflows/daily.yml/runs?per_page=5", token, opener=opener
    )
    return any(r.get("status") in _ACTIVE_STATUSES for r in (data or {}).get("workflow_runs", []))


_PENDING_STATUSES = {"queued", "pending", "waiting", "requested"}   # 不含 in_progress（那是本棒自己）


def pending_successor_exists(
    repo: str,
    token: str,
    self_run_id: str = "",
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    """bot.yml 是否已有「排隊中、尚未開始」的 run（接力後繼、cron 或手動排的）。
    GitHub 同一 concurrency group 只留一個 pending，再 dispatch 會把它取代掉，所以已有就不必再排。"""
    _, data = gh_request(
        "GET", f"/repos/{repo}/actions/workflows/bot.yml/runs?per_page=10", token, opener=opener
    )
    return any(
        r.get("status") in _PENDING_STATUSES and str(r.get("id")) != str(self_run_id)
        for r in (data or {}).get("workflow_runs", [])
    )


def resolve_runtime(env: Optional[Dict[str, str]] = None) -> Tuple[int, int, str]:
    """
    決定本棒執行秒數與接力提前量，回傳 (max_runtime_sec, lead_sec, 說明)。
    來源優先序：BOT_MAX_RUNTIME_MINUTES（workflow_dispatch 輸入 max_runtime_minutes）> BOT_MAX_RUNTIME_SEC（測試用）> 預設 300 分鐘。
    輸入無效或超出 [5, 300] 分鐘時不報錯停擺，而是退回預設／夾到邊界，並在說明中明確標出（呼叫端會印 log）。
    接力提前量 = min(900, 執行時間 / 3)：短測試棒也能在結束前先排好後繼，正式棒維持 15 分鐘。
    """
    env = dict(os.environ if env is None else env)
    raw_min = (env.get("BOT_MAX_RUNTIME_MINUTES") or "").strip()
    raw_sec = (env.get("BOT_MAX_RUNTIME_SEC") or "").strip()
    note = ""
    if raw_min:
        try:
            minutes = int(raw_min)
        except ValueError:
            minutes = DEFAULT_RUNTIME_MIN
            note = f"⚠️ max_runtime_minutes={raw_min!r} 不是整數，改用預設 {DEFAULT_RUNTIME_MIN} 分鐘"
        else:
            clamped = max(MIN_RUNTIME_MIN, min(minutes, MAX_RUNTIME_MIN))
            if clamped != minutes:
                note = f"⚠️ max_runtime_minutes={minutes} 超出允許範圍 {MIN_RUNTIME_MIN}~{MAX_RUNTIME_MIN}，改用 {clamped} 分鐘"
            else:
                note = f"max_runtime_minutes={clamped}（手動指定）"
            minutes = clamped
        runtime = minutes * 60
    elif raw_sec:
        try:
            runtime = max(1, int(raw_sec))
            note = f"BOT_MAX_RUNTIME_SEC={runtime}（環境變數，測試用）"
        except ValueError:
            runtime = DEFAULT_RUNTIME_MIN * 60
            note = f"⚠️ BOT_MAX_RUNTIME_SEC={raw_sec!r} 不是整數，改用預設 {DEFAULT_RUNTIME_MIN} 分鐘"
    else:
        runtime = DEFAULT_RUNTIME_MIN * 60
        note = f"預設 {DEFAULT_RUNTIME_MIN} 分鐘"
    try:
        lead_cfg = int((env.get("BOT_RELAY_LEAD_SEC") or "").strip() or DEFAULT_RELAY_LEAD_SEC)
    except ValueError:
        lead_cfg = DEFAULT_RELAY_LEAD_SEC
    lead = max(1, min(lead_cfg, runtime // 3 if runtime >= 3 else 1))
    return runtime, lead, note


def send_telegram_alert(
    text: str,
    env: Optional[Dict[str, str]] = None,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> bool:
    """
    最終失敗時的 Telegram 告警（標準函式庫，pip 失敗時也能用）。成功回 True。
    沒設定 token / chat id、或送出失敗都回 False 並寫 log——不拋例外，因為它本身就是最後一道通報。
    token、chat id 不會出現在 log 中。
    """
    env = dict(os.environ if env is None else env)
    token = (env.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (env.get("BOT_ALERT_CHAT_ID") or "").strip()
    if not token or not chat:
        log("ℹ️ 未設定 TELEGRAM_BOT_TOKEN / BOT_ALERT_CHAT_ID，告警只寫入 log（run 頁面仍有 ::error:: 註解）")
        return False
    body = json.dumps({"chat_id": chat, "text": "🚨 " + text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener(req, timeout=20) as resp:
            ok = 200 <= int(resp.status) < 300
    except urllib.error.HTTPError as e:
        log(f"❌ Telegram 告警送出失敗：HTTP {e.code}（{mask_chat_id(chat)}）")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"❌ Telegram 告警送出失敗：{redact(e)}（{mask_chat_id(chat)}）")
        return False
    if not ok:
        log(f"❌ Telegram 告警回應非 2xx（{mask_chat_id(chat)}）")
        return False
    log(f"📨 已送出 Telegram 告警（{mask_chat_id(chat)}）")
    return True


# ─────────────────────────────────────────
# 接力狀態機
# ─────────────────────────────────────────
@dataclass
class RelayState:
    """
    本棒在 start_ts + max_runtime_sec - lead_sec 之後開始嘗試觸發下一棒；
    失敗就每 retry_interval_sec 再試，直到本棒時間用完仍失敗才放棄（gave_up）。
    成功只代表 GitHub 回 2xx，不代表下一棒一定會被排進去（見模組說明的限制）。
    """

    start_ts: float
    max_runtime_sec: float
    lead_sec: float
    retry_interval_sec: float = 120.0
    done: bool = False
    gave_up: bool = False
    failures: int = 0
    last_error: str = ""
    next_try_ts: float = 0.0

    @property
    def relay_at(self) -> float:
        return self.start_ts + self.max_runtime_sec - self.lead_sec

    @property
    def deadline(self) -> float:
        return self.start_ts + self.max_runtime_sec

    def step(self, now: float, dispatch_fn: Callable[[], None]) -> str:
        """回傳 idle / ok / retry_later / gave_up。"""
        if self.done or self.gave_up:
            return "idle"
        if now < self.relay_at or now < self.next_try_ts:
            return "idle"
        try:
            dispatch_fn()
        except GitHubApiError as e:
            self.failures += 1
            self.last_error = redact(e)
            if now + self.retry_interval_sec >= self.deadline:
                self.gave_up = True
                return "gave_up"
            self.next_try_ts = now + self.retry_interval_sec
            return "retry_later"
        self.done = True
        return "ok"


# ─────────────────────────────────────────
# 每日報告觸發
# ─────────────────────────────────────────
def _rollover_hour() -> int:
    """與 telegram_date_utils.report_yyyymmdd 一致：台北幾點前視為前一日（預設 6）。"""
    try:
        h = int(os.environ.get("REPORT_ROLLOVER_HOUR", DEFAULT_ROLLOVER_HOUR))
    except ValueError:
        h = DEFAULT_ROLLOVER_HOUR
    return max(0, min(h, 12))


@dataclass
class DailyTrigger:
    """
    在台北 start_hm 之後（含跨午夜到 rollover 點前）反覆檢查：今天的日報還沒成功送出，
    就觸發 daily.yml（mode=all、skip_if_sent=true）。

    起始 18:30 與原 cron 主排程（18:23）同一個思路：全部班次都必須在當天公告大致收齊之後，
    否則會搶先送出不完整日報並寫下 .last_sent 標記，把之後完整的那班擋掉。

    重複觸發是安全的：daily.yml 有自己的 concurrency（排隊）與 skip_if_sent 守門，
    後到的 run 看到 .last_sent 已是今天就會略過；若前一個失敗則剛好等於重試。
    """

    enabled: bool = True
    start_hm: Tuple[int, int] = (18, 30)
    check_interval_sec: float = 300.0
    redispatch_interval_sec: float = 45 * 60.0
    max_attempts_per_date: int = 6
    last_check_ts: float = 0.0
    last_dispatch_ts: float = 0.0
    attempts: Dict[str, int] = field(default_factory=dict)
    # bot 剛處理完 /cb、/all（用同一組 Telegram session 抓取）後的一小段時間內不觸發日報，
    # 避免兩台機器同時使用同一組 session 而被 Telegram 撤銷（2026-09 實際發生過）。
    hold_until_ts: float = 0.0
    hold_window_start: float = 0.0
    _noted: set = field(default_factory=set)
    alerted_dates: set = field(default_factory=set)

    def hold(self, now_ts: float, hold_sec: float = 120.0, max_total_sec: float = 600.0) -> float:
        """
        暫緩觸發日報 hold_sec 秒。連續的 /cb、/all 會延長，但從「這一輪暫緩開始」算起最多 max_total_sec，
        到頂就一定放行（否則有人反覆下指令就能讓日報永遠發不出去）。回傳實際的 hold_until_ts。
        """
        if now_ts >= self.hold_until_ts:
            self.hold_window_start = now_ts   # 前一輪已結束，開新的一輪
        limit = self.hold_window_start + max_total_sec
        want = now_ts + hold_sec
        until = min(want, limit)
        if until < want:
            log(f"⏸ 暫緩觸發日報已達本輪上限 {int(max_total_sec)} 秒，不再延長（到點即放行）")
        self.hold_until_ts = max(self.hold_until_ts, until)
        log(f"⏸ 暫緩觸發每日報告 {int(max(0, self.hold_until_ts - now_ts))} 秒（避免與剛結束的抓取同時使用 Telegram session）")
        return self.hold_until_ts

    def in_window(self, now_tw: datetime) -> bool:
        return (now_tw.hour, now_tw.minute) >= self.start_hm or now_tw.hour < _rollover_hour()

    def _note_once(self, key: str, msg: str) -> None:
        if key not in self._noted:
            self._noted.add(key)
            log(msg)

    def tick(
        self,
        now_ts: float,
        now_tw: datetime,
        *,
        read_sent: Callable[[], str],
        run_active: Callable[[], bool],
        dispatch: Callable[[], None],
    ) -> Tuple[str, str]:
        """回傳 (action, report_date)。action 見下方各分支；dispatch_failed 由呼叫端決定是否告警。"""
        date = report_yyyymmdd(now_tw)
        if not self.enabled:
            return "disabled", date
        if not self.in_window(now_tw):
            return "outside_window", date
        if now_ts < self.hold_until_ts:
            return "holding", date
        if now_ts - self.last_check_ts < self.check_interval_sec:
            return "throttled", date
        self.last_check_ts = now_ts

        n = self.attempts.get(date, 0)
        if n >= self.max_attempts_per_date:
            self._note_once(f"max:{date}", f"⚠️ 報告日期 {date} 已觸發 {n} 次仍未見成功送出，本棒不再自動觸發（cron 備援或手動處理）")
            return "max_attempts", date
        if self.last_dispatch_ts and now_ts - self.last_dispatch_ts < self.redispatch_interval_sec:
            return "cooldown", date

        try:
            sent = read_sent()
        except GitHubApiError as e:
            # 讀不到標記：不能假設「已送」或「未送」。照常觸發，重複由 daily.yml 的 skip_if_sent 擋。
            log(f"⚠️ 讀取 reports/.last_sent 失敗，改為直接觸發（daily.yml 內有防重發守門）：{redact(e)}")
            sent = ""
        if sent == date:
            self._note_once(f"sent:{date}", f"✅ 報告日期 {date} 已成功送出（.last_sent），本棒不需觸發每日報告")
            return "already_sent", date

        try:
            active = run_active()
        except GitHubApiError as e:
            log(f"⚠️ 查詢每日報告 run 狀態失敗，視為沒有進行中的 run：{redact(e)}")
            active = False
        if active:
            return "run_active", date

        self.attempts[date] = n + 1
        self.last_dispatch_ts = now_ts
        try:
            dispatch()
        except GitHubApiError as e:
            gha_error("每日報告觸發失敗", f"報告日期 {date} 第 {n + 1} 次觸發失敗：{e}")
            return "dispatch_failed", date
        log(f"📅 已由 bot 觸發每日報告（報告日期 {date}，第 {n + 1}/{self.max_attempts_per_date} 次）")
        return "dispatched", date


# ─────────────────────────────────────────
# 失敗／被取消／逾時時的自救（bot.yml 最後一步呼叫）
# ─────────────────────────────────────────
def cmd_recover(
    env: Optional[Dict[str, str]] = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> int:
    """
    本棒沒有正常收尾（啟動階段失敗、bot 崩潰、接力沒成功、被取消或逾時）時，代為排下一棒。
    回傳結束碼：0 = 已排（或已有後繼）；1 = 放棄（原因已 ::error:: 並盡力以 Telegram 告警）。

    三道保險，避免壞掉的程式或自己造成無限重啟／互相覆蓋：
      - 連續次數上限：BOT_FAIL_COUNT >= 4 就停止（共最多連續 5 個 run：count 0,1,2,3 各自救一次）。
      - 最小間隔：失敗路徑先退避 60 / 120 / 240 / 480 秒；取消路徑（有 cancel 寬限時間限制）15 / 30 / 45 / 60 秒。
      - 已有排隊中的後繼就不再排（GitHub 同一 group 只留一個 pending，再排會把它取代掉，
        包含使用者手動排的驗證 run）。查詢失敗則照常排（寧可重複，不可無人接手）。
    取消與失敗都計入連續次數，所以「被取消 → 自救 → 又被取消」同樣會在上限停下來。
    """
    env = dict(os.environ if env is None else env)
    try:
        fail_count = int(env.get("BOT_FAIL_COUNT") or 0)
    except ValueError:
        fail_count = 0
    status = (env.get("BOT_JOB_STATUS") or "failure").strip().lower()
    cancelled = status == "cancelled"
    label = "被取消或逾時" if cancelled else "失敗"

    if fail_count >= MAX_CONSECUTIVE_START_FAILURES:
        msg = (
            f"已連續 {fail_count} 次未正常收尾（最近一次：{label}），不再自動重啟（避免壞掉的程式無限重跑）。"
            "請查看上一個失敗 run 的 log；cron 備援仍會嘗試拉起 bot。"
        )
        gha_error("bot 連續失敗，停止自救", msg)
        send_telegram_alert("bot 連續失敗，已停止自動重啟。" + msg, env, opener=opener)
        return 1

    delay = 15 * (fail_count + 1) if cancelled else 60 * (2 ** fail_count)
    log(f"🔁 本棒{label}（連續第 {fail_count + 1} 次），{delay} 秒後檢查並排下一棒")
    sleep(delay)

    repo = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GITHUB_TOKEN", "")
    dry_run = env.get("BOT_RELAY_DRY_RUN", "") == "1"
    if not dry_run:
        try:
            if pending_successor_exists(repo, token, env.get("GITHUB_RUN_ID", ""), opener=opener):
                log("✅ bot.yml 已有排隊中的後繼 run，本棒不再重複排（避免取代掉它）")
                return 0
        except GitHubApiError as e:
            log(f"⚠️ 無法確認是否已有後繼 run，照常觸發（寧可重複也不能沒人接手）：{redact(e)}")

    try:
        dispatch_workflow(
            repo,
            "bot.yml",
            env.get("BOT_RELAY_REF") or env.get("GITHUB_REF_NAME") or "master",
            # 只帶有值的欄位：沒有水位線可帶時不送空字串，避免對 API 做不必要的假設
            {
                k: v
                for k, v in {
                    "handoff_prev_run_id": env.get("GITHUB_RUN_ID", ""),
                    "handoff_fail_count": str(fail_count + 1),
                }.items()
                if v != ""
            },
            token,
            dry_run=dry_run,
            attempts=3 if cancelled else 4,
            base_delay=5 if cancelled else 10,
            sleep=sleep,
            opener=opener,
        )
    except GitHubApiError as e:
        msg = f"本棒{label}，自救接力也失敗：{e}。bot 目前沒有後繼 run，只能等 cron 備援。"
        gha_error("bot 自救接力失敗", msg)
        send_telegram_alert("bot " + msg, env, opener=opener)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "recover":
        sys.exit(cmd_recover())
    print("用法：python bot_relay.py recover", file=sys.stderr)
    sys.exit(2)
