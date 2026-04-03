#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自動化 Telegram 每日訊息抓取與分析
- 從 Telegram API 抓取今日訊息
- 格式化並分類公告
- 生成美觀報告與統計摘要
"""

import os
import sys
import subprocess
import csv
from datetime import datetime
from zoneinfo import ZoneInfo

_TW = ZoneInfo("Asia/Taipei")


def safe_print(msg: str):
    """安全輸出，避免編碼錯誤"""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('ascii', errors='ignore').decode('ascii'))


def ensure_dir(path: str):
    """確保目錄存在"""
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def run_step(step_name: str, cmd: list, timeout: int = 300) -> bool:
    """執行步驟並顯示進度"""
    safe_print(f"\n{'='*60}")
    safe_print(f"🔄 步驟：{step_name}")
    safe_print(f"{'='*60}")
    
    try:
        result = subprocess.run(
            cmd,
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='ignore'
        )
        
        if result.stdout:
            safe_print(result.stdout)
        
        if result.returncode == 0:
            safe_print(f"✅ {step_name} 完成")
            return True
        else:
            safe_print(f"⚠️ {step_name} 回傳碼：{result.returncode}")
            if result.stderr:
                safe_print(f"錯誤訊息：{result.stderr}")
            return False
            
    except subprocess.TimeoutExpired:
        safe_print(f"⚠️ {step_name} 逾時（>{timeout}秒），已終止")
        return False
    except Exception as e:
        safe_print(f"❌ {step_name} 執行錯誤：{e}")
        return False


def main():
    """主流程"""
    safe_print("\n" + "="*60)
    safe_print("🚀 自動化 Telegram 每日訊息分析系統")
    safe_print("="*60)
    safe_print(f"📅 日期：{datetime.now(_TW).strftime('%Y-%m-%d %H:%M:%S')}")
    safe_print("="*60)
    
    # 確保輸出目錄存在
    ensure_dir('outputs/daily')
    ensure_dir('outputs/daily/logs')
    
    today_str = datetime.now(_TW).strftime('%Y%m%d')
    py = sys.executable
    # GitHub Secrets 與本機一致：與 daily.yml 的 TELEGRAM_CHAT_NAME 對齊
    chat = (os.environ.get("TELEGRAM_CHAT_NAME") or "").strip()
    if not chat:
        chat = "📢 [非官方] 公開資訊觀測站 即時重大訊息"
    safe_print(f"📌 頻道名稱（TELEGRAM_CHAT_NAME 或預設）：{chat}")

    def _csv_data_row_count(path: str) -> int:
        if not os.path.exists(path):
            return 0
        try:
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                return max(0, sum(1 for _ in csv.DictReader(f)))
        except Exception:
            return 0

    csv_path = f"outputs/daily/telegram_messages_{today_str}.csv"

    page_size = int(os.environ.get("TELEGRAM_FETCH_PAGE_SIZE", "400"))
    max_today = int(os.environ.get("TELEGRAM_FETCH_MAX_TOTAL", "25000"))
    fallback_limit = int(os.environ.get("TELEGRAM_FETCH_FALLBACK_LIMIT", "8000"))

    # 步驟 1a：分頁抓取台北「今日」直到跨日或達上限（僅抓最新 N 則會漏掉清晨大量公告）
    step1_success = run_step(
        "抓取 Telegram 訊息（台北今日，分頁不漏）",
        [
            py,
            "telegram_api_exporter.py",
            "--chat",
            chat,
            "--paginate-today",
            "--page-size",
            str(page_size),
            "--max-total-messages",
            str(max_today),
        ],
        timeout=900,
    )

    if not step1_success:
        safe_print("\n⚠️ 訊息抓取失敗，嘗試繼續...")

    nrows = _csv_data_row_count(csv_path)
    safe_print(f"ℹ️  exporter 今日篩選後列數：{nrows}")

    # 今日 0 筆或未取得檔案時改抓「不篩日期」，供後續 quick_format / complete_formatter 有資料
    if nrows <= 0:
        if os.path.exists(csv_path):
            safe_print("⚠️ 今日篩選後無資料，改抓取最近訊息（不限日期）…")
        else:
            safe_print("⚠️ 未取得訊息檔，改抓取最近訊息（不限日期）…")
        run_step(
            "抓取 Telegram 最近訊息（不限日期）",
            [py, "telegram_api_exporter.py", "--chat", chat, "--limit", str(fallback_limit)],
            timeout=600,
        )
        nrows = _csv_data_row_count(csv_path)
        safe_print(f"ℹ️  不限日期抓取後列數：{nrows}")

    if not os.path.exists(csv_path):
        safe_print(f"\n❌ 找不到訊息檔：{csv_path}")
        safe_print("請確認 TELEGRAM_* Secrets、TELEGRAM_CHAT_NAME 與 telegram_api_exporter.py")
        return
    
    # 步驟 2：格式化與分類（只處理今日訊息）
    step2_success = run_step(
        "格式化與分類今日公告",
        [py, "quick_format.py"],
        timeout=120
    )
    
    if not step2_success:
        safe_print("\n⚠️ 格式化失敗")
        return
    
    # 步驟 3：生成完整分析報告（含轉換公司債優先）
    step3_success = run_step(
        "生成完整分析報告",
        [py, "complete_formatter.py"],
        timeout=120
    )
    
    if not step3_success:
        safe_print("\n⚠️ 完整分析報告生成失敗")
    
    # 顯示結果
    safe_print("\n" + "="*60)
    safe_print("🎉 自動化分析完成！")
    safe_print("="*60)
    safe_print("\n📁 生成檔案：")
    
    output_files = [
        (f'outputs/daily/beautiful_report_{today_str}.txt', '美觀報告'),
        (f'outputs/daily/statistics_{today_str}.txt', '統計摘要'),
        (f'outputs/daily/complete_report_{today_str}.txt', '完整分析報告'),
        (f'outputs/daily/convertible_bond_summary_{today_str}.txt', '轉換公司債摘要'),
        (f'outputs/daily/company_insights_{today_str}.txt', '公司洞察分析'),
    ]
    
    for fpath, fname in output_files:
        if os.path.exists(fpath):
            size = os.path.getsize(fpath)
            safe_print(f"  ✅ {fname}：{fpath} ({size:,} bytes)")
        else:
            safe_print(f"  ⚠️ {fname}：未生成")
    
    safe_print("\n" + "="*60)
    safe_print("💡 提示：")
    safe_print("  • 所有報告只包含今日訊息")
    safe_print("  • 轉換公司債相關訊息已優先標示")
    safe_print("  • 每則公告都包含「以下為分析」區段")
    safe_print("="*60)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        safe_print("\n\n⚠️ 使用者中斷執行")
        sys.exit(1)
    except Exception as e:
        safe_print(f"\n\n❌ 系統錯誤：{e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
