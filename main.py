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
    return os.path.join("outputs", "daily", f"complete_report_{today_str()}.txt")


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
CB_KEYWORDS = ["轉換公司債", "可轉債", "可轉換公司債", "轉債"]

def filter_cb_only(full_report: str) -> str:
    """從 complete_report 中只保留轉換公司債相關公告段落"""
    SEP = "=" * 80       # complete_formatter 用的公告分隔線

    # 分割成各個區塊（分隔線本身不保留）
    blocks = full_report.split(SEP)

    # 第一個區塊是總覽標頭（含日期、總計、CB則數）→ 一定保留
    header = blocks[0] if blocks else ""

    # 篩選含有 CB 關鍵字的公告區塊
    cb_blocks = [
        b for b in blocks[1:]
        if any(kw in b for kw in CB_KEYWORDS)
    ]

    if not cb_blocks:
        date_str = datetime.now(TW).strftime("%Y-%m-%d")
        return f"📊 公司債每日報告 {date_str}\n\n今日無轉換公司債相關公告。"

    # 重新組合：標頭 + 分隔線 + 每則 CB 公告
    result_parts = [header.rstrip()]
    for b in cb_blocks:
        result_parts.append(SEP)
        result_parts.append(b.strip())

    date_str = datetime.now(TW).strftime("%Y-%m-%d")
    total = len(cb_blocks)
    intro = (
        f"🔴 轉換公司債公告 {date_str}  共 {total} 則\n"
        + "=" * 55
    )
    return intro + "\n\n" + ("\n\n" + SEP + "\n").join(
        b.strip() for b in cb_blocks
    )


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
