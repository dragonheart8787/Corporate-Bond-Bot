#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金管會證期局「申報案件辦理情形彙總表」的取得與解析（只用標準函式庫，不得把 openpyxl 加進 requirements.txt）。

兩件事：
  1. resolve_year_link：從 sfb.gov.tw 的頁面解析出某年度 xlsx 的連結。檔名含版本（1151008v3.xlsx）每次都會變，
     所以絕不硬編；列的選擇用儲存格文字「完全等於」年度名稱（不是子字串）；找不到或多個候選一律明確報錯。
  2. read_xlsx：用 zipfile + XML 讀 xlsx，保留儲存格的原始文字、型別（字串／數字）與超連結目標（不跟隨）。

使用條款（頁面載明）：引用須註明來源、確保資料完整性不得任意增刪、不得作為商業使用。
本模組不改寫、不過濾任何儲存格內容。
"""

from __future__ import annotations

import io
import re
import unicodedata
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

FSC_HOSTS = {"www.sfb.gov.tw", "sfb.gov.tw", "www.fsc.gov.tw", "fsc.gov.tw"}
PAGE_URL = "https://www.sfb.gov.tw/ch/home.jsp?id=1016&parentpath=0,6,52"

# 18 欄，依序（欄位名稱照原表，不得改名）
EXPECTED_COLUMNS = [
    "證券代號", "公司型態", "結案類型", "公司名稱", "承銷商", "案件類別", "金額", "幣別", "發行價格",
    "收文日期", "自動補正日期", "停止生效日期", "解除生效日期", "生效日期", "廢止/撤銷日期",
    "自行撤回日期", "退件日期", "案件性質",
]
DATE_COLUMNS = ["收文日期", "自動補正日期", "停止生效日期", "解除生效日期", "生效日期", "廢止/撤銷日期", "自行撤回日期", "退件日期"]

MAX_UNCOMPRESSED = 300 * 1024 * 1024
MAX_MEMBERS = 2000
MAX_ROWS = 300_000


class FscError(Exception):
    """無法安全處理（頁面結構、連結、xlsx 結構）。訊息會原樣變成 ::error::。"""

    def __init__(self, message: str, diagnostics: Optional[List[str]] = None):
        super().__init__(message)
        self.diagnostics = diagnostics or []


def norm_label(text: str) -> str:
    """儲存格文字正規化：NFKC、NBSP 當空白、合併空白、去頭尾。比對『完全等於』前一律先過這個。"""
    s = unicodedata.normalize("NFKC", text or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_header(text: str) -> str:
    """表頭欄名正規化：NFKC 後移除所有空白與換行（Excel 表頭常在欄名中間折行，例如「證券\n代號」）。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "").replace("\xa0", " "))


# ─────────────────────────────────────────
# 頁面解析
# ─────────────────────────────────────────
@dataclass
class Cell:
    text: str = ""
    links: List[Tuple[str, str]] = field(default_factory=list)    # (href, 錨點文字)


@dataclass
class Row:
    cells: List[Cell] = field(default_factory=list)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[Row] = []
        self.tables = 0
        self._row_stack: List[Row] = []
        self._cell_stack: List[Cell] = []
        self._buf_stack: List[List[str]] = []
        self._a_href: Optional[str] = None
        self._a_text: List[str] = []
        self.text_chunks: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables += 1
        elif tag == "tr":
            self._row_stack.append(Row())
        elif tag in ("td", "th") and self._row_stack:
            self._cell_stack.append(Cell())
            self._buf_stack.append([])
        elif tag == "a":
            self._a_href = dict(attrs).get("href")
            self._a_text = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell_stack:
            cell = self._cell_stack.pop()
            cell.text = norm_label("".join(self._buf_stack.pop()))
            if self._row_stack:
                self._row_stack[-1].cells.append(cell)
        elif tag == "tr" and self._row_stack:
            self.rows.append(self._row_stack.pop())
        elif tag == "a":
            if self._a_href is not None and self._cell_stack:
                self._cell_stack[-1].links.append((self._a_href, norm_label("".join(self._a_text))))
            self._a_href = None

    def handle_data(self, data):
        self.text_chunks.append(data)
        if self._buf_stack:
            self._buf_stack[-1].append(data)
        if self._a_href is not None:
            self._a_text.append(data)


def parse_page(html: str) -> _PageParser:
    p = _PageParser()
    p.feed(html)
    p.close()
    return p


def _ext(href: str) -> str:
    path = urllib.parse.urlparse(href).path.lower()
    return path.rsplit(".", 1)[-1] if "." in path else ""


@dataclass
class LinkChoice:
    url: str
    anchor_text: str
    filename: str


def resolve_year_link(html: str, page_url: str, label: str, allowed: Optional[set] = None) -> LinkChoice:
    """
    找出『某儲存格文字完全等於 label』的那一列，回傳該列唯一的 .xlsx 連結（ODS 與其他附件忽略）。
    0 列、多列、0 個 xlsx、多個不同 xlsx、網址不是 https 或主機不在白名單 → FscError（附診斷，不含任何公司資料）。
    """
    allowed = FSC_HOSTS if allowed is None else allowed
    p = parse_page(html)
    target = norm_label(label)
    matches = [r for r in p.rows if any(c.text == target for c in r.cells)]
    diag = [f"頁面共 {p.tables} 個表格、{len(p.rows)} 列"]
    labels = sorted({c.text for r in p.rows for c in r.cells if "年度" in c.text and len(c.text) <= 60})
    diag.append("含「年度」的儲存格文字：" + (" ｜ ".join(labels[:40]) if labels else "（無）"))
    if not matches:
        raise FscError(f"頁面上找不到儲存格文字完全等於「{target}」的列", diag)
    if len(matches) > 1:
        raise FscError(f"頁面上有 {len(matches)} 列的儲存格文字完全等於「{target}」，無法確定是哪一列", diag)
    row = matches[0]
    links = [(h, t) for c in row.cells for (h, t) in c.links]
    xlsx = []
    for href, text in links:
        if _ext(href) == "xlsx":
            xlsx.append((urllib.parse.urljoin(page_url, href), text))
    uniq: Dict[str, str] = {}
    for u, t in xlsx:
        uniq.setdefault(u, t)
    diag.append(f"該列的連結副檔名：{sorted({_ext(h) or '（無）' for h, _ in links})}")
    if not uniq:
        raise FscError(f"「{target}」那一列沒有 .xlsx 連結", diag)
    if len(uniq) > 1:
        names = [u.rsplit("/", 1)[-1] for u in uniq]
        raise FscError(f"「{target}」那一列有 {len(uniq)} 個不同的 .xlsx 連結：{names}，無法確定", diag)
    (url, text), = uniq.items()
    u = urllib.parse.urlparse(url)
    if u.scheme != "https":
        raise FscError(f"「{target}」的 xlsx 連結不是 https（{u.scheme or '無'}），不下載", diag)
    if (u.hostname or "").lower() not in allowed:
        raise FscError(f"「{target}」的 xlsx 連結主機 {u.hostname!r} 不在白名單", diag)
    return LinkChoice(url, text[:60], u.path.rsplit("/", 1)[-1])


def page_update_text(html: str) -> str:
    """頁面上的『更新日期』文字（只回日期本身），找不到回空字串。"""
    m = re.search(r"更新(?:日期)?\s*[:：]\s*(\d{2,4}[./\-年]\d{1,2}[./\-月]\d{1,2}日?)", norm_label(re.sub(r"<[^>]+>", " ", html)))
    return m.group(1) if m else ""


# ─────────────────────────────────────────
# xlsx 解析（zipfile + XML）
# ─────────────────────────────────────────
_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_R_ID = "{%s}id" % _NS["r"]


@dataclass
class XCell:
    value: str = ""                 # 儲存格文字（字串型為字串；數字型為 XML 裡的原樣文字，例如 1150109）
    kind: str = "empty"             # str | num | bool | err | empty
    link: Optional[str] = None      # 超連結目標（外部網址或內部位置），不跟隨
    link_kind: str = ""             # external | internal | formula


@dataclass
class XSheet:
    name: str
    rows: List[List[XCell]]


def _col_index(ref: str) -> int:
    letters = re.match(r"([A-Z]+)", ref or "")
    if not letters:
        return 0
    n = 0
    for ch in letters.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _split_ref(ref: str) -> Tuple[int, int]:
    m = re.match(r"([A-Z]+)(\d+)$", ref or "")
    if not m:
        return -1, -1
    return _col_index(m.group(1)), int(m.group(2)) - 1


def _guard_xml(zf: zipfile.ZipFile, name: str) -> None:
    with zf.open(name) as f:
        head = f.read(4096).lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise FscError(f"xlsx 內的 {name} 含 DTD／實體宣告，拒絕解析（防止 XML 炸彈）")


def _read_rels(zf: zipfile.ZipFile, path: str) -> Dict[str, Tuple[str, str]]:
    """關係檔 → {Id: (Target, TargetMode)}。檔案不存在回空。"""
    if path not in zf.namelist():
        return {}
    _guard_xml(zf, path)
    root = ET.fromstring(zf.read(path))
    return {e.get("Id", ""): (e.get("Target", ""), e.get("TargetMode", "")) for e in root.findall("pr:Relationship", _NS)}


def _shared_strings(zf: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    _guard_xml(zf, "xl/sharedStrings.xml")
    out: List[str] = []
    for _, el in ET.iterparse(zf.open("xl/sharedStrings.xml"), events=("end",)):
        if el.tag == "{%s}si" % _NS["m"]:
            parts = []
            # 只取 <si> 直屬的 <t> 與 <r><t>；注音（rPh）裡的 <t> 不是儲存格文字
            for child in el:
                if child.tag == "{%s}t" % _NS["m"]:
                    parts.append(child.text or "")
                elif child.tag == "{%s}r" % _NS["m"]:
                    for t in child.findall("m:t", _NS):
                        parts.append(t.text or "")
            out.append("".join(parts))
            el.clear()
    return out


_HYPERLINK_FN = re.compile(r'HYPERLINK\(\s*"([^"]*)"', re.IGNORECASE)


def _range_cells(ref: str) -> List[str]:
    if ":" not in ref:
        return [ref]
    a, b = ref.split(":", 1)
    c1, r1 = _split_ref(a)
    c2, r2 = _split_ref(b)
    if min(c1, r1, c2, r2) < 0 or (c2 - c1 + 1) * (r2 - r1 + 1) > 5000:
        return [a]
    cells = []
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            col, n = "", c + 1
            while n:
                n, rem = divmod(n - 1, 26)
                col = chr(65 + rem) + col
            cells.append(f"{col}{r + 1}")
    return cells


def _read_sheet(zf: zipfile.ZipFile, name: str, path: str, sst: List[str]) -> XSheet:
    _guard_xml(zf, path)
    rels_path = path.rsplit("/", 1)[0] + "/_rels/" + path.rsplit("/", 1)[1] + ".rels"
    rels = _read_rels(zf, rels_path)
    rows_map: Dict[int, Dict[int, XCell]] = {}
    links: Dict[str, Tuple[str, str]] = {}
    for _, el in ET.iterparse(zf.open(path), events=("end",)):
        tag = el.tag.split("}")[-1]
        if tag == "row":
            r_attr = el.get("r")
            ridx = int(r_attr) - 1 if r_attr and r_attr.isdigit() else (max(rows_map) + 1 if rows_map else 0)
            if ridx >= MAX_ROWS:
                raise FscError(f"工作表 {name!r} 超過 {MAX_ROWS} 列，拒絕解析")
            cells = rows_map.setdefault(ridx, {})
            next_col = 0
            for c in el.findall("m:c", _NS):
                ref = c.get("r") or ""
                cidx = _col_index(ref) if ref else next_col
                next_col = cidx + 1
                t = c.get("t", "n")
                v = c.find("m:v", _NS)
                f = c.find("m:f", _NS)
                xc = XCell()
                if t == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(sst):
                    xc.value, xc.kind = sst[int(v.text)], "str"
                elif t == "inlineStr":
                    is_ = c.find("m:is", _NS)
                    xc.value = "".join((x.text or "") for x in is_.iter("{%s}t" % _NS["m"])) if is_ is not None else ""
                    xc.kind = "str"
                elif t == "str" and v is not None:
                    xc.value, xc.kind = v.text or "", "str"
                elif t == "b" and v is not None:
                    xc.value, xc.kind = v.text or "", "bool"
                elif t == "e" and v is not None:
                    xc.value, xc.kind = v.text or "", "err"
                elif v is not None and v.text is not None:
                    xc.value, xc.kind = v.text, "num"
                if f is not None and f.text:
                    m = _HYPERLINK_FN.search(f.text)
                    if m:
                        xc.link, xc.link_kind = m.group(1), "formula"
                if xc.kind != "empty" or xc.link:
                    cells[cidx] = xc
            el.clear()
        elif tag == "hyperlink":
            ref = el.get("ref") or ""
            rid = el.get(_R_ID)
            location = el.get("location")
            target, mode = rels.get(rid or "", ("", ""))
            for cell_ref in _range_cells(ref):
                if rid and target:
                    links[cell_ref] = (target, "external" if mode == "External" else "internal")
                elif location:
                    links[cell_ref] = (location, "internal")
    for ref, (target, kind) in links.items():
        ci, ri = _split_ref(ref)
        if ci < 0:
            continue
        cell = rows_map.setdefault(ri, {}).setdefault(ci, XCell())
        cell.link, cell.link_kind = target, kind
    rows: List[List[XCell]] = []
    for ridx in sorted(rows_map):
        cells = rows_map[ridx]
        width = (max(cells) + 1) if cells else 0
        while len(rows) < ridx:
            rows.append([])
        rows.append([cells.get(i, XCell()) for i in range(width)])
    return XSheet(name, rows)


def read_xlsx(data: bytes) -> List[XSheet]:
    """xlsx 位元組 → 各工作表。壞檔、DTD、過大、缺必要部件 → FscError。"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise FscError(f"不是有效的 xlsx（zip 結構損毀）：{e}") from None
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise FscError(f"xlsx 內有 {len(infos)} 個部件，超過上限 {MAX_MEMBERS}")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise FscError("xlsx 解壓縮後過大，拒絕解析（zip 炸彈防護）")
    names = set(zf.namelist())
    if "xl/workbook.xml" not in names:
        raise FscError("xlsx 缺少 xl/workbook.xml")
    _guard_xml(zf, "xl/workbook.xml")
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = _read_rels(zf, "xl/_rels/workbook.xml.rels")
    sst = _shared_strings(zf)
    sheets: List[XSheet] = []
    for s in wb.findall("m:sheets/m:sheet", _NS):
        name = s.get("name", "")
        target = rels.get(s.get(_R_ID, ""), ("", ""))[0]
        if not target:
            raise FscError(f"工作表 {name!r} 找不到對應的部件")
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        if path not in names:
            raise FscError(f"工作表 {name!r} 的部件 {path} 不存在")
        try:
            sheets.append(_read_sheet(zf, name, path, sst))
        except ET.ParseError as e:
            raise FscError(f"工作表 {name!r} 的 XML 無法解析：{e}") from None
    if not sheets:
        raise FscError("xlsx 內沒有任何工作表")
    return sheets


def find_header(sheet: XSheet, first_col: str = "證券代號", scan_rows: int = 15) -> Optional[int]:
    """表頭列 = 前 scan_rows 列中，第一個含 first_col（正規化後完全相等）儲存格的列。找不到回 None。"""
    for i, row in enumerate(sheet.rows[:scan_rows]):
        if any(norm_header(c.value) == first_col for c in row):
            return i
    return None
