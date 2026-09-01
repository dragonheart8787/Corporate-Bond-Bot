#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram CSV / 報告管線共用的台北日期解析與篩選。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from zoneinfo import ZoneInfo

TW = ZoneInfo("Asia/Taipei")


def today_yyyymmdd() -> str:
    return datetime.now(TW).strftime("%Y%m%d")


def today_yyyy_mm_dd() -> str:
    return datetime.now(TW).strftime("%Y-%m-%d")


def yyyymmdd_from_date_cell(value: str) -> Optional[str]:
    """與 telegram_api_exporter 的 date 欄一致：YYYY-MM-DD HH:MM:SS。"""
    if not value:
        return None
    v = value.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    m2 = re.match(r"^(\d{8})", v.replace("/", "-"))
    return m2.group(1) if m2 else None


def yyyymmdd_from_csv_filename(path: str) -> Optional[str]:
    m = re.search(r"telegram_messages_(\d{8})\.csv$", path.replace("\\", "/"))
    return m.group(1) if m else None


def is_row_today(row: Dict[str, Any], today: Optional[str] = None) -> bool:
    today = today or today_yyyymmdd()
    d = yyyymmdd_from_date_cell(str(row.get("date", "") or ""))
    return d is not None and d == today


def filter_rows_today(rows: List[Dict[str, Any]], today: Optional[str] = None) -> List[Dict[str, Any]]:
    today = today or today_yyyymmdd()
    return [r for r in rows if is_row_today(r, today)]


def filter_rows_within_days(rows: List[Dict[str, Any]], days: int, today: Optional[str] = None) -> List[Dict[str, Any]]:
    """僅保留台北日曆最近 N 日（含今日）；days=1 等同僅今日。"""
    today = today or today_yyyymmdd()
    if days < 1:
        days = 1
    cutoff = (datetime.now(TW) - timedelta(days=days - 1)).strftime("%Y%m%d")
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = yyyymmdd_from_date_cell(str(r.get("date", "") or ""))
        if d and d >= cutoff:
            out.append(r)
    return out


# ─────────────────────────────────────────
# 公告欄位抽取（quick_format 與 complete_formatter 共用）
#
# 注意：舊版寫成 [^發佈]+ / [^說明]+ / [^發言人職稱]+，那是「否定字元集」
# （不是某個單字），只要公司名或職稱含到其中任一個字就會被截斷或整條失效，
# 例如「#1234 #佈佳公司」會被判成「未知公司」。改用欄位標籤 lookahead。
# ─────────────────────────────────────────

# 實際訊息格式：#8936 #國統 發佈時間：20260826 15:50:53 <標題> #可轉債
# 公司名是一個 hashtag token，本身不含空白與 #
COMPANY_RE = re.compile(r"#(\d+)\s*#([^\s#]+)")
SPEAKER_RE = re.compile(r"發言人：\s*(.+?)(?=\s*發言人職稱：|\s*說明：|\s*$)")
SPEAKER_TITLE_RE = re.compile(r"發言人職稱：\s*(.+?)(?=\s*說明：|\s*$)")
PUBLISH_TIME_RE = re.compile(r"發佈時間：(\d{8}\s+\d{2}:\d{2}:\d{2})")


def clean_company_name(raw: str) -> str:
    """
    公司名與後續欄位若沒有空白分隔時的防呆（如「國統發佈時間：20260826…」）。

    必須連欄位冒號一起比對，否則像「說明科技」「發言人科技」這種公司名
    會被切成空字串。
    """
    name = (raw or "").strip()
    for label in ("發佈時間", "發言人職稱", "發言人", "說明"):
        for colon in ("：", ":"):
            name = name.split(label + colon)[0]
    return name.strip()


def dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in rows:
        key = (r.get("date", ""), r.get("text", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out
