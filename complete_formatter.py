#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
完整格式化工具
特別標示轉換公司債相關公告，並將它們列在第一行
"""

import os
import csv
import re
from datetime import datetime
from typing import List, Dict, Tuple

def safe_print(msg: str):
    """安全輸出，避免編碼錯誤"""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode('ascii', errors='ignore').decode('ascii'))

class CompleteFormatter:
    def __init__(self):
        # 轉換公司債相關關鍵字
        # 轉換公司債判定：加權與語境
        self.cb_strong_kw = ['可轉債', '轉換公司債', '可轉換公司債', '轉債']
        self.cb_medium_kw = [
            '轉換價格', '轉換比率', '轉換期間', '轉換條件', '轉換權', '轉換股數',
            '贖回條款', '強制贖回', '贖回', '回售條款', '回售',
            '轉換價', '轉換價值', '轉換溢價'
        ]
        self.cb_context_kw = ['可轉', '轉換', '轉債', '公司債', 'cb']
        self.cb_negative_kw = ['普通公司債', '無擔保公司債', '有擔保公司債']
        
        # 其他分類關鍵字
        self.categories = {
            '澄清媒體報導': ['澄清', '媒體', '報導', '臆測', '預測'],
            '財務資訊': ['營收', '獲利', '財務', '損益', '現金', '負債', '流動比率'],
            '人事異動': ['董事長', '總經理', '董事', '異動', '新任', '辭職', '解任'],
            '注意交易': ['注意交易', '達公布', '集中交易', '異常'],
            '重大訊息': ['重大訊息', '重大', '公告', '說明']
        }

        # 公司情況研判規則（簡易關鍵字 -> 標籤與影響）
        # impact: Positive / Negative / Neutral
        self.analysis_rules: List[Tuple[List[str], str, str]] = [
            (['可轉債', '轉換公司債', 'CB', '發行可轉', '辦理可轉'], '可能規劃/進行可轉債', 'Neutral'),
            (['現金增資', '私募增資', '發行新股', '增資'], '可能籌資需求', 'Neutral'),
            (['贖回條款', '贖回', '強制贖回'], '可轉債進入贖回階段', 'Neutral'),
            (['回售條款', '投資人回售', '回售'], '投資人可能回售債券', 'Neutral'),
            (['下修轉換價', '調降轉換價', '轉換價格調整', '轉換價調整'], '轉換價下修，潛在稀釋壓力', 'Negative'),
            (['上修轉換價', '調高轉換價'], '轉換價上修，稀釋壓力趨緩', 'Positive'),
            (['重大訂單', '接獲訂單', '簽約', '合作備忘錄', 'MOU'], '取得或洽談訂單/合作', 'Positive'),
            (['訴訟', '仲裁', '侵權', '官司'], '涉及法律爭議/訴訟風險', 'Negative'),
            (['主管機關', '處分', '裁罰', '金管會', '櫃買中心處分'], '遭主管機關處分/關切', 'Negative'),
            (['財報更正', '重編財報', '重述', '會計師意見'], '財務資訊異常或需更正', 'Negative'),
            (['澄清', '媒體報導', '非屬實', '不實報導'], '澄清市場傳聞/媒體報導', 'Neutral'),
            (['重大投資', '併購', '收購', '轉投資'], '規劃/進行投資或併購', 'Neutral'),
            (['董事長', '總經理', '重要人事', '辭任', '解任'], '重要人事異動', 'Neutral'),
            (['注意交易', '集中交易', '異常波動'], '被列為注意交易', 'Neutral'),
            (['營收', '月營收', '累計營收', '年增', '月增'], '營收資訊更新', 'Neutral')
        ]
    
    def is_convertible_bond_related(self, text: str) -> bool:
        """加權規則判定是否為轉換公司債相關"""
        import re as _re
        if not text:
            return False
        t = text.lower()
        score = 0
        if any(kw in t for kw in [k.lower() for k in self.cb_strong_kw]):
            score += 3
        if _re.search(r'(?<![A-Za-z])cb(?![A-Za-z])', t) or 'convertible bond' in t:
            score += 2
        if any(kw in t for kw in [k.lower() for k in self.cb_medium_kw]) and any(ctx in t for ctx in self.cb_context_kw):
            score += 1
        if any(kw in t for kw in [k.lower() for k in self.cb_negative_kw]) and ('可轉' not in t and '轉換' not in t and _re.search(r'(?<![A-Za-z])cb(?![A-Za-z])', t) is None):
            score -= 2
        return score >= 2
    
    def extract_company_info(self, text: str) -> Dict[str, str]:
        """提取公司資訊"""
        info = {}
        
        # 提取公司代號和名稱
        company_match = re.search(r'#(\d+)\s*#([^發佈]+)', text)
        if company_match:
            info['code'] = company_match.group(1)
            info['name'] = company_match.group(2).strip()
        
        # 提取發言人
        speaker_match = re.search(r'發言人：\s*([^發言人職稱]+)', text)
        if speaker_match:
            info['speaker'] = speaker_match.group(1).strip()
        
        # 提取發言人職稱
        title_match = re.search(r'發言人職稱：\s*([^說明]+)', text)
        if title_match:
            info['title'] = title_match.group(1).strip()
        
        # 提取發佈時間
        time_match = re.search(r'發佈時間：(\d{8}\s+\d{2}:\d{2}:\d{2})', text)
        if time_match:
            info['publish_time'] = time_match.group(1)
        
        return info
    
    def categorize_announcement(self, text: str) -> str:
        """智能分類公告類型"""
        text_lower = text.lower()
        
        # 優先檢查轉換公司債
        if self.is_convertible_bond_related(text):
            return '轉換公司債'
        
        for category, keywords in self.categories.items():
            for keyword in keywords:
                if keyword in text_lower:
                    return category
        
        return '其他'

    def analyze_company_situation(self, text: str) -> Dict[str, List[str]]:
        """根據關鍵字對公司情況進行簡易研判，回傳 {'Positive':[], 'Negative':[], 'Neutral':[]}"""
        buckets: Dict[str, List[str]] = {'Positive': [], 'Negative': [], 'Neutral': []}
        text_lower = text.lower()
        for keywords, label, impact in self.analysis_rules:
            for kw in keywords:
                if kw.lower() in text_lower:
                    if label not in buckets[impact]:
                        buckets[impact].append(label)
                    break
        return buckets
    
    def format_announcement_content(self, text: str) -> str:
        """格式化公告內容，改善排版"""
        # 基本清理
        text = re.sub(r'\s+', ' ', text)  # 合併多個空格
        
        # 分段處理
        sections = []
        
        # 提取說明部分
        explanation_match = re.search(r'說明：(.+)', text)
        if explanation_match:
            explanation = explanation_match.group(1)
            
            # 按數字分段
            parts = re.split(r'(\d+\.)', explanation)
            formatted_parts = []
            
            for i, part in enumerate(parts):
                if re.match(r'\d+\.', part):
                    formatted_parts.append(f"\n{part}")
                elif part.strip():
                    formatted_parts.append(part.strip())
            
            explanation = ''.join(formatted_parts)
            sections.append(f"📋 說明：\n{explanation}")
        
        return '\n'.join(sections) if sections else text
    
    def format_single_announcement(self, row: Dict, index: int) -> str:
        """格式化單一公告"""
        text = row.get('text', '')
        date = row.get('date', '')
        sender = row.get('from', '')
        
        # 提取公司資訊
        company_info = self.extract_company_info(text)
        
        # 分類
        category = self.categorize_announcement(text)
        
        # 格式化內容
        formatted_content = self.format_announcement_content(text)
        
        # 組裝最終格式
        result = []
        result.append("=" * 80)
        
        # 標題行
        if company_info.get('code') and company_info.get('name'):
            result.append(f"🏢 {company_info['code']} {company_info['name']}")
        else:
            result.append("🏢 公司公告")
        
        # 分類標籤 - 轉換公司債特別標示
        if category == '轉換公司債':
            result.append(f"🔥 分類：{category} ⭐ 重要")
        else:
            category_emoji = {
                '澄清媒體報導': '📢',
                '財務資訊': '💰',
                '人事異動': '👥',
                '注意交易': '⚠️',
                '重大訊息': '📋',
                '其他': '📄'
            }
            result.append(f"{category_emoji.get(category, '📄')} 分類：{category}")
        
        # 時間和發言人
        if company_info.get('publish_time'):
            result.append(f"🕐 發佈時間：{company_info['publish_time']}")
        if company_info.get('speaker'):
            result.append(f"👤 發言人：{company_info['speaker']}")
        if company_info.get('title'):
            result.append(f"💼 職稱：{company_info['title']}")
        
        result.append("-" * 80)
        
        # 內容
        result.append(formatted_content)

        # 公司情況研判
        analysis = self.analyze_company_situation(text)
        if any(analysis.values()):
            result.append("")
            result.append("🔎 公司情況研判：")
            if analysis['Positive']:
                result.append(f"  ✅ 正向：{'; '.join(analysis['Positive'])}")
            if analysis['Negative']:
                result.append(f"  ❗ 負向：{'; '.join(analysis['Negative'])}")
            if analysis['Neutral']:
                result.append(f"  ℹ 中性：{'; '.join(analysis['Neutral'])}")
        
        result.append("=" * 80)
        result.append("")
        
        return '\n'.join(result)
    
    def format_with_convertible_bond_priority(self, rows: List[Dict]) -> str:
        """按轉換公司債優先順序格式化所有公告"""
        # 分離轉換公司債和其他公告
        convertible_bond_announcements = []
        other_announcements = []
        
        for row in rows:
            if self.is_convertible_bond_related(row.get('text', '')):
                convertible_bond_announcements.append(row)
            else:
                other_announcements.append(row)
        
        # 生成格式化內容
        result = []
        result.append("📊 Telegram 公開資訊觀測站 - 完整格式化報告")
        result.append("=" * 60)
        result.append(f"📅 生成時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        result.append(f"📈 總計公告數：{len(rows)}")
        result.append(f"🔥 轉換公司債相關：{len(convertible_bond_announcements)} 則")
        result.append(f"📋 其他公告：{len(other_announcements)} 則")
        # 若無轉換公司債，於統計後直接標示
        if not convertible_bond_announcements:
            result.append("(無轉換公司債消息)")
        result.append("")
        
        # 1. 轉換公司債相關公告（優先顯示）
        if convertible_bond_announcements:
            result.append("🔥 轉換公司債相關公告（優先顯示）")
            result.append("=" * 60)
            result.append("")
            
            for i, row in enumerate(convertible_bond_announcements, 1):
                result.append(f"【轉換公司債 - 第 {i} 則】")
                result.append(self.format_single_announcement(row, i))
        
        # 2. 其他公告按分類顯示
        if other_announcements:
            result.append("📋 其他公告")
            result.append("=" * 60)
            result.append("")
            
            # 按分類分組
            categorized = {}
            for row in other_announcements:
                category = self.categorize_announcement(row.get('text', ''))
                if category not in categorized:
                    categorized[category] = []
                categorized[category].append(row)
            
            # 按分類輸出
            category_order = ['澄清媒體報導', '財務資訊', '人事異動', '注意交易', '重大訊息', '其他']
            category_emoji = {
                '澄清媒體報導': '📢',
                '財務資訊': '💰',
                '人事異動': '👥',
                '注意交易': '⚠️',
                '重大訊息': '📋',
                '其他': '📄'
            }
            
            for category in category_order:
                if category in categorized and categorized[category]:
                    announcements = categorized[category]
                    result.append(f"{category_emoji[category]} {category} ({len(announcements)} 則)")
                    result.append("-" * 50)
                    
                    for i, row in enumerate(announcements, 1):
                        result.append(f"【{category} - 第 {i} 則】")
                        result.append(self.format_single_announcement(row, i))
                    
                    result.append("")
        
        return '\n'.join(result)
    
    def generate_convertible_bond_summary(self, rows: List[Dict]) -> str:
        """生成轉換公司債摘要報告"""
        convertible_bond_announcements = []
        for row in rows:
            if self.is_convertible_bond_related(row.get('text', '')):
                convertible_bond_announcements.append(row)
        
        if not convertible_bond_announcements:
            return "(無轉換公司債消息)"
        
        result = []
        result.append("🔥 轉換公司債相關公告摘要")
        result.append("=" * 50)
        result.append(f"📅 日期：{datetime.now().strftime('%Y-%m-%d')}")
        result.append(f"📈 轉換公司債公告數：{len(convertible_bond_announcements)}")
        result.append("")
        
        # 按公司分組
        companies = {}
        for row in convertible_bond_announcements:
            company_info = self.extract_company_info(row.get('text', ''))
            company_code = company_info.get('code', '未知')
            if company_code not in companies:
                companies[company_code] = []
            companies[company_code].append(row)
        
        result.append("🏢 涉及公司：")
        for company_code in sorted(companies.keys()):
            company_name = self.extract_company_info(companies[company_code][0].get('text', '')).get('name', '未知公司')
            result.append(f"  • {company_code} {company_name} ({len(companies[company_code])} 則)")
        
        result.append("")
        result.append("📋 詳細公告：")
        for i, row in enumerate(convertible_bond_announcements, 1):
            company_info = self.extract_company_info(row.get('text', ''))
            result.append(f"{i}. {company_info.get('code', '未知')} {company_info.get('name', '未知公司')}")
            result.append(f"   發佈時間：{company_info.get('publish_time', '未知')}")
            result.append(f"   發言人：{company_info.get('speaker', '未知')}")
            result.append("")
        
        return '\n'.join(result)

def main():
    """主程式"""
    formatter = CompleteFormatter()
    
    # 讀取今日的 Telegram 資料
    today = datetime.now().strftime('%Y%m%d')
    csv_path = f'outputs/daily/telegram_messages_{today}.csv'
    
    if not os.path.exists(csv_path):
        safe_print(f"❌ 找不到檔案：{csv_path}")
        safe_print("請先執行 telegram_api_exporter.py 抓取資料")
        return
    
    # 讀取 CSV 資料
    rows = []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    
    safe_print(f"✅ 讀取到 {len(rows)} 則公告")
    
    # 檢查轉換公司債相關公告
    convertible_bond_count = sum(1 for row in rows if formatter.is_convertible_bond_related(row.get('text', '')))
    safe_print(f"🔥 發現 {convertible_bond_count} 則轉換公司債相關公告")
    
    # 生成格式化報告
    output_dir = 'outputs/daily'
    os.makedirs(output_dir, exist_ok=True)
    
    # 1. 完整格式化報告（轉換公司債優先，含公司情況研判）
    formatted_report = formatter.format_with_convertible_bond_priority(rows)
    formatted_path = os.path.join(output_dir, f'complete_report_{today}.txt')
    with open(formatted_path, 'w', encoding='utf-8') as f:
        f.write(formatted_report)
    safe_print(f"✅ 完整格式化報告：{formatted_path}")
    
    # 2. 轉換公司債摘要報告（無論有無皆輸出）
    cb_summary = formatter.generate_convertible_bond_summary(rows)
    cb_summary_path = os.path.join(output_dir, f'convertible_bond_summary_{today}.txt')
    with open(cb_summary_path, 'w', encoding='utf-8') as f:
        f.write(cb_summary)
    safe_print(f"🔥 轉換公司債摘要：{cb_summary_path}")

    # 3. 公司研判彙總（按公司彙整所有研判標籤）
    insights_by_company: Dict[str, Dict[str, List[str]]] = {}
    for row in rows:
        text = row.get('text', '')
        company = formatter.extract_company_info(text)
        code = company.get('code') or '未知'
        name = company.get('name') or '未知公司'
        key = f"{code} {name}"
        analysis = formatter.analyze_company_situation(text)
        if not any(analysis.values()):
            continue
        if key not in insights_by_company:
            insights_by_company[key] = {'Positive': [], 'Negative': [], 'Neutral': []}
        for bucket in ['Positive', 'Negative', 'Neutral']:
            for label in analysis[bucket]:
                if label not in insights_by_company[key][bucket]:
                    insights_by_company[key][bucket].append(label)

    insights_path = os.path.join(output_dir, f'company_insights_{today}.txt')
    with open(insights_path, 'w', encoding='utf-8') as f:
        f.write("🏢 公司情況研判彙總\n")
        f.write("=" * 50 + "\n")
        f.write(f"📅 日期：{datetime.now().strftime('%Y-%m-%d')}\n\n")
        if not insights_by_company:
            f.write("(今日無可研判之公司情況)\n")
        else:
            for company_key in sorted(insights_by_company.keys()):
                buckets = insights_by_company[company_key]
                f.write(f"{company_key}\n")
                if buckets['Positive']:
                    f.write(f"  ✅ 正向：{'; '.join(buckets['Positive'])}\n")
                if buckets['Negative']:
                    f.write(f"  ❗ 負向：{'; '.join(buckets['Negative'])}\n")
                if buckets['Neutral']:
                    f.write(f"  ℹ 中性：{'; '.join(buckets['Neutral'])}\n")
                f.write("-" * 40 + "\n")
    safe_print(f"🧠 公司研判彙總：{insights_path}")
    
    safe_print("\n🎉 完整格式化完成！")
    safe_print("📁 生成檔案：")
    safe_print(f"  • 完整報告：{formatted_path}")
    safe_print(f"  • 轉換公司債摘要：{cb_summary_path}")
    safe_print(f"  • 公司研判彙總：{insights_path}")

if __name__ == '__main__':
    main()

