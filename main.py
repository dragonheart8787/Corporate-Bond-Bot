#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公司債每日自動化主程式（GitHub Actions 入口）

用法：
  python main.py fetch        → 執行 auto_telegram_daily.py（抓取 + 格式化 + 生成 complete_report）
  python main.py send         → 讀取 complete_report 並傳送至 Telegram Bot
  python main.py all          → fetch + send（測試用）
  python main.py report-date  → 印出本次報告的目標日期（YYYYMMDD），供 workflow 使用

環境變數：
  REPORT_DATE             強制指定報告日期（YYYYMMDD 或 YYYY-MM-DD），用於補跑
  REPORT_ROLLOVER_HOUR    台北幾點前視為「延遲跨日」而回退為昨日（預設 6）
  SKIP_IF_ALREADY_SENT    =1 時，若該日期已成功發送過就略過（備援排程用）
"""

import os
import sys
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram_date_utils import report_yyyymmdd

# 與 quick_format / exporter 一致，避免「固定 UTC+8」與 IANA 時區在邊界日期不一致
_TWZ = ZoneInfo("Asia/Taipei")

# 記錄「最後一次成功發送的報告日期」，讓備援排程不會把同一天的報告重發一次
SENT_MARKER = os.path.join("reports", ".last_sent")


def safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("utf-8"), flush=True)


def today_str() -> str:
    """報告目標日期（含延遲跨午夜回退與 REPORT_DATE 覆寫）。"""
    return report_yyyymmdd()


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def already_sent(date_str: str) -> bool:
    try:
        with open(SENT_MARKER, "r", encoding="utf-8") as f:
            return f.read().strip() == date_str
    except OSError:
        return False


def mark_sent(date_str: str) -> None:
    try:
        os.makedirs(os.path.dirname(SENT_MARKER), exist_ok=True)
        with open(SENT_MARKER, "w", encoding="utf-8") as f:
            f.write(date_str + "\n")
        safe_print(f"📝 已記錄發送標記：{SENT_MARKER} = {date_str}")
    except OSError as e:
        safe_print(f"⚠️ 無法寫入發送標記（{e}），備援排程可能重發")


def report_path() -> str:
    """
    依優先順序找報告（僅接受「檔名日期 = 台北今日」，避免送出 4/30 等舊報告）：
    1. outputs/daily/complete_report_{today}.txt
    2. reports/complete_report_latest.txt（僅當內文標頭日期為今日）
    """
    today = today_str()
    exact = os.path.join("outputs", "daily", f"complete_report_{today}.txt")
    if os.path.exists(exact):
        return exact

    repo_report = os.path.join("reports", "complete_report_latest.txt")
    if os.path.exists(repo_report):
        today_dash = f"{today[:4]}-{today[4:6]}-{today[6:8]}"
        try:
            with open(repo_report, "r", encoding="utf-8") as f:
                head = f.read(1200)
            if today_dash in head or f"日期：{today}" in head or f"日期：{today_dash}" in head:
                safe_print("ℹ️  使用 reports/complete_report_latest.txt（標頭為今日）")
                return repo_report
        except OSError:
            pass
        safe_print(
            f"⚠️  略過 reports/complete_report_latest.txt（非今日 {today_dash}，避免重發舊公告）"
        )

    return exact


# ─────────────────────────────────────────
# Telegram Bot 發送
# ─────────────────────────────────────────
def send_telegram(token: str, chat_id: str, text: str) -> bool:
    import requests
    import time

    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    total = len(chunks)
    failed = []

    for idx, chunk in enumerate(chunks, 1):
        header = f"[{idx}/{total}]\n" if total > 1 else ""
        sent = False
        for attempt in range(3):          # 最多重試 3 次
            try:
                resp = requests.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": header + chunk},
                    timeout=30,
                )
                if resp.ok:
                    safe_print(f"  ✅ 已發送第 {idx}/{total} 段")
                    sent = True
                    break
                elif resp.status_code == 429:   # 頻率限制
                    wait = int(resp.json().get("parameters", {}).get("retry_after", 5))
                    safe_print(f"  ⏳ 頻率限制，等待 {wait} 秒後重試...")
                    time.sleep(wait)
                else:
                    safe_print(f"  ⚠️ 第 {idx} 段失敗（{resp.status_code}），重試 {attempt+1}/3...")
                    time.sleep(2)
            except Exception as e:
                safe_print(f"  ⚠️ 第 {idx} 段錯誤（{e}），重試 {attempt+1}/3...")
                time.sleep(3)

        if not sent:
            safe_print(f"  ❌ 第 {idx} 段最終失敗，跳過")
            failed.append(idx)

        time.sleep(0.3)   # 避免連續傳送過快

    if failed:
        safe_print(f"  ⚠️ 共 {len(failed)} 段失敗：{failed}")
        return False
    return True


# ─────────────────────────────────────────
# MODE: fetch — 執行 auto_telegram_daily.py
# ─────────────────────────────────────────
def mode_fetch() -> bool:
    safe_print("\n" + "=" * 55)
    safe_print(f"📥 [FETCH] {datetime.now(_TWZ).strftime('%Y-%m-%d %H:%M:%S')} 台灣時間")
    safe_print("=" * 55)

    # 將 GitHub Secrets 傳入子行程環境變數
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # 日期在此釘死，整條管線（exporter / formatter）都用同一天，避免跨午夜各算各的
    env["REPORT_DATE"] = today_str()

    safe_print("▶ 執行 auto_telegram_daily.py ...")
    result = subprocess.run(
        [sys.executable, "auto_telegram_daily.py"],
        env=env,
        timeout=600,
    )

    if result.returncode != 0:
        safe_print(f"⚠️ auto_telegram_daily.py 回傳碼：{result.returncode}")
        # 不強制退出，嘗試看是否已有報告
    else:
        safe_print("✅ auto_telegram_daily.py 完成")

    # 確認 complete_report 是否「本次新生成」。
    # 注意不能用 report_path()：它會退回 reports/complete_report_latest.txt，
    # 於是抓取整個失敗時仍印出「✅ 已就緒」，把 session 被撤銷這類錯誤蓋掉。
    fresh = os.path.join("outputs", "daily", f"complete_report_{today_str()}.txt")
    if os.path.exists(fresh):
        size = os.path.getsize(fresh)
        safe_print(f"✅ complete_report 已就緒：{fresh}（{size:,} bytes）")
        return True

    safe_print(f"❌ 本次未產生 complete_report：{fresh}")
    if result.returncode != 0:
        safe_print(f"   auto_telegram_daily.py 回傳碼 {result.returncode}，請往上看抓取步驟的錯誤訊息")

    fallback = os.path.join("reports", "complete_report_latest.txt")
    if os.path.exists(fallback):
        safe_print(f"ℹ️  repo 內有備份報告 {fallback}，但那是舊的，不代表本次抓取成功")

    # 不在這裡 exit：mode_send 與 all 會內部呼叫本函式，硬中斷會讓發送步驟被跳過。
    # 由 main() 依回傳值決定行程結束碼。
    return False


# ─────────────────────────────────────────
# 讀取 Bot 憑證（環境變數 → 本機 config 檔）
# ─────────────────────────────────────────
def load_bot_credentials() -> tuple[str, str]:
    """回傳 (bot_token, chat_id)，優先讀環境變數，其次讀本機 config"""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    # 若環境變數未設，嘗試讀本機設定檔
    if not bot_token or not chat_id:
        cfg_path = os.path.join("configs", "telegram_bot_config.json")
        if os.path.exists(cfg_path):
            import json
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            bot_token = bot_token or cfg.get("bot_token", "").strip()
            chat_id   = chat_id   or cfg.get("chat_id", "").strip()
            safe_print(f"ℹ️  使用本機 {cfg_path}")

    return bot_token, chat_id


# ─────────────────────────────────────────
# 過濾：只保留「轉換公司債」分類的公告
# ─────────────────────────────────────────

def filter_cb_only(full_report: str) -> str:
    """
    從 complete_report 中提取完整的「轉換公司債相關公告」區段。
    complete_formatter 的格式：
      🔥 轉換公司債相關公告（共 N 則）
      ============================================================ (60=)
      【轉換公司債 - 第 1 則】
      ...
      【轉換公司債 - 第 N 則】
      ...
      📢 澄清媒體報導 (N 則)  ← 下一個非CB區段（用此作為結尾）
    """
    # 用報告目標日期，不是「現在」：延遲跨午夜時內容是前一日的，標題不能寫成今天
    d = today_str()
    date_str = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    # ── 找 CB 區段的開始位置 ──
    CB_SECTION_MARKERS = [
        "🔥 轉換公司債相關公告",
        "轉換公司債相關公告",
        "【轉換公司債 - 第 1 則】",
    ]
    cb_start = -1
    for m in CB_SECTION_MARKERS:
        idx = full_report.find(m)
        if idx != -1:
            cb_start = idx
            break

    if cb_start == -1:
        return f"📊 公司債每日報告 {date_str}\n\n今日無轉換公司債相關公告。"

    # ── 找 CB 區段的結尾（下一個非CB類別開頭）──
    OTHER_SECTION_MARKERS = [
        "📢 澄清媒體報導",
        "💰 財務資訊",
        "👥 人事異動",
        "⚠️ 注意交易",
        "📋 重大訊息",
        "📄 其他",
        "── 其他公告",
    ]
    cb_end = len(full_report)
    for m in OTHER_SECTION_MARKERS:
        idx = full_report.find(m, cb_start + 50)   # 跳過區段標題本身
        if idx != -1 and idx < cb_end:
            cb_end = idx

    cb_content = full_report[cb_start:cb_end].strip()
    count = cb_content.count("【轉換公司債")

    header = (
        f"🔴 轉換公司債公告  {date_str}\n"
        f"共 {count} 則\n"
        + "=" * 40
    )
    return header + "\n\n" + cb_content


# ─────────────────────────────────────────
# 判斷過濾結果是否為「無公告」
# ─────────────────────────────────────────
def _is_empty_result(content: str) -> bool:
    """回傳 True 表示過濾後無轉換公司債公告"""
    return "今日無轉換公司債" in content or "無轉換公司債相關公告" in content


# ─────────────────────────────────────────
# MODE: send — 讀取 complete_report 並傳送
# ─────────────────────────────────────────
def mode_send() -> None:
    safe_print("\n" + "=" * 55)
    safe_print(f"📤 [SEND] {datetime.now(_TWZ).strftime('%Y-%m-%d %H:%M:%S')} 台灣時間")
    safe_print("=" * 55)
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        safe_print(
            f"🔎 DEBUG: GITHUB_EVENT_NAME={os.environ.get('GITHUB_EVENT_NAME', '')!r} "
            f"RUN_ID={os.environ.get('GITHUB_RUN_ID', '')!r} "
            f"ACTOR={os.environ.get('GITHUB_ACTOR', '')!r}"
        )

    target_date = today_str()
    if target_date != datetime.now(_TWZ).strftime("%Y%m%d"):
        safe_print(
            f"ℹ️  現在是台北 {datetime.now(_TWZ):%H:%M}，判定為延遲跨日，報告目標日期為 {target_date}"
        )

    # 備援排程用：若這一天已經成功發送過，就不要再送一次
    if _truthy("SKIP_IF_ALREADY_SENT") and already_sent(target_date):
        safe_print(f"✅ {target_date} 的報告先前已成功發送（{SENT_MARKER}），略過本次發送")
        return

    bot_token, chat_id = load_bot_credentials()

    if not bot_token:
        safe_print("❌ 缺少 TELEGRAM_BOT_TOKEN，請在 configs/telegram_bot_config.json 或 GitHub Secrets 設定")
        sys.exit(1)
    if not chat_id or "請填入" in chat_id:
        safe_print("❌ 尚未設定 TELEGRAM_CHAT_ID")
        safe_print("   請開啟瀏覽器前往：")
        safe_print(f"   https://api.telegram.org/bot{bot_token}/getUpdates")
        safe_print("   先對 Bot 傳一則訊息，再從 JSON 中找 \"id\" 的數值")
        safe_print("   填入 configs/telegram_bot_config.json 的 chat_id 欄位")
        sys.exit(1)

    path = report_path()
    if not os.path.exists(path):
        safe_print(f"❌ 找不到報告：{path}（請先執行 fetch 步驟）")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as f:
        full_content = f.read()

    # ── 只保留轉換公司債段落 ──
    content = filter_cb_only(full_content)

    # ── 若第一次未抓到可轉債段落：本機可 sleep 後再 fetch；CI 同 job 已跑過 fetch 時應關閉避免重複分頁（省 1 分鐘～十多分鐘）──
    skip_refetch = os.environ.get("SKIP_SEND_REFETCH_ON_EMPTY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if _is_empty_result(content) and skip_refetch:
        safe_print(
            "ℹ️  SKIP_SEND_REFETCH_ON_EMPTY：略過 send 內二次 fetch（與前一步 fetch 重複），直接發送目前過濾結果"
        )
    elif _is_empty_result(content):
        import time

        try:
            wait_sec = int(os.environ.get("SEND_EMPTY_RETRY_SLEEP_SEC", "60"))
        except ValueError:
            wait_sec = 60
        wait_sec = max(0, min(wait_sec, 300))
        safe_print(f"⚠️ 第一次過濾結果為無公告，{wait_sec} 秒後重新抓取確認...")
        time.sleep(wait_sec)
        safe_print("🔄 重新執行 fetch ...")
        mode_fetch()
        path = report_path()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                full_content = f.read()
            content = filter_cb_only(full_content)
            if _is_empty_result(content):
                safe_print("ℹ️ 重試後仍無轉換公司債公告，確認發送無公告訊息")
            else:
                safe_print("✅ 重試後有找到轉換公司債公告")
        else:
            safe_print("⚠️ 重試後仍找不到報告，沿用無公告訊息")

    safe_print(f"📄 報告路徑：{path}（原始 {len(full_content):,} 字元 → 過濾後 {len(content):,} 字元）")

    # Actions 自動跑時附註 run 編號與觸發類型，方便對照「為何手動有、排程沒訊息」（其實是沒觸發或 job 失敗）
    mark = os.environ.get("TELEGRAM_APPEND_ACTIONS_MARKER", "").strip().lower()
    if mark in ("1", "true", "yes", "on") and os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        rn = os.environ.get("GITHUB_RUN_NUMBER", "")
        ev = os.environ.get("GITHUB_EVENT_NAME", "")
        rid = os.environ.get("GITHUB_RUN_ID", "")
        if rn or rid:
            tail = f"\n\n—— CI run #{rn}" + (f" ({ev})" if ev else "") + (f" id={rid}" if rid else "")
            content = content.rstrip() + tail

    safe_print("▶ 傳送至 Telegram Bot ...")

    ok = send_telegram(bot_token, chat_id, content)
    if ok:
        safe_print("✅ Telegram 傳送完成！")
        mark_sent(target_date)
    else:
        # 部分失敗不寫標記，讓備援排程有機會補送
        safe_print("⚠️ 部分段落發送失敗")
        sys.exit(1)


# ─────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────
def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode == "fetch":
        if not mode_fetch():
            sys.exit(1)
    elif mode == "send":
        mode_send()
    elif mode == "all":
        ok = mode_fetch()
        safe_print("\n▶ 直接發送（all 模式）")
        mode_send()          # 抓取失敗仍嘗試發送（可能用 repo 內備份報告）
        if not ok:
            sys.exit(1)
    elif mode == "report-date":
        # 供 workflow 解析一次日期後傳給所有步驟（見 daily.yml）
        print(today_str())
    else:
        safe_print(f"❌ 未知模式：{mode}（請使用 fetch / send / all / report-date）")
        sys.exit(1)


if __name__ == "__main__":
    main()
