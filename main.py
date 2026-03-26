#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公司債每日自動化主程式（GitHub Actions 入口）

流程：
1. 用 Telethon StringSession 從 Telegram 頻道抓取今日訊息
2. 格式化並分類（轉換公司債優先）
3. 生成每日報告
4. 透過 Telegram Bot 發送摘要
"""

import os
import sys
import asyncio
import csv
import re
from datetime import datetime, timezone, timedelta
from typing import List, Dict

# ── 台灣時區（UTC+8）──
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
        # 嘗試多個可能的頻道名稱
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
            safe_print("❌ 找不到任何已知頻道，請確認 chat_name 設定")
            return rows

        async for msg in client.iter_messages(entity, limit=limit):
            if not msg.message:
                continue
            msg_date_tw = msg.date.astimezone(TW).date()
            if msg_date_tw < today_tw:
                break  # 訊息按時間倒序，遇到昨天就停
            rows.append({
                "date": msg.date.astimezone(TW).strftime("%Y-%m-%d %H:%M:%S"),
                "from": getattr(msg.sender, "first_name", "") or "",
                "text": msg.message,
                "links": extract_links(msg.message),
            })

    safe_print(f"📨 今日訊息共 {len(rows)} 則")
    return rows


# ─────────────────────────────────────────
# 2. 儲存 CSV / TXT
# ─────────────────────────────────────────
def save_messages(rows: List[Dict], out_dir: str) -> Dict[str, str]:
    ensure_dir(out_dir)
    today = datetime.now(TW).strftime("%Y%m%d")
    csv_path = os.path.join(out_dir, f"telegram_messages_{today}.csv")
    txt_path = os.path.join(out_dir, f"telegram_messages_{today}.txt")

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "from", "text", "links"])
        writer.writeheader()
        writer.writerows(rows)

    with open(txt_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(f"{r['date']} | {r['from']}\n{r['text']}\n")
            if r["links"]:
                f.write(f"links: {r['links']}\n")
            f.write("-" * 80 + "\n")

    safe_print(f"📄 CSV: {csv_path}")
    safe_print(f"📄 TXT: {txt_path}")
    return {"csv": csv_path, "txt": txt_path}


# ─────────────────────────────────────────
# 3. 格式化（轉換公司債優先）
# ─────────────────────────────────────────
CB_STRONG = ["可轉債", "轉換公司債", "可轉換公司債", "轉債"]
CB_MEDIUM = ["轉換價格", "轉換比率", "贖回條款", "回售條款", "轉換價"]
CB_NEGATIVE = ["普通公司債", "無擔保公司債", "有擔保公司債"]

ANALYSIS_RULES = [
    (["可轉債", "轉換公司債", "CB", "發行可轉", "辦理可轉"], "可能規劃/進行可轉債", "Neutral"),
    (["下修轉換價", "調降轉換價", "轉換價格調整"], "轉換價下修", "Negative"),
    (["上修轉換價", "調高轉換價"], "轉換價上修", "Positive"),
    (["重大訂單", "接獲訂單", "簽約"], "取得訂單/合作", "Positive"),
    (["訴訟", "仲裁", "侵權"], "涉及法律爭議", "Negative"),
    (["財報更正", "重編財報"], "財務資訊異常", "Negative"),
    (["澄清", "媒體報導", "非屬實"], "澄清市場傳聞", "Neutral"),
    (["董事長", "總經理", "辭任"], "重要人事異動", "Neutral"),
]


def is_cb_related(text: str) -> bool:
    t = text.lower()
    score = sum(3 for kw in CB_STRONG if kw in t)
    if re.search(r"(?<![A-Za-z])cb(?![A-Za-z])", t):
        score += 2
    score += sum(1 for kw in CB_MEDIUM if kw in t)
    score -= sum(2 for kw in CB_NEGATIVE if kw in t and "可轉" not in t)
    return score >= 2


def analyze(text: str) -> str:
    t = text.lower()
    tags = []
    for kws, label, impact in ANALYSIS_RULES:
        if any(kw.lower() in t for kw in kws):
            emoji = "📈" if impact == "Positive" else ("📉" if impact == "Negative" else "📌")
            tags.append(f"{emoji} {label}")
    return "、".join(tags) if tags else "📌 一般公告"


def format_report(rows: List[Dict]) -> str:
    today_str = datetime.now(TW).strftime("%Y-%m-%d")
    cb_rows = [r for r in rows if is_cb_related(r["text"])]
    other_rows = [r for r in rows if not is_cb_related(r["text"])]

    lines = [
        f"📊 公司債每日摘要 {today_str}",
        f"共 {len(rows)} 則訊息｜轉換公司債相關：{len(cb_rows)} 則",
        "=" * 40,
    ]

    if cb_rows:
        lines.append(f"\n🔴 轉換公司債相關（{len(cb_rows)} 則）")
        for i, r in enumerate(cb_rows[:20], 1):
            text_short = r["text"][:120].replace("\n", " ")
            tag = analyze(r["text"])
            lines.append(f"\n[{i}] {r['date']}\n{text_short}...\n→ {tag}")
            if r["links"]:
                lines.append(f"🔗 {r['links'][:100]}")

    if other_rows:
        lines.append(f"\n📋 其他重大訊息（{len(other_rows)} 則，顯示前 10 則）")
        for i, r in enumerate(other_rows[:10], 1):
            text_short = r["text"][:80].replace("\n", " ")
            lines.append(f"\n[{i}] {r['date']} {text_short}...")

    lines.append("\n" + "=" * 40)
    lines.append("🤖 由 GitHub Actions 自動產生")
    return "\n".join(lines)


def save_report(report: str, out_dir: str) -> str:
    ensure_dir(out_dir)
    today = datetime.now(TW).strftime("%Y%m%d")
    path = os.path.join(out_dir, f"daily_report_{today}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    safe_print(f"📄 報告: {path}")
    return path


# ─────────────────────────────────────────
# 4. Telegram Bot 發送
# ─────────────────────────────────────────
def send_telegram(token: str, chat_id: str, text: str) -> bool:
    import requests

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram 單則訊息最多 4096 字元，超過則分段
    chunks = [text[i : i + 4000] for i in range(0, len(text), 4000)]
    success = True
    for chunk in chunks:
        try:
            resp = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk, "parse_mode": ""},
                timeout=30,
            )
            if not resp.ok:
                safe_print(f"⚠️ Telegram 發送失敗：{resp.status_code} {resp.text}")
                success = False
        except Exception as e:
            safe_print(f"❌ Telegram 發送錯誤：{e}")
            success = False
    return success


# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────
async def async_main() -> None:
    safe_print("\n" + "=" * 50)
    safe_print("🚀 公司債每日自動化系統啟動")
    safe_print(f"📅 台灣時間：{datetime.now(TW).strftime('%Y-%m-%d %H:%M:%S')}")
    safe_print("=" * 50)

    # ── 讀取環境變數（GitHub Actions Secrets）──
    api_id_str = os.environ.get("TELEGRAM_API_ID", "")
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    session_string = os.environ.get("TELEGRAM_SESSION_STRING", "")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    chat_name = os.environ.get(
        "TELEGRAM_CHAT_NAME",
        "📢 [非官方] 公開資訊觀測站 即時重大訊息",
    )

    # 若環境變數不存在，嘗試讀取本機 configs
    if not api_id_str or not api_hash:
        cfg_path = os.path.join("configs", "telegram_api.json")
        if os.path.exists(cfg_path):
            import json
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            api_id_str = str(cfg.get("api_id", ""))
            api_hash = cfg.get("api_hash", "")
            safe_print("ℹ️ 使用本機 configs/telegram_api.json")

    if not api_id_str or not api_hash:
        safe_print("❌ 缺少 TELEGRAM_API_ID / TELEGRAM_API_HASH，請設定 GitHub Secrets")
        sys.exit(1)

    if not session_string:
        safe_print("❌ 缺少 TELEGRAM_SESSION_STRING，請執行 get_session_string.py 產生")
        sys.exit(1)

    out_dir = "outputs/daily"

    # 步驟 1：抓取訊息
    safe_print("\n▶ 步驟 1：抓取 Telegram 今日訊息")
    rows = await fetch_telegram_messages(
        api_id=int(api_id_str),
        api_hash=api_hash,
        session_string=session_string,
        chat_name=chat_name,
        limit=500,
    )

    if not rows:
        safe_print("⚠️ 今日無訊息，結束執行")
        if bot_token and chat_id:
            today_str = datetime.now(TW).strftime("%Y-%m-%d")
            send_telegram(bot_token, chat_id, f"📊 公司債每日摘要 {today_str}\n今日無新訊息。")
        return

    # 步驟 2：儲存原始資料
    safe_print("\n▶ 步驟 2：儲存原始資料")
    save_messages(rows, out_dir)

    # 步驟 3：格式化報告
    safe_print("\n▶ 步驟 3：格式化報告")
    report = format_report(rows)
    save_report(report, out_dir)
    safe_print(f"\n{'='*50}\n{report[:500]}...\n{'='*50}")

    # 步驟 4：Telegram Bot 發送
    if bot_token and chat_id:
        safe_print("\n▶ 步驟 4：Telegram Bot 發送")
        ok = send_telegram(bot_token, chat_id, report)
        if ok:
            safe_print("✅ Telegram 發送成功")
        else:
            safe_print("⚠️ Telegram 發送部分失敗")
    else:
        safe_print("ℹ️ 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，跳過發送")

    safe_print("\n✅ 每日自動化完成")


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
