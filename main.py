#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公司債每日自動化主程式（GitHub Actions 入口）

用法：
  python main.py fetch   → 執行 auto_telegram_daily.py（抓取 + 格式化 + 生成 complete_report）
  python main.py send    → 讀取 complete_report 並傳送至 Telegram Bot
  python main.py all     → fetch + send（測試用）
"""

import os
import sys
import subprocess
from datetime import datetime, timezone, timedelta

TW = timezone(timedelta(hours=8))


def safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("utf-8", "replace").decode("utf-8"), flush=True)


def today_str() -> str:
    return datetime.now(TW).strftime("%Y%m%d")


def report_path() -> str:
    """找今日報告；若不存在則找最近一份（應對時區邊界情況）"""
    import glob
    exact = os.path.join("outputs", "daily", f"complete_report_{today_str()}.txt")
    if os.path.exists(exact):
        return exact

    # 保底：找 outputs/daily/ 下最新的 complete_report_*.txt
    candidates = sorted(
        glob.glob(os.path.join("outputs", "daily", "complete_report_*.txt")),
        key=os.path.getmtime,
        reverse=True,
    )
    if candidates:
        safe_print(f"ℹ️  今日報告不存在，改用最新：{os.path.basename(candidates[0])}")
        return candidates[0]

    return exact   # 讓後續邏輯顯示正確的找不到錯誤訊息


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
def mode_fetch() -> None:
    safe_print("\n" + "=" * 55)
    safe_print(f"📥 [FETCH] {datetime.now(TW).strftime('%Y-%m-%d %H:%M:%S')} 台灣時間")
    safe_print("=" * 55)

    # 將 GitHub Secrets 傳入子行程環境變數
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

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

    # 確認 complete_report 是否已生成
    path = report_path()
    if os.path.exists(path):
        size = os.path.getsize(path)
        safe_print(f"✅ complete_report 已就緒：{path}（{size:,} bytes）")
    else:
        safe_print(f"❌ 找不到 complete_report：{path}")
        sys.exit(1)


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
    date_str = datetime.now(TW).strftime("%Y-%m-%d")

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
# MODE: send — 讀取 complete_report 並傳送
# ─────────────────────────────────────────
def mode_send() -> None:
    safe_print("\n" + "=" * 55)
    safe_print(f"📤 [SEND] {datetime.now(TW).strftime('%Y-%m-%d %H:%M:%S')} 台灣時間")
    safe_print("=" * 55)

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
    safe_print(f"📄 報告路徑：{path}（原始 {len(full_content):,} 字元 → 過濾後 {len(content):,} 字元）")
    safe_print("▶ 傳送至 Telegram Bot ...")

    ok = send_telegram(bot_token, chat_id, content)
    if ok:
        safe_print("✅ Telegram 傳送完成！")
    else:
        safe_print("⚠️ 部分段落發送失敗")
        sys.exit(1)


# ─────────────────────────────────────────
# 主入口
# ─────────────────────────────────────────
def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    if mode == "fetch":
        mode_fetch()
    elif mode == "send":
        mode_send()
    elif mode == "all":
        mode_fetch()
        safe_print("\n▶ 直接發送（all 模式）")
        mode_send()
    else:
        safe_print(f"❌ 未知模式：{mode}（請使用 fetch / send / all）")
        sys.exit(1)


if __name__ == "__main__":
    main()
