#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
唯讀探測：金管會證期局「申報案件辦理情形彙總表」（頁面 + 115／114 年度 xlsx）。只用標準函式庫。

只回答結構性問題，供決定留存怎麼寫：
  頁面：HTTP 狀態、大小、表格／列數、含「年度」的列標籤、目標列是否唯一、xlsx 連結（檔名與主機，不跟隨）。
  xlsx：HTTP 狀態、大小、工作表名稱與數量、表頭列、列數、各日期欄的格式樣態、
        案件類別與結案類型（以及公司型態、案件性質）的值清單與筆數、各欄超連結數與目標主機。
絕不輸出公司名稱、證券代號、承銷商、金額等內容；日期欄只輸出「樣態」與最早／最晚日期。

請求規則：每個 URL 只 GET 一次（頁面 1 次、115 年 xlsx 1 次、114 年 xlsx 1 次）；請求之間至少 6 秒；
不跟隨轉址；不重試；只允許 https 與 sfb/fsc 主機；xlsx 連結必須從頁面解析（不硬編）；不寫檔（執行摘要除外）。
失敗一律 ::error:: 並以非 0 結束。

環境變數：PROBE_FSC_PAGE_URL（覆寫頁面網址，仍須通過白名單）、PROBE_DRY_RUN=1、PROBE_REPO、
          PROBE_MODE（xlsx＝上述原行為，預設；formats＝見下）。

formats 模式（115 年列已沒有 .xlsx 時用）：
  1. 列出 115／114 兩列的「全部」連結（副檔名、解碼後檔名、主機、連結文字）。
  2. 115 列的每個 .ods／.xls 連結各 GET 一次（上限 4 個）：HTTP 狀態、大小、前 8 bytes（hex）、Content-Type、
     以魔術位元組判斷實際格式（OLE2／zip／HTML／其他），副檔名與實際格式不符會明講。
  3. zip 結構的 ODS：只用標準函式庫讀 content.xml（沿用 DTD／zip 炸彈防護），輸出工作表、表頭、18 欄是否相符、
     資料列數、各日期欄的儲存格型別與樣態、金額與發行價格的型別、收文日期最早／最晚、結尾列數。
  4. OLE2（BIFF .xls）：只判斷格式與大小，不解析。
  5. 與 data/fsc/parsed/115.json（唯讀）比對：鍵 = 證券代號＋案件類別＋收文日期＋金額；只輸出筆數與欄名、型別／樣態差異。
  6. 新鮮度：ODS 最大收文日期、頁面更新日期、檔名日期，並明確回答 ODS 有沒有跟上。
  不輸出公司名稱、承銷商、金額內容。
結束碼：0 全部成功；1 至少一項失敗；2 設定不合法。
"""

from __future__ import annotations

import collections
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import fsc_sources as fs
import probe_sources as ps

LABELS = ("115年度申報案件", "114年度申報案件")
CATEGORY_COLUMNS = ("案件類別", "結案類型", "公司型態", "案件性質")
MAX_DISTINCT_LISTED = 40
MAX_LABELS_SHOWN = 12
MAX_DOWNLOADS = 4
OLD_PARSED = Path(__file__).resolve().parent.parent / "data" / "fsc" / "parsed" / "115.json"
STABLE_COLUMNS = ("證券代號", "公司型態", "公司名稱", "承銷商", "案件類別", "金額", "幣別", "發行價格", "收文日期", "案件性質")
ANY_ACCEPT = "application/vnd.oasis.opendocument.spreadsheet, application/vnd.ms-excel, application/octet-stream;q=0.9, */*;q=0.5"
OLE2_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
XLSX_ACCEPT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/octet-stream;q=0.9, */*;q=0.5"


def log(msg: str) -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────
# 樣態分類（只回傳結構，不回傳內容）
# ─────────────────────────────────────────
def shape_of(value: str) -> str:
    """把值轉成結構簽名：數字→9、英文→a、中日韓字→C，連續同類壓成 9{7}；其他字元照原樣（換行顯示為 ⏎）。"""
    out: List[str] = []
    prev, n = "", 0

    def flush() -> None:
        if prev:
            out.append(f"{prev}{{{n}}}" if n > 1 else prev)

    for ch in unicodedata.normalize("NFKC", value or ""):
        if ch.isdigit():
            cls = "9"
        elif ch.isascii() and ch.isalpha():
            cls = "a"
        elif "㐀" <= ch <= "鿿":
            cls = "C"
        else:
            cls = "⏎" if ch == "\n" else ch
        if cls == prev and cls in ("9", "a", "C"):
            n += 1
        else:
            flush()
            prev, n = cls, 1
    flush()
    return "".join(out)


def roc7_to_iso(token: str) -> Optional[str]:
    if not re.fullmatch(r"\d{7}", token):
        return None
    try:
        return date(int(token[:3]) + 1911, int(token[3:5]), int(token[5:])).isoformat()
    except ValueError:
        return None


def classify_date_cell(cell: fs.XCell) -> Tuple[str, List[str]]:
    """回傳 (樣態標籤, 解析出的 ISO 日期清單)。標籤含儲存格型別（str 字串／num 數字）。"""
    raw = unicodedata.normalize("NFKC", cell.value or "").strip()
    k = {"str": "字串", "num": "數字"}.get(cell.kind, cell.kind)
    if not raw:
        return "空值", []
    if re.fullmatch(r"\d{7}", raw):
        iso = roc7_to_iso(raw)
        return (f"7碼[{k}]" if iso else f"7碼但日期不存在[{k}]"), ([iso] if iso else [])
    parts = [p for p in re.split(r"[,，、;；\s]+", raw) if p]
    if len(parts) >= 2 and all(re.fullmatch(r"\d{7}", p) for p in parts):
        isos = [roc7_to_iso(p) for p in parts]
        good = [i for i in isos if i]
        tag = f"逗號多值{len(parts)}個[{k}]" if all(isos) else f"逗號多值{len(parts)}個含不存在日期[{k}]"
        return tag, good
    return f"其他:{shape_of(raw)}[{k}]", []


def link_host(link: Optional[str], kind: str) -> str:
    if not link:
        return ""
    if kind == "internal":
        return "（內部位置）"
    return urllib.parse.urlparse(link).hostname or "（無主機）"


# ─────────────────────────────────────────
# xlsx 分析
# ─────────────────────────────────────────
def analyze_sheet(sheet: fs.XSheet) -> List[str]:
    out: List[str] = []
    h = fs.find_header(sheet)
    nonempty_by_row = [sum(1 for c in r if c.kind != "empty") for r in sheet.rows[:8]]
    out.append(f"  工作表「{sheet.name}」：總列數 {len(sheet.rows)}（含表頭之前的列）；前 8 列的非空儲存格數 {nonempty_by_row}")
    if h is None:
        out.append("  ⚠ 前 15 列找不到含「證券代號」的表頭列")
        return out
    header = [fs.norm_header(c.value) for c in sheet.rows[h]]
    while header and header[-1] == "":
        header.pop()
    out.append(f"  表頭在第 {h + 1} 列，欄數 {len(header)}")
    out.append("  表頭：" + " | ".join(header))
    if header == fs.EXPECTED_COLUMNS:
        out.append("  表頭比對：與預期的 18 欄完全相同（名稱與順序）")
    else:
        missing = [c for c in fs.EXPECTED_COLUMNS if c not in header]
        extra = [c for c in header if c not in fs.EXPECTED_COLUMNS]
        out.append(f"  ⚠ 表頭比對：與預期的 18 欄不同；缺少 {missing}；多出 {extra}；"
                   f"{'順序不同' if not missing and not extra else ''}")
    idx = {name: i for i, name in enumerate(header)}
    data = [r for r in sheet.rows[h + 1:] if any(c.kind != "empty" for c in r)]
    code_i = idx.get("證券代號")
    blank_code = sum(1 for r in data if code_i is None or code_i >= len(r) or not fs.norm_label(r[code_i].value))
    out.append(f"  資料列數（表頭之後、非全空）：{len(data)}；證券代號為空的列：{blank_code}")

    def col(r: List[fs.XCell], name: str) -> fs.XCell:
        i = idx.get(name)
        return r[i] if i is not None and i < len(r) else fs.XCell()

    for name in CATEGORY_COLUMNS:
        if name not in idx:
            continue
        cnt = collections.Counter(fs.norm_label(col(r, name).value) or "（空）" for r in data)
        if len(cnt) > MAX_DISTINCT_LISTED:
            out.append(f"  {name}：{len(cnt)} 種不同的值（超過 {MAX_DISTINCT_LISTED} 種，不列出）")
        else:
            out.append(f"  {name}（{len(cnt)} 種）：" + "；".join(f"{v}×{n}" for v, n in cnt.most_common()))
    for name in fs.DATE_COLUMNS:
        if name not in idx:
            out.append(f"  日期欄「{name}」：表頭中不存在")
            continue
        labels: collections.Counter = collections.Counter()
        isos: List[str] = []
        for r in data:
            lab, got = classify_date_cell(col(r, name))
            labels[lab] += 1
            isos.extend(got)
        shown = labels.most_common(MAX_LABELS_SHOWN)
        rest = sum(labels.values()) - sum(n for _, n in shown)
        rng = f"；日期範圍 {min(isos)} ～ {max(isos)}（共 {len(isos)} 個日期）" if isos else "；沒有可解析的日期"
        out.append(f"  日期欄「{name}」：" + "；".join(f"{lab}×{n}" for lab, n in shown)
                   + (f"；其他樣態合計×{rest}" if rest > 0 else "") + rng)
    # 超連結（不跟隨）：每欄的數量與目標主機
    link_lines = []
    for name in header:
        i = idx[name]
        hosts: collections.Counter = collections.Counter()
        for r in data:
            c = r[i] if i < len(r) else fs.XCell()
            if c.link:
                hosts[f"{link_host(c.link, c.link_kind)}（{c.link_kind}）"] += 1
        if hosts:
            link_lines.append(f"{name}：" + "、".join(f"{h}×{n}" for h, n in hosts.most_common(6)))
    out.append("  超連結（欄：目標主機×數量）：" + ("；".join(link_lines) if link_lines else "（資料列中沒有任何超連結）"))
    return out


def analyze_xlsx(data: bytes) -> List[str]:
    sheets = fs.read_xlsx(data)
    out = [f"  工作表數量 {len(sheets)}：" + "、".join(f"「{s.name}」" for s in sheets[:20])]
    for s in sheets[:6]:
        out.extend(analyze_sheet(s))
    if len(sheets) > 6:
        out.append(f"  （另有 {len(sheets) - 6} 個工作表未分析）")
    return out


# ─────────────────────────────────────────
# formats 模式：實際格式判斷、ODS 結構、與舊版比對、新鮮度
# ─────────────────────────────────────────
def sniff(body: bytes) -> Tuple[str, str]:
    """回傳 (類別, 說明)。類別：ole2 | zip | html | xml | other | empty。只看開頭位元組與目錄項名稱，不解析內容。"""
    if not body:
        return "empty", "空內容"
    if body[:8] == OLE2_MAGIC:
        w16 = "Workbook".encode("utf-16-le")
        b16 = "Book".encode("utf-16-le")
        if w16 in body:
            return "ole2", "OLE2 複合檔，含「Workbook」目錄項（BIFF8 .xls）"
        if b16 in body:
            return "ole2", "OLE2 複合檔，含「Book」目錄項（BIFF5 .xls）"
        return "ole2", "OLE2 複合檔（沒找到 Workbook／Book 目錄項，可能不是試算表）"
    if body[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip", "zip 容器"
    head = body[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<script", b"<meta")):
        return "html", "HTML 文件"
    if head.startswith(b"<?xml") or head.startswith(b"<workbook"):
        return "xml", "XML 文件（可能是 SpreadsheetML 2003）"
    return "other", "未知格式"


def html_title(body: bytes) -> str:
    m = re.search(rb"<title[^>]*>(.{0,200}?)</title>", body[:20000], re.I | re.S)
    if not m:
        return ""
    text, _ = decode_html(m.group(1))
    return re.sub(r"\s+", " ", text).strip()[:60]


def zip_kind(body: bytes) -> str:
    """zip 內容是 ods | xlsx | other（只看部件名稱與 mimetype）。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(body))
        names = set(zf.namelist())
        if "mimetype" in names and zf.read("mimetype")[:200].decode("ascii", "replace").strip() == fs.ODS_MIME:
            return "ods"
        if "xl/workbook.xml" in names:
            return "xlsx"
    except (zipfile.BadZipFile, OSError):
        pass
    return "other"


def canon_value(v: str) -> str:
    """穩定欄位比對用的標準化：數字統一成整數字串（1.5E9／1500000000.0 → 1500000000），其餘 NFKC＋去頭尾空白。"""
    t = unicodedata.normalize("NFKC", v or "").strip()
    if re.fullmatch(r"[+-]?\d+(\.\d+)?([eE][+-]?\d+)?", t):
        try:
            d = Decimal(t)
            return str(int(d)) if d == d.to_integral_value() else format(d.normalize(), "f")
        except InvalidOperation:
            pass
    return t


def ods_cell_text(c: fs.OCell) -> str:
    """ODS 儲存格轉成與 xlsx 解析同語意的字串：字串型用顯示文字；數字型用 office:value 原樣。"""
    if c.vtype in ("float", "percentage", "currency") and c.value != "":
        return c.value
    return c.text if c.text != "" else c.value


def ods_date_class(c: fs.OCell) -> Tuple[str, List[str]]:
    kind = {"string": "字串", "float": "數字", "date": "日期型", "": "空"}.get(c.vtype, c.vtype or "空")
    if c.vtype == "date":
        return f"ods日期型[{kind}]", [c.value[:10]] if re.match(r"\d{4}-\d{2}-\d{2}", c.value) else []
    fake = fs.XCell(value=ods_cell_text(c), kind="num" if c.vtype == "float" else ("str" if c.text else "empty"))
    tag, isos = classify_date_cell(fake)
    return tag.replace("[字串]", f"[{kind}]").replace("[數字]", f"[{kind}]"), isos


class OdsResult:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.rows: List[List[str]] = []          # 18 欄文字（資料列）
        self.max_receipt: Optional[str] = None
        self.min_receipt: Optional[str] = None
        self.header_ok = False
        self.sheet_count = 0


def analyze_ods(data: bytes) -> OdsResult:
    res = OdsResult()
    out = res.lines
    sheets = fs.read_ods(data)
    res.sheet_count = len(sheets)
    out.append(f"  工作表數量 {len(sheets)}：" + "、".join(f"「{s.name}」" for s in sheets[:20]))
    target = None
    for sh in sheets[:6]:
        nonempty_by_row = [sum(1 for c in r if c.text or c.vtype) for r in sh.rows[:8]]
        out.append(f"  工作表「{sh.name}」：總列數 {len(sh.rows)}；前 8 列的非空儲存格數 {nonempty_by_row}")
        h = next((i for i, r in enumerate(sh.rows[:15]) if any(fs.norm_header(c.text) == "證券代號" for c in r)), None)
        if h is None:
            out.append("  ⚠ 前 15 列找不到含「證券代號」的表頭列")
            continue
        header = [fs.norm_header(c.text) for c in sh.rows[h]]
        while header and header[-1] == "":
            header.pop()
        out.append(f"  表頭在第 {h + 1} 列，欄數 {len(header)}")
        out.append("  表頭：" + " | ".join(header))
        ok = header == fs.EXPECTED_COLUMNS
        if ok:
            out.append("  表頭比對：與預期的 18 欄完全相同（名稱與順序）")
        else:
            missing = [c for c in fs.EXPECTED_COLUMNS if c not in header]
            extra = [c for c in header if c not in fs.EXPECTED_COLUMNS]
            out.append(f"  ⚠ 表頭比對：與預期的 18 欄不同；缺少 {missing}；多出 {extra}"
                       + ("；名稱相同但順序不同" if not missing and not extra else ""))
        if target is None:
            target = (sh, h, header, ok)
    if target is None:
        raise fs.FscError("所有工作表都找不到含「證券代號」的表頭列")
    sh, h, header, ok = target
    res.header_ok = ok
    idx = {n: i for i, n in enumerate(header)}
    code_i = idx.get("證券代號", 0)

    def cell(r: List[fs.OCell], name: str) -> fs.OCell:
        i = idx.get(name)
        return r[i] if i is not None and i < len(r) else fs.OCell()

    body = list(sh.rows[h + 1:])

    def is_trailer_like(r: List[fs.OCell]) -> bool:
        # 與 archive_fsc 的「結尾說明列」判定相同：非空儲存格 ≤ 2 且證券代號不像代號（3～8 個英數字）
        if not any(c.text or c.vtype for c in r):
            return True
        nonempty = sum(1 for c in r if fs.norm_label(c.text))
        code = fs.norm_label(r[code_i].text) if code_i < len(r) else ""
        return nonempty <= 2 and not re.fullmatch(r"[0-9A-Za-z]{3,8}", code)

    cut = len(body)
    while cut > 0 and is_trailer_like(body[cut - 1]):
        cut -= 1
    trailer = sum(1 for r in body[cut:] if any(c.text or c.vtype for c in r))
    data_rows = [r for r in body[:cut] if any(c.text or c.vtype for c in r)]
    blank_code = sum(1 for r in data_rows if not (code_i < len(r) and fs.norm_label(r[code_i].text)))
    out.append(f"  資料列數（表頭之後、結尾說明列之前、非全空）：{len(data_rows)}；其中證券代號為空 {blank_code}；結尾／說明列數：{trailer}")
    for name in fs.DATE_COLUMNS:
        if name not in idx:
            out.append(f"  日期欄「{name}」：表頭中不存在")
            continue
        labels: collections.Counter = collections.Counter()
        isos: List[str] = []
        for r in data_rows:
            lab, got = ods_date_class(cell(r, name))
            labels[lab] += 1
            isos.extend(got)
        shown = labels.most_common(MAX_LABELS_SHOWN)
        rest = sum(labels.values()) - sum(n for _, n in shown)
        rng = f"；日期範圍 {min(isos)} ～ {max(isos)}（共 {len(isos)} 個日期）" if isos else "；沒有可解析的日期"
        out.append(f"  日期欄「{name}」：" + "；".join(f"{l}×{n}" for l, n in shown)
                   + (f"；其他樣態合計×{rest}" if rest > 0 else "") + rng)
        if name == "收文日期" and isos:
            res.min_receipt, res.max_receipt = min(isos), max(isos)
    for name in ("金額", "發行價格"):
        if name not in idx:
            out.append(f"  「{name}」：表頭中不存在")
            continue
        types: collections.Counter = collections.Counter()
        shapes: collections.Counter = collections.Counter()
        for r in data_rows:
            c = cell(r, name)
            types[c.vtype or "空"] += 1
            t = ods_cell_text(c)
            shapes[shape_of(t) if t else "空"] += 1
        out.append(f"  「{name}」儲存格型別：" + "；".join(f"{k}×{n}" for k, n in types.most_common())
                   + f"｜文字樣態（前 {MAX_LABELS_SHOWN} 種）：" + "；".join(f"{k}×{n}" for k, n in shapes.most_common(MAX_LABELS_SHOWN)))
    for name in CATEGORY_COLUMNS:
        if name not in idx:
            continue
        cnt = collections.Counter(fs.norm_label(cell(r, name).text) or "（空）" for r in data_rows)
        if len(cnt) <= MAX_DISTINCT_LISTED:
            out.append(f"  {name}（{len(cnt)} 種）：" + "；".join(f"{v}×{n}" for v, n in cnt.most_common()))
        else:
            out.append(f"  {name}：{len(cnt)} 種不同的值（不列出）")
    res.rows = [[ods_cell_text(cell(r, n)) for n in fs.EXPECTED_COLUMNS] for r in data_rows]
    return res


def _row_key(vals: Dict[str, str]) -> Tuple[str, str, str, str]:
    receipt = canon_value(vals.get("收文日期", ""))
    return (canon_value(vals.get("證券代號", "")), canon_value(vals.get("案件類別", "")), receipt, canon_value(vals.get("金額", "")))


def keyed(rows: List[Dict[str, str]]) -> Dict[Tuple[Tuple[str, str, str, str], int], Dict[str, str]]:
    seen: collections.Counter = collections.Counter()
    out = {}
    for r in rows:
        k = _row_key(r)
        seen[k] += 1
        out[(k, seen[k])] = r
    return out


def consistency(ods_rows: List[List[str]], old_path: Path) -> List[str]:
    """與舊版 parsed JSON（唯讀）比對。只輸出筆數、欄名、型別／樣態差異，不輸出內容。"""
    out: List[str] = []
    try:
        old = json.loads(old_path.read_text(encoding="utf-8"))
        old_rows = [r["fields"] for r in old["rows"]]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise fs.FscError(f"無法讀取舊版比對資料 {old_path.name}：{type(e).__name__}") from None
    out.append(f"  舊版（data/fsc/parsed/115.json，來源 sha {str(old.get('sha256', ''))[:8]}、基準日 {old.get('reference_date')}）：{len(old_rows)} 列")
    new = [dict(zip(fs.EXPECTED_COLUMNS, r)) for r in ods_rows]
    nk, ok_ = keyed(new), keyed(old_rows)
    both = [k for k in nk if k in ok_]
    only_new = [k for k in nk if k not in ok_]
    only_old = [k for k in ok_ if k not in nk]
    out.append(f"  比對鍵 = 證券代號＋案件類別＋收文日期＋金額（同鍵依出現順序配對）：ODS {len(new)} 列、舊版 {len(old_rows)} 列")
    out.append(f"  兩邊都有 {len(both)}｜只在 ODS {len(only_new)}｜只在舊版 {len(only_old)}")
    if not both:
        out.append("  ⚠ 沒有任何鍵相同，無法比對各欄")
        return out
    diff_cols: collections.Counter = collections.Counter()
    patterns: Dict[str, collections.Counter] = {c: collections.Counter() for c in STABLE_COLUMNS}
    for k in both:
        a, b = nk[k], ok_[k]
        for c in STABLE_COLUMNS:
            va, vb = a.get(c, ""), b.get(c, "")
            if canon_value(va) != canon_value(vb):
                diff_cols[c] += 1
                patterns[c][f"ODS:{shape_of(va) or '空'} ≠ 舊:{shape_of(vb) or '空'}"] += 1
            elif va != vb:
                patterns[c][f"僅表示法不同（標準化後相同）ODS:{shape_of(va) or '空'} / 舊:{shape_of(vb) or '空'}"] += 1
    out.append(f"  穩定欄位在 {len(both)} 組相同鍵的列上的差異（標準化後；數字型 1.5E9 與 1500000000 視為相同）：")
    for c in STABLE_COLUMNS:
        n = diff_cols[c]
        out.append(f"    {c}：不一致 {n}" + (f"（{ '；'.join(f'{p}×{m}' for p, m in patterns[c].most_common(4)) }）" if patterns[c] else ""))
    return out


def freshness(ods_max: Optional[str], ods_n: int, filenames: Dict[str, str], page_update: str, old_path: Path) -> List[str]:
    """回答：ODS 有沒有跟上最新？只用日期與筆數比較。"""
    out: List[str] = []
    try:
        old = json.loads(old_path.read_text(encoding="utf-8"))
        old_dates = [d for r in old["rows"] for d in (r.get("dates_iso", {}).get("收文日期") or []) if d]
        old_max, old_n, old_ref = max(old_dates) if old_dates else None, len(old["rows"]), old.get("reference_date")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        old_max, old_n, old_ref = None, None, None
    out.append(f"  頁面更新日期文字：{page_update or '（找不到）'}")
    out.append("  檔名（解碼後）：" + (" ｜ ".join(f"{e} → {n}" for e, n in filenames.items()) or "（無）"))
    dnums = {e: re.findall(r"\d{7,8}", n) for e, n in filenames.items()}
    out.append("  檔名中的日期／版本數字：" + (" ｜ ".join(f"{e}: {v or '（無）'}" for e, v in dnums.items()) or "（無）"))
    out.append(f"  ODS 最大收文日期：{ods_max or '（無）'}｜舊版（{old_ref}）最大收文日期：{old_max or '（無）'}｜ODS 列數 {ods_n}｜舊版列數 {old_n}")
    m = re.search(r"(\d{2,3})\s*[./年]\s*(\d{1,2})\s*[./月]\s*(\d{1,2})", unicodedata.normalize("NFKC", page_update or ""))
    page_iso = None
    if m:
        try:
            page_iso = date(int(m[1]) + 1911, int(m[2]), int(m[3])).isoformat()
        except ValueError:
            page_iso = None
    out.append(f"  頁面更新日（ISO）：{page_iso or '（無法解析）'}")
    if ods_max is None:
        out.append("  結論：無法判斷（ODS 沒有可解析的收文日期）")
    else:
        if page_iso:
            gap = (date.fromisoformat(page_iso) - date.fromisoformat(ods_max)).days
            # 收文日期通常早於頁面更新日幾天到兩週；差太多才懷疑落後
            out.append(f"  ODS 最大收文日期距頁面更新日 {gap} 天")
        grew = (old_max is not None and ods_max > old_max) or (old_n is not None and ods_n > old_n)
        if page_iso and (date.fromisoformat(page_iso) - date.fromisoformat(ods_max)).days <= 14 and grew:
            out.append(f"  結論：ODS 看起來有跟上（最大收文日期在頁面更新日 14 天內，且比 {old_ref} 的舊版更新）。")
        elif grew:
            out.append(f"  結論：ODS 比 {old_ref} 的舊版新，但最大收文日期離頁面更新日超過 14 天或頁面日期無法解析，無法確認已跟上最新，請人工確認。")
        else:
            out.append(f"  結論：ODS 沒有比 {old_ref} 的舊版新（最大收文日期與列數都沒增加）→ 判定落後，不可當最新資料來源。")
    out.append("  註：10-08 曾出現 xlsx 為 v3、ods 檔名仍為 v1（ODS 落後）；本次以檔名版本號與資料日期交叉看，不以單一訊號下結論。")
    return out


def run_formats(env: Dict[str, str], page_url: str, ua: str, opener: Optional[Any], sleep: Callable[[float], None]) -> int:
    report: List[str] = []
    failed = 0

    def emit(line: str) -> None:
        report.append(line)
        log(line)

    def err(title: str, msg: str) -> None:
        nonlocal failed
        failed += 1
        log(f"::error title=探測失敗 {title}::{msg}")

    fr = ps.fetch_once(page_url, ua, opener, accept="text/html, */*;q=0.5")
    emit(f"== [頁面] {page_url}")
    emit(f"HTTP 狀態：{fr.status if fr.status is not None else '無回應'}｜回應大小：{fr.size} bytes｜content-type：{fr.content_type or '（未提供）'}")
    if fr.error or fr.truncated:
        reason = fr.error or "回應超過大小上限"
        emit(f"結果：失敗 — {reason}" + (f"（轉址目的主機：{fr.location_host}）" if fr.location_host else ""))
        log(f"::error title=探測失敗 頁面::{reason}")
        _write_summary(env, report)
        return 1
    html, enc = decode_html(fr.body)
    parsed = fs.parse_page(html)
    page_update = fs.page_update_text(html)
    emit(f"編碼：{enc}｜表格 {parsed.tables} 個、列 {len(parsed.rows)} 列｜頁面更新日期文字：{page_update or '（找不到）'}")

    # (a) 兩列的全部連結
    row_map: Dict[str, List[fs.RowLink]] = {}
    for lab in LABELS:
        emit("")
        try:
            links = fs.row_links(html, page_url, lab)
        except fs.FscError as e:
            emit(f"== 「{lab}」那一列：失敗 — {e}")
            for d in e.diagnostics:
                emit(f"  診斷：{d}")
            err(lab, str(e))
            continue
        row_map[lab] = links
        emit(f"== 「{lab}」那一列的全部連結（共 {len(links)} 個）")
        for i, l in enumerate(links, 1):
            emit(f"  {i}. 副檔名 {l.ext}｜檔名 {l.filename}｜主機 {l.host or '（無）'}｜{'https' if l.https else '非 https'}"
                 f"｜{'可下載' if l.allowed else '不可下載（非 https 或主機不在白名單）'}｜連結文字 {l.anchor_text!r}")
        exts = collections.Counter(l.ext for l in links)
        emit("  副檔名統計：" + ("、".join(f"{e}×{n}" for e, n in exts.most_common()) or "（沒有任何連結）"))

    # (b)-(d) 115 列的 .ods / .xls
    lab115 = LABELS[0]
    targets = [l for l in row_map.get(lab115, []) if l.ext in (".ods", ".xls")]
    if lab115 in row_map:
        if any(l.ext == ".xlsx" for l in row_map[lab115]):
            emit("")
            emit(f"註：「{lab115}」那一列現在有 .xlsx 連結，可改回 xlsx 路徑（本模式只下載 .ods／.xls）。")
        if not targets:
            err(lab115, f"「{lab115}」那一列沒有任何 .ods 或 .xls 連結，沒有東西可探測")
        if len(targets) > MAX_DOWNLOADS:
            emit(f"註：.ods／.xls 連結共 {len(targets)} 個，只取前 {MAX_DOWNLOADS} 個。")
            targets = targets[:MAX_DOWNLOADS]
    ods_result: Optional[OdsResult] = None
    ods_name = ""
    fnames: Dict[str, str] = {}
    for t in targets:
        fnames[t.ext] = t.filename
    for t in targets:
        sleep(max(ps.INTERVAL_SEC, ps.MIN_INTERVAL_SEC))
        emit("")
        emit(f"== [{lab115} {t.ext}] {t.filename}（主機 {t.host}）")
        if not t.allowed:
            emit("結果：略過 — 非 https 或主機不在白名單，不下載")
            err(f"{lab115} {t.ext}", "連結不可下載（非 https 或主機不在白名單）")
            continue
        xr = ps.fetch_once(t.url, ua, opener, accept=ANY_ACCEPT)
        emit(f"HTTP 狀態：{xr.status if xr.status is not None else '無回應'}｜回應大小：{xr.size} bytes｜content-type：{xr.content_type or '（未提供）'}"
             f"｜前 8 bytes：{xr.body[:8].hex().upper() or '（無）'}")
        if xr.error or xr.truncated:
            reason = xr.error or "回應超過大小上限，被截斷"
            emit(f"結果：失敗 — {reason}" + (f"（轉址目的主機：{xr.location_host}）" if xr.location_host else ""))
            err(f"{lab115} {t.ext}", reason)
            continue
        cat, desc = sniff(xr.body)
        emit(f"實際格式：{desc}（類別 {cat}）")
        if cat == "html":
            title = html_title(xr.body)
            emit(f"結果：失敗 — 回應是 HTML 而不是試算表（可能是阻擋頁／錯誤頁／導覽頁）；<title>：{title or '（無）'}")
            err(f"{lab115} {t.ext}", f"副檔名 {t.ext} 但回應是 HTML 文件，不是試算表檔")
            continue
        if cat in ("empty", "other"):
            emit(f"結果：失敗 — 無法辨識為試算表（{desc}）")
            err(f"{lab115} {t.ext}", f"副檔名 {t.ext} 但內容無法辨識為試算表（{desc}）")
            continue
        if cat == "ole2":
            if t.ext != ".xls":
                emit(f"⚠ 副檔名與實際格式不符：副檔名 {t.ext}，實際是 OLE2（BIFF .xls 系）")
                log(f"::warning title=副檔名與格式不符 {t.ext}::副檔名 {t.ext} 但內容是 OLE2 複合檔")
            emit(f"結果：已判斷格式（BIFF .xls 系；本階段不解析）｜大小 {xr.size} bytes")
            continue
        if cat == "xml":
            emit(f"結果：已判斷格式（{desc}）；本階段不解析")
            if t.ext != ".xls":
                log(f"::warning title=副檔名與格式不符 {t.ext}::副檔名 {t.ext} 但內容是 XML 文件")
            continue
        # zip
        zk = zip_kind(xr.body)
        emit(f"zip 內容種類：{zk}")
        if (t.ext == ".ods") != (zk == "ods"):
            emit(f"⚠ 副檔名與實際格式不符：副檔名 {t.ext}，zip 內容種類 {zk}")
            log(f"::warning title=副檔名與格式不符 {t.ext}::副檔名 {t.ext} 但 zip 內容種類是 {zk}")
        try:
            if zk == "ods":
                r = analyze_ods(xr.body)
                for line in r.lines:
                    emit(line)
                if ods_result is None:
                    ods_result, ods_name = r, t.filename
            elif zk == "xlsx":
                for line in analyze_xlsx(xr.body):
                    emit(line)
            else:
                emit("結果：失敗 — zip 內容既不是 ODS 也不是 xlsx")
                err(f"{lab115} {t.ext}", "zip 內容既不是 ODS 也不是 xlsx")
        except fs.FscError as e:
            emit(f"結果：失敗 — {e}")
            err(f"{lab115} {t.ext}", str(e))

    # (e) 一致性 與 (f) 新鮮度
    emit("")
    emit("== 與舊版比對（唯讀 data/fsc/parsed/115.json）")
    if ods_result is None:
        emit("  略過：沒有成功讀取的 ODS")
    else:
        try:
            for line in consistency(ods_result.rows, OLD_PARSED):
                emit(line)
        except fs.FscError as e:
            emit(f"  失敗 — {e}")
            err("一致性檢查", str(e))
    emit("")
    emit("== 新鮮度（ODS 有沒有跟上最新？）")
    if ods_result is None:
        emit("  無法判斷：沒有成功讀取的 ODS" + (f"；.xls 只判斷了格式（檔名 {fnames.get('.xls', '（無）')}）" if '.xls' in fnames else ""))
    else:
        for line in freshness(ods_result.max_receipt, len(ods_result.rows), fnames, page_update, OLD_PARSED):
            emit(line)
    emit("")
    emit(f"探測完成：{'有失敗，請看上方 ::error::' if failed else '全部成功'}")
    _write_summary(env, report)
    return 1 if failed else 0


# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────
def decode_html(body: bytes) -> Tuple[str, str]:
    for enc in ("utf-8", "big5"):
        try:
            return body.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", "replace"), "utf-8(replace)"


def run(
    env: Optional[Dict[str, str]] = None,
    *,
    opener: Optional[Any] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    env = dict(os.environ if env is None else env)
    page_url = (env.get("PROBE_FSC_PAGE_URL") or fs.PAGE_URL).strip()
    ok, why = ps.validate_url(page_url, fs.FSC_HOSTS)
    if not ok:
        log(f"::error title=探測設定不合法::頁面網址：{why}")
        log("未送出任何請求。")
        return 2
    ua = ps.user_agent(env.get("PROBE_REPO", ""), tool="probe-fsc", purpose="read-only structure check of a public spreadsheet")
    mode = (env.get("PROBE_MODE") or "xlsx").strip()
    if mode not in ("xlsx", "formats"):
        log(f"::error title=探測設定不合法::PROBE_MODE 只能是 xlsx 或 formats（收到 {mode[:20]!r}）")
        log("未送出任何請求。")
        return 2
    if mode == "formats":
        if env.get("PROBE_DRY_RUN", "") == "1":
            log("🧪 DRY-RUN（formats）：不連線。最多送 %d 次 GET（間隔 %.0f 秒，不跟隨轉址、不重試）：" % (1 + MAX_DOWNLOADS, ps.INTERVAL_SEC))
            log(f"  1. 頁面：{page_url}")
            log(f"  - 列出「{LABELS[0]}」「{LABELS[1]}」兩列的全部連結（不下載 114 年的任何檔案）")
            log(f"  - 「{LABELS[0]}」那一列的每個 .ods／.xls 連結各 GET 一次（最多 {MAX_DOWNLOADS} 個；連結每次從頁面解析，不硬編）")
            log("  - 唯讀比對 data/fsc/parsed/115.json；不寫入 repo")
            log(f"User-Agent：{ua}")
            return 0
        return run_formats(env, page_url, ua, opener, sleep)
    if env.get("PROBE_DRY_RUN", "") == "1":
        log("🧪 DRY-RUN：不連線。最多送 3 次 GET（間隔 %.0f 秒）：" % ps.INTERVAL_SEC)
        log(f"  1. 頁面：{page_url}")
        for lab in LABELS:
            log(f"  - 從頁面解析「{lab}」那一列的 .xlsx 連結後各 GET 一次（連結每次從頁面解析，不硬編）")
        log(f"User-Agent：{ua}")
        return 0

    report: List[str] = []
    failed = 0

    def emit(line: str) -> None:
        report.append(line)
        log(line)

    fr = ps.fetch_once(page_url, ua, opener, accept="text/html, */*;q=0.5")
    emit(f"== [頁面] {page_url}")
    emit(f"HTTP 狀態：{fr.status if fr.status is not None else '無回應'}｜回應大小：{fr.size} bytes｜content-type：{fr.content_type or '（未提供）'}")
    if fr.error or fr.truncated:
        reason = fr.error or "回應超過大小上限"
        emit(f"結果：失敗 — {reason}" + (f"（轉址目的主機：{fr.location_host}）" if fr.location_host else ""))
        log(f"::error title=探測失敗 頁面::{reason}")
        _write_summary(env, report)
        return 1
    html, enc = decode_html(fr.body)
    parsed = fs.parse_page(html)
    emit(f"編碼：{enc}｜表格 {parsed.tables} 個、列 {len(parsed.rows)} 列｜頁面更新日期文字：{fs.page_update_text(html) or '（找不到）'}")
    labels_seen = sorted({c.text for r in parsed.rows for c in r.cells if "年度" in c.text and len(c.text) <= 60})
    emit("頁面上含「年度」的列標籤：" + (" ｜ ".join(labels_seen[:40]) if labels_seen else "（無）"))
    links: Dict[str, fs.LinkChoice] = {}
    for lab in LABELS:
        try:
            links[lab] = fs.resolve_year_link(html, page_url, lab)
            ch = links[lab]
            emit(f"「{lab}」→ 唯一的 xlsx 連結：主機 {urllib.parse.urlparse(ch.url).hostname}｜檔名 {ch.filename}｜連結文字 {ch.anchor_text!r}")
        except fs.FscError as e:
            failed += 1
            emit(f"「{lab}」解析失敗：{e}")
            for d in e.diagnostics:
                emit(f"  診斷：{d}")
            log(f"::error title=探測失敗 {lab}::{e}")

    for lab, ch in links.items():
        sleep(max(ps.INTERVAL_SEC, ps.MIN_INTERVAL_SEC))
        xr = ps.fetch_once(ch.url, ua, opener, accept=XLSX_ACCEPT)
        emit("")
        emit(f"== [{lab}] {ch.url}")
        emit(f"HTTP 狀態：{xr.status if xr.status is not None else '無回應'}｜回應大小：{xr.size} bytes｜content-type：{xr.content_type or '（未提供）'}")
        if xr.error or xr.truncated:
            reason = xr.error or "回應超過大小上限，被截斷"
            failed += 1
            emit(f"結果：失敗 — {reason}" + (f"（轉址目的主機：{xr.location_host}）" if xr.location_host else ""))
            log(f"::error title=探測失敗 {lab}::{reason}")
            continue
        try:
            for line in analyze_xlsx(xr.body):
                emit(line)
        except fs.FscError as e:
            failed += 1
            emit(f"結果：失敗 — {e}")
            log(f"::error title=探測失敗 {lab}::{e}")
    emit("")
    emit(f"探測完成：{'有失敗，請看上方 ::error::' if failed else '全部成功'}")
    _write_summary(env, report)
    return 1 if failed else 0


def _write_summary(env: Dict[str, str], report: List[str]) -> None:
    path = env.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("### 證期局申報案件彙總表探測（只有結構，沒有內容）\n```\n" + "\n".join(report) + "\n```\n")
    except OSError as e:
        log(f"::warning title=無法寫入執行摘要::{e}")


if __name__ == "__main__":
    sys.exit(run())
