#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
archive-fsc 失敗／恢復通知（只用標準函式庫）。由 archive-fsc.yml 的「通知」步驟執行；送訊沿用 bot_relay.send_telegram_alert。

什麼時候發（只看「本 workflow、同一分支、已完成、非 dry_run、非本次」的前幾次 run，由新到舊）：
  本次失敗、前一次成功（或沒有前一次）       → 發「連續失敗第 1 次」
  本次失敗、連續失敗次數 N 是 6 的倍數        → 重發（N 含本次）
  本次成功、前一次失敗                        → 發「已恢復」
  其他                                        → 不發
  查詢前幾次 run 的 API 失敗：本次失敗 → 發（寧可多發不要漏發，連續次數寫「無法查詢」並在 log 說明）；本次成功 → 不發（不知道有沒有失敗過）。
略過的 run：進行中、已取消／略過、dry_run（run-name 帶「（dry-run）」）。

不變量：這個程式永遠以 0 結束——通知本身失敗只寫 ::warning:: 與 log，絕不改變原本的失敗結果（失敗閘門步驟另有 exit 1）。
訊息只含：workflow 名稱、run 連結、失敗年度與失敗類別、各年度最後成功檢查日、連續失敗次數。
不含公司名稱、金額、token、chat id（失敗類別取自既有 ::error:: 的分類，不轉貼 ::error:: 原文）。

測試開關 ALERT_TEST=1：只把訊息內容印到 log，絕不送出（沒有任何會真實發送的測試模式）。ARCHIVE_DRY_RUN=1：不通知。

環境變數：
  ARCHIVE_OUTCOME / COMMIT_OUTCOME  抓取並存檔、提交存檔兩個步驟的 outcome（success|failure|skipped|cancelled）
  ARCHIVE_LOG                       抓取並存檔步驟的輸出檔（用來分類失敗）
  ARCHIVE_FSC_DIR                   data/fsc 目錄（讀 _last_check.json，預設 data/fsc）
  GITHUB_REPOSITORY / GITHUB_RUN_ID / GITHUB_REF_NAME / GITHUB_SERVER_URL / GITHUB_API_URL / GITHUB_WORKFLOW
  ALERT_WORKFLOW_FILE               查詢哪個 workflow 的歷史（預設 archive-fsc.yml）
  GH_API_TOKEN                      查詢 API 用（github.token，只需 actions: read）
  TELEGRAM_BOT_TOKEN / BOT_ALERT_CHAT_ID  送訊用（與 daily.yml 的告警相同的兩個 secret）
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

REPEAT_EVERY = 6                 # 連續失敗每 6 次重發（6、12、18…）
DRY_RUN_MARK = "dry-run"         # archive-fsc.yml 的 run-name 在 dry_run 時帶「（dry-run）」
PER_PAGE = 100
MAX_PAGES = 5                    # 最多回溯 500 次 run（每天 2 班約 8 個月）
API_TIMEOUT = 20
FAIL_CONCLUSIONS = ("failure", "timed_out", "startup_failure")
SUCCESS_CONCLUSIONS = ("success",)

CAT_PAGE = "頁面找不到"
CAT_LINK = "連結異常"
CAT_GATE = "閘門擋下"
CAT_DOWNLOAD = "下載錯誤"
CAT_CONTENT = "檔案內容異常"
CAT_PUSH = "提交推送失敗"
CAT_OTHER = "其他"


def log(msg: str) -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────
# 失敗分類（取自 archive_fsc.py 既有的 ::error:: 訊息；只輸出類別，不轉貼原文）
# ─────────────────────────────────────────
@dataclass
class Failure:
    scope: str          # 年度標籤（例如 115年度申報案件）或「頁面」「最後檢查紀錄」
    category: str


_ERR_LINE = re.compile(r"^::error title=留存失敗 (?P<scope>[^:]*)::(?P<msg>.*)$")


def classify(scope: str, msg: str) -> str:
    if "格式切換閘門" in msg:
        return CAT_GATE
    if "找不到儲存格文字完全等於" in msg or re.search(r"頁面上有 \d+ 列的儲存格文字完全等於", msg):
        return CAT_PAGE
    if scope == "頁面":
        return CAT_DOWNLOAD
    if scope == "最後檢查紀錄":
        return CAT_OTHER
    if (re.search(r"沒有 \.xlsx|沒有 \.ods|個不同的 \.xlsx|個不同的 \.ods|連結不是 https|連結主機|不在白名單", msg)):
        return CAT_LINK
    if re.search(r"HTTP \d{3}|連線失敗|逾時|回應超過大小上限|轉址目的主機", msg):
        return CAT_DOWNLOAD
    if re.search(r"無法解析|工作表|表頭|資料列|證券代號|DTD|zip|mimetype|結構|ods|xlsx", msg):
        return CAT_CONTENT
    return CAT_OTHER


def parse_failures(log_text: str) -> List[Failure]:
    out: List[Failure] = []
    for line in (log_text or "").splitlines():
        m = _ERR_LINE.match(line.strip())
        if m:
            out.append(Failure(m.group("scope").strip(), classify(m.group("scope").strip(), m.group("msg"))))
    return out


# ─────────────────────────────────────────
# 判斷
# ─────────────────────────────────────────
@dataclass
class Decision:
    kind: str                       # first | repeat | unknown | recovered | none
    streak: Optional[int]           # 本次失敗時含本次的連續失敗次數；恢復時為恢復前連續失敗的次數；無法查詢為 None
    at_least: bool = False          # 回溯上限內全部是失敗：實際次數可能更多
    reason: str = ""

    @property
    def send(self) -> bool:
        return self.kind in ("first", "repeat", "unknown", "recovered")


def decide(current_failed: bool, history: Optional[List[str]]) -> Decision:
    """
    history：前幾次（已排除本次、dry_run、進行中、取消）的結論，由新到舊，每個是 "success" 或 "failure"；None = 查詢失敗。
    """
    if history is None:
        if current_failed:
            return Decision("unknown", None, reason="無法查詢前幾次 run，依預設發通知（寧可多發不要漏發）")
        return Decision("none", None, reason="本次成功，但無法查詢前幾次 run，不知道有沒有失敗過，不發「已恢復」")
    prior_fail = 0
    for h in history:
        if h != "failure":
            break
        prior_fail += 1
    exhausted = prior_fail == len(history) and len(history) > 0
    if current_failed:
        n = prior_fail + 1
        if prior_fail == 0:
            return Decision("first", 1, reason="連續失敗第 1 次")
        if n % REPEAT_EVERY == 0:
            return Decision("repeat", n, at_least=exhausted, reason=f"連續失敗第 {n} 次（每 {REPEAT_EVERY} 次重發）")
        return Decision("none", n, at_least=exhausted, reason=f"連續失敗第 {n} 次，不是 {REPEAT_EVERY} 的倍數，不重發")
    if prior_fail > 0:
        return Decision("recovered", prior_fail, at_least=exhausted, reason=f"連續失敗 {prior_fail} 次後本次成功")
    return Decision("none", 0, reason="本次成功且前一次也成功（或沒有前一次）")


# ─────────────────────────────────────────
# 查詢前幾次 run
# ─────────────────────────────────────────
def conclusion_of(run: Dict[str, Any]) -> Optional[str]:
    """已完成 run 的結論：success | failure；取消、略過、dry_run、進行中 → None（不計入）。"""
    if run.get("status") != "completed":
        return None
    title = " ".join(str(run.get(k) or "") for k in ("display_title", "name"))
    if DRY_RUN_MARK in title:
        return None
    c = run.get("conclusion")
    if c in SUCCESS_CONCLUSIONS:
        return "success"
    if c in FAIL_CONCLUSIONS:
        return "failure"
    return None


def fetch_history(env: Dict[str, str], opener: Callable[..., Any] = urllib.request.urlopen) -> List[str]:
    """前幾次 run 的結論（新到舊）。遇到第一個成功就停止回溯。任何 API 問題都拋例外（呼叫端據此走「無法查詢」）。"""
    repo = env.get("GITHUB_REPOSITORY", "")
    if not repo or "/" not in repo:
        raise RuntimeError("缺少 GITHUB_REPOSITORY")
    token = env.get("GH_API_TOKEN", "")
    if not token:
        raise RuntimeError("缺少 GH_API_TOKEN")
    base = (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    wf = urllib.parse.quote(env.get("ALERT_WORKFLOW_FILE") or "archive-fsc.yml", safe="")
    current = str(env.get("GITHUB_RUN_ID", ""))
    branch = env.get("GITHUB_REF_NAME", "")
    out: List[str] = []
    for page in range(1, MAX_PAGES + 1):
        q = {"status": "completed", "per_page": str(PER_PAGE), "page": str(page)}
        if branch:
            q["branch"] = branch
        url = f"{base}/repos/{repo}/actions/workflows/{wf}/runs?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Corporate-Bond-Bot-archive-fsc-alert"})
        with opener(req, timeout=API_TIMEOUT) as resp:
            status = int(getattr(resp, "status", 200))
            if not 200 <= status < 300:
                raise RuntimeError(f"HTTP {status}")
            data = json.loads(resp.read().decode("utf-8"))
        runs = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(runs, list):
            raise RuntimeError("回應結構不符（沒有 workflow_runs 陣列）")
        for r in runs:
            if not isinstance(r, dict) or str(r.get("id")) == current:
                continue
            c = conclusion_of(r)
            if c is not None:
                out.append(c)
        if "success" in out or len(runs) < PER_PAGE:
            break
    return out[:out.index("success") + 1] if "success" in out else out       # 第一個成功之後的歷史與判斷無關


# ─────────────────────────────────────────
# 訊息
# ─────────────────────────────────────────
def last_checks(directory: str) -> str:
    path = os.path.join(directory, "_last_check.json")
    try:
        with open(path, encoding="utf-8") as f:
            years = json.load(f)["years"]
        ok = isinstance(years, dict) and all(isinstance(k, str) and re.fullmatch(r"\d{3}", k) and isinstance(v, str)
                                             and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) for k, v in years.items())
        if not ok or not years:
            return "（紀錄格式異常）"
        return "、".join(f"{y}年={d}" for y, d in sorted(years.items()))
    except (OSError, ValueError, KeyError, TypeError):
        return "（無紀錄）"


def describe_failures(failures: List[Failure], commit_failed: bool, archive_outcome: str) -> str:
    parts: List[str] = []
    seen = set()
    for f in failures:
        key = (f.scope, f.category)
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{f.scope}（{f.category}）")
    if commit_failed:
        parts.append(f"data/fsc 提交推送（{CAT_PUSH}）")
    if not parts:
        parts.append("無法從輸出判斷（" + (CAT_OTHER if archive_outcome != "skipped" else "抓取步驟被略過，前置步驟失敗") + "）")
    return "；".join(parts)


def run_url(env: Dict[str, str]) -> str:
    server = (env.get("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    return f"{server}/{env.get('GITHUB_REPOSITORY', '')}/actions/runs/{env.get('GITHUB_RUN_ID', '')}"


def build_message(env: Dict[str, str], d: Decision, current_failed: bool, failures: List[Failure], commit_failed: bool,
                  directory: str, test: bool = False, checks_text: Optional[str] = None) -> str:
    wf = env.get("GITHUB_WORKFLOW") or "留存證期局申報案件彙總表"
    checks = checks_text if checks_text is not None else last_checks(directory)
    lines: List[str] = []
    if current_failed:
        if d.streak is None:
            streak_txt = "連續失敗次數：無法查詢（API 失敗，依預設通知）"
        else:
            streak_txt = f"連續失敗：{'至少 ' if d.at_least else ''}{d.streak} 次" + ("（前一次成功）" if d.streak == 1 else "")
        head = "證期局申報彙總表留存失敗" + ("（仍未恢復）" if d.kind == "repeat" else "")
        lines += [head, f"workflow：{wf}", f"run：{run_url(env)}",
                  f"失敗項目：{describe_failures(failures, commit_failed, env.get('ARCHIVE_OUTCOME', ''))}",
                  f"各年度最後成功檢查日：{checks}", streak_txt,
                  "成功的年度已照常寫入；詳細原因請看 run 頁面的錯誤註解。"]
    else:
        lines += ["證期局申報彙總表留存已恢復", f"workflow：{wf}", f"run：{run_url(env)}",
                  f"先前連續失敗：{'至少 ' if d.at_least else ''}{d.streak} 次，本次兩個年度都成功",
                  f"各年度最後成功檢查日：{checks}"]
    if test:
        lines.insert(0, "【測試預覽，不會送出】")
    return "\n".join(lines)


# 測試樣本：test_alert=true 時，不論本次成功或失敗，都用「同一組函式」組出一則失敗樣本與一則恢復樣本印到 log。
# 年度、類別、日期、次數全是合成的（不讀 _last_check.json、不讀 ::error:: 輸出），只有 workflow 名稱與 run 連結是本次 run 自己的資訊。
SAMPLE_CHECKS = "114年=2026-01-01、115年=2026-01-01（合成樣本）"
SAMPLE_FAILURES = [Failure("115年度申報案件", CAT_GATE), Failure("114年度申報案件", CAT_DOWNLOAD)]


def sample_messages(env: Dict[str, str]) -> Tuple[str, str]:
    """(失敗樣本, 恢復樣本)。與真實訊息同一個 build_message，內容是合成的。"""
    fail = build_message(env, Decision("first", 1), True, SAMPLE_FAILURES, False, "", test=True, checks_text=SAMPLE_CHECKS)
    rec = build_message(env, Decision("recovered", 3), False, [], False, "", test=True, checks_text=SAMPLE_CHECKS)
    return fail, rec


# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────
def main(env: Optional[Dict[str, str]] = None, *, opener: Callable[..., Any] = urllib.request.urlopen,
         send: Optional[Callable[[str], bool]] = None) -> int:
    env = dict(os.environ if env is None else env)
    try:
        return _main(env, opener, send)
    except Exception as e:  # 通知本身出任何事都不能改變原本的結果
        log(f"::warning title=通知程式例外::{type(e).__name__}（不影響留存結果；失敗閘門另行處理）")
        return 0


def _main(env: Dict[str, str], opener: Callable[..., Any], send: Optional[Callable[[str], bool]]) -> int:
    if env.get("ARCHIVE_DRY_RUN", "") == "1":
        log("dry_run：不通知。")
        return 0
    test = env.get("ALERT_TEST", "") == "1"
    archive_outcome = env.get("ARCHIVE_OUTCOME", "")
    commit_outcome = env.get("COMMIT_OUTCOME", "")
    if archive_outcome not in ("success", "failure", "skipped", "cancelled") or commit_outcome not in ("success", "failure", "skipped", "cancelled", ""):
        log("::warning title=通知步驟收到未知的步驟結果::ARCHIVE_OUTCOME／COMMIT_OUTCOME 不是預期的值，為避免誤報，本次不通知")
        return 0
    if archive_outcome == "cancelled" or commit_outcome == "cancelled":
        log("run 被取消：不通知。")
        return 0
    archive_failed = archive_outcome != "success"        # failure 或 skipped（前置步驟失敗）
    commit_failed = commit_outcome == "failure"
    current_failed = archive_failed or commit_failed
    directory = env.get("ARCHIVE_FSC_DIR") or os.path.join("data", "fsc")
    failures: List[Failure] = []
    if current_failed:
        try:
            with open(env.get("ARCHIVE_LOG", ""), encoding="utf-8", errors="replace") as f:
                failures = parse_failures(f.read())
        except OSError:
            log("ℹ️ 讀不到抓取步驟的輸出檔，失敗細節只能寫「無法從輸出判斷」")

    history: Optional[List[str]]
    try:
        history = fetch_history(env, opener)
    except Exception as e:
        history = None
        log(f"⚠️ 查詢前幾次 run 失敗（{type(e).__name__}: {str(e)[:80]}）：" + ("依預設發通知（寧可多發不要漏發）。" if current_failed else "本次成功，不發「已恢復」。"))
    d = decide(current_failed, history)
    log(f"判斷：本次{'失敗' if current_failed else '成功'}｜前幾次（新→舊）：{history if history is None else history[:8]}｜{d.reason}｜{'發送' if d.send else '不發'}")

    if test:
        # 測試模式：只印、絕不送出（這個分支一定在送出之前 return；沒有任何會真實發送的開關）
        secrets_empty = not (env.get("TELEGRAM_BOT_TOKEN") or env.get("BOT_ALERT_CHAT_ID"))
        log(f"TEST_ALERT=true：不會呼叫 Telegram；Telegram secrets 已被替換為空字串：{'是' if secrets_empty else '否（異常，仍不會送出）'}")
        if not secrets_empty:
            log("::warning title=test_alert 時 secrets 應為空字串::workflow 的 env 設定可能被改動，請檢查；本次仍不會送出")
        if d.send:
            log("【本次真實狀態會發送的內容（預覽，不送出）】")
            log("----")
            log(build_message(env, d, current_failed, failures, commit_failed, directory, test=True))
            log("----")
        else:
            log("本次真實狀態不需要發送通知（見上方判斷）。")
        fail_sample, rec_sample = sample_messages(env)
        log("【失敗樣本（合成資料，與真實訊息同一組函式產生）】")
        log("----")
        log(fail_sample)
        log("----")
        log("【恢復樣本（合成資料，與真實訊息同一組函式產生）】")
        log("----")
        log(rec_sample)
        log("----")
        return 0
    if not d.send:
        return 0

    msg = build_message(env, d, current_failed, failures, commit_failed, directory)
    log("通知內容：\n" + msg)
    if send is None:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        import bot_relay                                   # 既有的告警能力（token／chat id 走環境變數，不進 log）

        def send(text: str) -> bool:
            return bot_relay.send_telegram_alert(text, {"TELEGRAM_BOT_TOKEN": env.get("TELEGRAM_BOT_TOKEN", ""),
                                                        "BOT_ALERT_CHAT_ID": env.get("BOT_ALERT_CHAT_ID", "")})
    ok = False
    try:
        ok = bool(send(msg))
    except Exception as e:
        log(f"❌ 通知送出時發生例外：{type(e).__name__}")
    if not ok:
        log("::warning title=通知未送出::Telegram 通知沒有送出（未設定 secrets 或送出失敗）；不影響留存結果與失敗閘門，請直接查看 Actions 頁面")
    return 0


if __name__ == "__main__":
    sys.exit(main())
