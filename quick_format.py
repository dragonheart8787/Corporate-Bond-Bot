#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速格式化工具
直接格式化現有的 Telegram 資料
"""

import os
import glob
import csv
import json
from datetime import datetime
from zoneinfo import ZoneInfo

_TW = ZoneInfo("Asia/Taipei")

def safe_print(msg: str):
    """安全輸出，避免編碼錯誤"""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('ascii', errors='ignore').decode('ascii'))

def main():
    """主程式"""
    safe_print("📝 快速格式化 Telegram 公告")
    safe_print("=" * 50)
    
    today = datetime.now(_TW).strftime('%Y%m%d')
    csv_path = f'outputs/daily/telegram_messages_{today}.csv'
    
    if not os.path.exists(csv_path):
        # 自動選擇最新一個 CSV 作為來源，但僅輸出今日資料
        candidates = sorted(glob.glob('outputs/daily/telegram_messages_*.csv'), reverse=True)
        if candidates:
            csv_path = candidates[0]
            safe_print(f"ℹ️ 今日檔不存在，改用最新檔：{os.path.basename(csv_path)}（僅輸出今日訊息）")
        else:
            safe_print(f"❌ 找不到今日或任何歷史檔案：{csv_path}")
            safe_print("請先執行 telegram_api_exporter.py 抓取資料")
            return
    
    # 讀取資料：合併所有 telegram_messages_*.csv，再僅保留今天
    rows = []
    all_csvs = sorted(glob.glob('outputs/daily/telegram_messages_*.csv'), reverse=True)
    if csv_path not in all_csvs:
        all_csvs.insert(0, csv_path)
    used_csvs = []
    for path in all_csvs:
        try:
            with open(path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
            used_csvs.append(os.path.basename(path))
        except Exception:
            continue
    if used_csvs:
        safe_print(f"ℹ️ 已讀取 {len(used_csvs)} 個來源檔：{', '.join(used_csvs[:5])}{' ...' if len(used_csvs) > 5 else ''}")
    
    # 僅保留今天的訊息（容忍多種日期格式：YYYYMMDD 或 YYYY-MM-DD）
    from datetime import datetime as _dt
    def _is_today_date(value: str) -> bool:
        if not value:
            return False
        v = value.strip()
        # 常見格式前10碼：YYYY-MM-DD 或 YYYY/MM/DD 或 YYYYMMDD
        d10 = v[:10]
        # 標準化為 YYYYMMDD
        norm = d10.replace('-', '').replace('/', '').replace(' ', '')
        return norm.startswith(today)

    rows_today = [r for r in rows if _is_today_date(r.get('date', ''))]

    # 去重（以日期+文字為鍵）
    seen = set()
    unique_today = []
    for r in rows_today:
        key = (r.get('date', ''), r.get('text', ''))
        if key in seen:
            continue
        seen.add(key)
        unique_today.append(r)
    rows_today = unique_today
    safe_print(f"✅ 今日公告數：{len(rows_today)}")
    
    # 生成美觀的格式化報告
    output_dir = 'outputs/daily'
    os.makedirs(output_dir, exist_ok=True)
    
    # 偵測轉換公司債相關訊息（加權規則與語境判斷）
    import re as _re
    strong_kw = [
        '可轉債', '轉換公司債', '可轉換公司債', '轉債'
    ]
    medium_kw = [
        '轉換價格', '轉換比率', '轉換期間', '轉換條件', '轉換權', '轉換股數',
        '贖回條款', '強制贖回', '贖回', '回售條款', '回售',
        '轉換價', '轉換價值', '轉換溢價'
    ]
    context_kw = ['可轉', '轉換', '轉債', '公司債', 'cb']
    negative_kw = ['普通公司債', '無擔保公司債', '有擔保公司債']

    def _has_word_boundary_cb(t: str) -> bool:
        # 僅匹配獨立的 CB，避免 PCB 等誤判
        return _re.search(r'(?<![A-Za-z])cb(?![A-Za-z])', t) is not None

    def is_cb(text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        score = 0
        # 強關鍵字
        if any(kw in t for kw in [k.lower() for k in strong_kw]):
            score += 3
        # 獨立 CB 縮寫
        if _has_word_boundary_cb(t) or 'convertible bond' in t:
            score += 2
        # 中關鍵字需搭配語境
        if any(kw in t for kw in [k.lower() for k in medium_kw]) and any(ctx in t for ctx in context_kw):
            score += 1
        # 負面排除：若只提普通/無擔保公司債且無強語彙，降低分數
        if any(kw in t for kw in [k.lower() for k in negative_kw]) and ('可轉' not in t and '轉換' not in t and not _has_word_boundary_cb(t)):
            score -= 2
        return score >= 2
    cb_count = sum(1 for r in rows_today if is_cb(r.get('text', '')))

    # 1. 生成美觀的格式化報告
    formatted_path = os.path.join(output_dir, f'beautiful_report_{today}.txt')
    with open(formatted_path, 'w', encoding='utf-8') as f:
        f.write("📊 Telegram 公開資訊觀測站 - 美觀格式化報告\n")
        f.write("=" * 60 + "\n")
        f.write(f"📅 日期：{today}\n")
        f.write(f"🕐 生成時間：{datetime.now(_TW).strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"📈 總計公告數：{len(rows_today)}\n")
        if cb_count == 0:
            f.write("(無轉換公司債消息)\n\n")
        else:
            f.write(f"🔥 轉換公司債相關：{cb_count} 則\n\n")
        
        # 按時間排序（最新的在前）
        sorted_rows = sorted(rows_today, key=lambda x: x.get('date', ''), reverse=True)
        
        for i, row in enumerate(sorted_rows, 1):
            text = row.get('text', '')
            date = row.get('date', '')
            sender = row.get('from', '')
            
            # 提取公司資訊
            import re
            company_match = re.search(r'#(\d+)\s*#([^發佈]+)', text)
            if company_match:
                company_code = company_match.group(1)
                company_name = company_match.group(2).strip()
            else:
                company_code = "未知"
                company_name = "未知公司"
            
            # 提取發言人
            speaker_match = re.search(r'發言人：\s*([^發言人職稱]+)', text)
            speaker = speaker_match.group(1).strip() if speaker_match else "未提供"
            
            # 提取發言人職稱
            title_match = re.search(r'發言人職稱：\s*([^說明]+)', text)
            title = title_match.group(1).strip() if title_match else "未提供"
            
            # 提取發佈時間
            time_match = re.search(r'發佈時間：(\d{8}\s+\d{2}:\d{2}:\d{2})', text)
            publish_time = time_match.group(1) if time_match else "未提供"
            
            # 判斷公告類型
            if '澄清媒體報導' in text:
                announcement_type = "📢 澄清媒體報導"
            elif '注意交易' in text:
                announcement_type = "⚠️ 注意交易資訊"
            elif '財務' in text or '營收' in text:
                announcement_type = "💰 財務資訊"
            elif '董事' in text or '人事' in text:
                announcement_type = "👥 人事異動"
            else:
                announcement_type = "📋 一般公告"
            
            # 格式化內容
            f.write(f"【第 {i} 則】\n")
            f.write("=" * 50 + "\n")
            f.write(f"🏢 公司：{company_code} {company_name}\n")
            f.write(f"{announcement_type}\n")
            f.write(f"🕐 發佈時間：{publish_time}\n")
            f.write(f"👤 發言人：{speaker}\n")
            f.write(f"💼 職稱：{title}\n")

            # 轉換公司債標示
            if is_cb(text):
                f.write("🔥 轉換公司債相關\n")
            f.write("-" * 50 + "\n")
            
            # 格式化說明內容
            explanation_match = re.search(r'說明：(.+)', text)
            if explanation_match:
                explanation = explanation_match.group(1)
                # 按數字分段
                parts = re.split(r'(\d+\.)', explanation)
                formatted_parts = []
                
                for part in parts:
                    if re.match(r'\d+\.', part):
                        formatted_parts.append(f"\n{part}")
                    elif part.strip():
                        formatted_parts.append(part.strip())
                
                explanation = ''.join(formatted_parts)
                f.write(f"📋 說明：{explanation}\n")
            else:
                f.write(f"📄 內容：{text[:200]}...\n")

            # 分析區塊
            f.write("\n以下為分析：\n")
            analysis_points = []
            lower = (text or '').lower()
            if is_cb(text):
                analysis_points.append("可能規劃/進行可轉債或轉債資訊更新")
            if any(k in lower for k in ['下修轉換價', '調降轉換價', '轉換價格調整']):
                analysis_points.append("轉換價下修，潛在稀釋壓力增加")
            if any(k in lower for k in ['上修轉換價', '調高轉換價']):
                analysis_points.append("轉換價上修，稀釋壓力趨緩")
            if any(k in lower for k in ['贖回條款', '強制贖回', '贖回']):
                analysis_points.append("可轉債進入或接近贖回階段")
            if any(k in lower for k in ['回售條款', '投資人回售', '回售']):
                analysis_points.append("投資人可能啟動回售機制")
            if any(k in lower for k in ['注意交易', '異常波動', '集中交易']):
                analysis_points.append("被列為注意交易，需留意波動與資訊揭露")
            if any(k in lower for k in ['處分', '裁罰', '金管會', '主管機關']):
                analysis_points.append("涉及主管機關事項，可能有合規風險")
            if any(k in lower for k in ['訴訟', '仲裁', '侵權', '官司']):
                analysis_points.append("涉及法律爭議/訴訟風險")
            if any(k in lower for k in ['重大訂單', '接獲訂單', '合作備忘錄', 'mou', '簽約']):
                analysis_points.append("可能取得/洽談訂單或合作")
            if any(k in lower for k in ['現金增資', '私募', '增資', '發行新股']):
                analysis_points.append("可能有籌資需求或股本變動")

            if analysis_points:
                for p in analysis_points:
                    f.write(f"  • {p}\n")
            else:
                f.write("  • 無明顯風險/機會關鍵字\n")
            
            f.write("=" * 50 + "\n\n")
    
    safe_print(f"✅ 美觀格式化報告：{formatted_path}")
    
    # 2. 生成統計摘要
    summary_path = os.path.join(output_dir, f'statistics_{today}.txt')
    with open(summary_path, 'w', encoding='utf-8') as f:
        f.write("📊 每日公告統計摘要\n")
        f.write("=" * 40 + "\n")
        f.write(f"📅 日期：{today}\n")
        f.write(f"📈 總公告數：{len(rows_today)}\n\n")
        
        # 統計公告類型
        type_counts = {}
        companies = set()
        
        for row in rows_today:
            text = row.get('text', '')
            
            if '澄清媒體報導' in text:
                type_counts['澄清媒體報導'] = type_counts.get('澄清媒體報導', 0) + 1
            elif '注意交易' in text:
                type_counts['注意交易'] = type_counts.get('注意交易', 0) + 1
            elif '財務' in text or '營收' in text:
                type_counts['財務資訊'] = type_counts.get('財務資訊', 0) + 1
            elif '董事' in text or '人事' in text:
                type_counts['人事異動'] = type_counts.get('人事異動', 0) + 1
            else:
                type_counts['其他'] = type_counts.get('其他', 0) + 1
            
            # 提取公司代號
            company_match = re.search(r'#(\d+)', text)
            if company_match:
                companies.add(company_match.group(1))
        
        f.write("📋 公告類型統計：\n")
        for announcement_type, count in sorted(type_counts.items(), key=lambda x: x[1], reverse=True):
            percentage = (count / len(rows) * 100) if len(rows) > 0 else 0
            f.write(f"  • {announcement_type}：{count} 則 ({percentage:.1f}%)\n")
        
        f.write(f"\n🏢 涉及公司數：{len(companies)}\n")
        f.write("🏢 公司清單：\n")
        for company in sorted(companies):
            f.write(f"  • {company}\n")
    
    safe_print(f"✅ 統計摘要：{summary_path}")
    
    safe_print("\n🎉 快速格式化完成！")
    safe_print("📁 生成檔案：")
    safe_print(f"  • 美觀報告：{formatted_path}")
    safe_print(f"  • 統計摘要：{summary_path}")

if __name__ == '__main__':
    main()
