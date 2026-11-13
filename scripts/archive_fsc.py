#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日留存金管會證期局「申報案件辦理情形彙總表」（115、114 兩個年度都查）。只用標準函式庫。
只留存，不接進日報、不接進 /cb、不發任何訊息。

來源頁面使用條款：引用須註明來源、確保資料完整性不得任意增刪、不得作為商業使用 → 原始檔（xlsx，或 xlsx 消失時的 ods）逐位元保留，
解析結果只「附加」欄位（ISO 日期、品質旗標），不修改、不刪除任何原始值。見 data/fsc/SOURCE.md。

連線規則（沿用 probe_sources / fsc_sources）：只允許 https 與 sfb／fsc 主機、不跟隨轉址、不重試、
每次最多 3 個請求（頁面 1 + 兩個年度檔各 1）、請求之間至少 6 秒；連結每次從頁面解析（檔名含版本，不硬編）。

選檔規則（fail-closed，fsc_sources.resolve_year_source）：目標列有 .xlsx → 一律用 xlsx，xlsx 下載或解析失敗該年度就失敗，不得改用 ODS；
沒有 .xlsx 但恰有一個 .ods → 用 ODS（2026-10-21 起 115 年的頁面只有 .xls 與 .ods）；都沒有、或有多個不同的 .ods → 失敗；.xls 一律忽略（不解析 BIFF）。
ODS 經 fsc_sources.read_ods／ods_to_xsheets 轉成與 xlsx 相同的資料結構，之後的解析、旗標、序列化完全共用同一套程式
（數字型儲存格的文字寫法見 ods_number_text），所以同一份資料做成 xlsx 與 ods，parsed 的 rows 逐位元相同。

格式切換閘門（只在「上一版 meta 的 source_format 與本次不同」時啟用，見下方 SWITCH_* 常數）：與上一版 parsed 比對共有案件鍵比例、
八個穩定欄位的不一致比例、ODS 檔名日期與頁面更新日期的差距、最大收文日期的落後天數；任何一項超過門檻就整個年度不寫入。

輸出（預設 data/fsc/，可用 ARCHIVE_FSC_DIR 改）：
  raw/<年度>_<sha8>_<安全檔名>.xlsx   原始檔（歷次版本都留）
  parsed/<年度>.json                  最新版的完整解析：18 欄原始字串、ISO 日期、案件鍵、重複序號、品質旗標
  meta/<年度>.json                    目前版本的 sha256／大小／頁面更新日期文字／結尾列內容／列數／首見日期（台北時間）
  diffs/<年度>_<舊sha8>_<新sha8>.json 新版本出現時：新增／消失的案件、同鍵欄位變更（結案類型與各日期欄）
  _last_check.json                    各年度「最後成功檢查日」（台北 YYYY-MM-DD，只有日期）。某年度本次下載、驗證、解析、寫入全部成功
                                      （無論有無新版本）才更新該年度；失敗的年度日期維持不變。同一台北日重跑逐位元相同；損毀時不覆蓋、報錯。
parsed／raw 不含任何執行時間戳：同一個檔案重跑，輸出逐位元相同（git diff --staged 為空）。
首見日期只在新版本第一次出現時寫入 meta，之後不變。

fail-closed（該年度完全不寫入，::error::，整體結束碼 1；另一年度已成功的資料照常寫入）：
  工作表數 ≠ 1、表頭欄數／欄名／順序不符、資料列比前一版少 20% 以上、證券代號空白、頁面找不到或不唯一的年度列、
  下載失敗／被截斷／被轉址／不是 xlsx、資料列為 0、既有的 meta／parsed 損毀（為避免覆蓋已留存資料）。

環境變數：ARCHIVE_FSC_DIR、ARCHIVE_FSC_PAGE_URL（覆寫頁面網址，仍須通過白名單）、ARCHIVE_DRY_RUN=1、PROBE_REPO、GITHUB_STEP_SUMMARY。
結束碼：0 全部成功；1 至少一項失敗；2 設定不合法（未送出請求）。
"""

from __future__ import annotations

import collections
import hashlib
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import fsc_sources as fs
import probe_fsc as pf          # 同一個目錄：沿用 decode_html／shape_of／roc7_to_iso
import probe_sources as ps

YEARS: Tuple[Tuple[str, str], ...] = (("115", "115年度申報案件"), ("114", "114年度申報案件"))
EXPECTED_SHEET_NAME = "IMP0080A_Upload"
STORE_VERSION = 1
MAX_DROP_NUM, MAX_DROP_DEN = 1, 5          # 資料列比前一版少 1/5（20%）以上 → 拒絕
ONE_DATE_COLUMNS = ("收文日期", "生效日期", "廢止/撤銷日期", "自行撤回日期", "退件日期")
MULTI_DATE_COLUMNS = ("自動補正日期", "停止生效日期", "解除生效日期")
DIFF_FIELDS = ("結案類型",) + tuple(fs.DATE_COLUMNS)
CODE_RE = re.compile(r"[0-9A-Za-z]{3,8}")
NUMERIC_RE = re.compile(r"\d+(?:\.\d+)?")
TOKEN_SPLIT = re.compile(r"[,，、;；\s]+")
YEAR_LABEL_RE = re.compile(r"(\d{3})年度申報案件")
# ── 格式切換閘門（xlsx ⇄ ods）。門檻依據：2026-11-13 的探測（上一版 10-20 xlsx 682 列 vs 11-12 ODS 718 列）──
# 共有案件鍵 ÷ 上一版案件鍵 ≥ 90%：實測 680/682 = 99.7%（23 天間隔、僅 2 筆消失）。90% 容許一個月以上的正常汰換，
# 但擋得住截斷、改版、或整批鍵被重寫的檔案。
SWITCH_MIN_COMMON_KEY_PCT = 90
# 八個穩定欄位（證券代號、案件類別、金額、幣別、收文日期、案件性質、承銷商、公司名稱）在共有鍵上，單一欄位不一致列數 ≤ 共有鍵的 2%：
# 實測這八欄不一致皆為 0。2%（680 列約 13 列）容許來源端合理的更正，但擋得住系統性差異（例如數字寫法、空白、字元編碼不同）。
SWITCH_MAX_COLUMN_MISMATCH_PCT = 2
SWITCH_GATED_COLUMNS = ("證券代號", "案件類別", "金額", "幣別", "收文日期", "案件性質", "承銷商", "公司名稱")
# 公司型態（興櫃→上櫃等）與發行價格（定價後更新）可能合理變動：不納入閘門，但在摘要列出各自的不一致筆數（實測 6 與 1）。
SWITCH_INFO_COLUMNS = ("公司型態", "發行價格")
# 新鮮度一：檔名內的民國日期與頁面更新日期相差 ≤ 3 天。實測兩者相差 0 天（1151112 與 2026-11-12）；
# 3 天容許假日與頁面晚更新，擋住「檔名是舊版本、頁面卻已更新」的落後檔（2026-10-08 曾出現 xlsx 為 v3、ods 檔名仍為 v1）。
SWITCH_MAX_FILE_PAGE_GAP_DAYS = 3
# 新鮮度二：最大收文日期不得比頁面更新日早超過 14 天。實測差 1 天；收文是逐日發生，落後兩週以上代表內容沒有跟上。
SWITCH_MAX_RECEIPT_LAG_DAYS = 14
ODS_ACCEPT = "application/vnd.oasis.opendocument.spreadsheet, application/octet-stream;q=0.9, */*;q=0.5"
MAX_LISTED_ROWS = 200
MAX_TRAILER_LOG_CHARS = 80
TAIPEI = timezone(timedelta(hours=8))

F_NONEXISTENT = "日期不存在"
F_UNPARSEABLE = "日期無法解析"
F_LATER = "日期晚於來源更新日"
F_MULTI_IN_ONE = "單一日期欄含多值"
F_AMOUNT_CHANGE = "同日同類別多筆"          # 同 證券代號+案件類別+收文日期 出現不同金額的多筆（中性名稱：只標記事實，不推論是金額變更）
F_AMOUNT_NONNUM = "金額非純數字"
F_PRICE_NONNUM = "發行價格非純數字"
F_EXTRA_CELLS = "表頭外有資料"


def log(msg: str) -> None:
    print(msg, flush=True)


class ArchiveError(Exception):
    """該年度（或檔案）無法安全處理。訊息會原樣變成 ::error::。"""


# ─────────────────────────────────────────
# 檔案讀寫
# ─────────────────────────────────────────
def write_bytes_atomic(path: str, data: bytes) -> bool:
    """原子寫入；內容與既有檔案完全相同就不動它。回傳是否真的寫了。"""
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except FileNotFoundError:
        pass
    except OSError as e:
        raise ArchiveError(f"無法讀取既有檔案 {path}：{e}") from e
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise ArchiveError(f"寫入 {path} 失敗：{e}") from e
    return True


def dump_meta(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def dump_rows_file(head: Dict[str, Any], rows: List[Dict[str, Any]]) -> bytes:
    """parsed 檔：頂層欄位 + 每列一行（git diff 好讀、且確定性：鍵排序、不含時間）。"""
    parts = ["{"]
    for k in sorted(head):
        parts.append(f" {json.dumps(k, ensure_ascii=False)}: {json.dumps(head[k], ensure_ascii=False, sort_keys=True)},")
    parts.append(' "rows": [')
    for i, r in enumerate(rows):
        parts.append("  " + json.dumps(r, ensure_ascii=False) + ("," if i < len(rows) - 1 else ""))   # 列內維持建構順序（確定性），欄位順序 = 原表
    parts.append(" ]")
    parts.append("}")
    return ("\n".join(parts) + "\n").encode("utf-8")


def read_json(path: str, what: str) -> Optional[Any]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ArchiveError(f"既有的{what} {path} 無法讀取（{type(e).__name__}），為避免覆蓋已留存的資料，不處理") from e


def safe_filename(decoded: str, ext: str = ".xlsx") -> str:
    """百分比編碼解碼後的檔名 → 安全檔名：只留文字／數字／. - _ ( )，其餘換成 _；不含路徑分隔、不以 . 開頭；副檔名保留（xlsx 或 ods）。"""
    name = unicodedata.normalize("NFC", decoded).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^\w.\-()]", "_", name).lstrip(".")
    if not name.lower().endswith(ext):
        name = (name or "file") + ext
    if len(name) > 80:
        name = name[:80 - len(ext)] + ext
    return name


# ─────────────────────────────────────────
# 日期
# ─────────────────────────────────────────
def parse_date_cell(raw: str, multi_ok: bool) -> Tuple[List[Optional[str]], List[str]]:
    """原始儲存格文字 → (逐個 token 的 ISO 日期或 None, 旗標)。空值回 ([], [])。原始值不動，這裡只是附加資訊。"""
    text = unicodedata.normalize("NFKC", raw or "").strip()
    if not text:
        return [], []
    tokens = [t for t in TOKEN_SPLIT.split(text) if t]
    isos: List[Optional[str]] = []
    flags: List[str] = []
    for t in tokens:
        iso = pf.roc7_to_iso(t)
        isos.append(iso)
        if iso is None:
            flags.append(F_NONEXISTENT if re.fullmatch(r"\d{7}", t) else F_UNPARSEABLE)
    if len(tokens) > 1 and not multi_ok:
        flags.append(F_MULTI_IN_ONE)
    return isos, flags


def filename_date(decoded: str) -> Optional[str]:
    m = re.match(r"(\d{7})", decoded)
    return pf.roc7_to_iso(m.group(1)) if m else None


def file_version(decoded: str) -> str:
    m = re.search(r"v(\d+)", decoded, re.IGNORECASE)
    return m.group(1) if m else ""


def page_text_to_iso(text: str) -> Optional[str]:
    m = re.fullmatch(r"(\d{2,4})[./\-年](\d{1,2})[./\-月](\d{1,2})日?", (text or "").strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 1911:
        y += 1911
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def reference_date(file_iso: Optional[str], page_iso: Optional[str]) -> Optional[str]:
    """來源更新日 = 檔名日期與頁面更新日期中較晚者（保守：較少誤報「晚於來源更新日」）。都沒有則 None。"""
    got = [d for d in (file_iso, page_iso) if d]
    return max(got) if got else None


# ─────────────────────────────────────────
# xlsx → 資料列
# ─────────────────────────────────────────
@dataclass
class SheetParse:
    sheet_name: str
    header_row: int                                   # 1 起算
    data: List[Tuple[int, List[str], List[str]]]      # (列號, 18 欄原始字串, 表頭外的非空儲存格)
    trailer: List[Dict[str, Any]]
    blank_rows: int                                   # 表頭之後所有全空列（含資料列之間與結尾）
    trailing_blank_rows: int                          # 其中「最後一筆資料之後」的全空列
    total_rows: int


def _is_trailer_like(vals: List[str], code_i: int) -> bool:
    nonempty = sum(1 for v in vals if fs.norm_label(v))
    if nonempty > 2:
        return False
    code = fs.norm_label(vals[code_i]) if code_i < len(vals) else ""
    return not CODE_RE.fullmatch(code)


def read_source_sheets(data: bytes, fmt: str) -> List[fs.XSheet]:
    """原始檔位元組 → 工作表（xlsx 與 ods 共用同一種資料結構）。fmt 由選檔規則決定，這裡不會猜格式、不會改用另一種。"""
    if fmt == "ods":
        cat, desc = pf.sniff(data)
        if cat != "zip":
            raise ArchiveError(f"ods 無法解析：檔案開頭不是 zip（{desc}）；副檔名與實際格式不符或被阻擋頁取代")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                mt = zf.read("mimetype")[:200].decode("ascii", "replace").strip() if "mimetype" in zf.namelist() else None
        except zipfile.BadZipFile as e:
            raise ArchiveError(f"ods 無法解析：zip 結構損毀（{e}）") from None
        if mt != fs.ODS_MIME:
            raise ArchiveError(f"ods 無法解析：zip 的 mimetype {'缺少' if mt is None else '不是 ODS（' + repr(mt[:60]) + '）'}")
        try:
            return fs.ods_to_xsheets(fs.read_ods(data))
        except fs.FscError as e:
            raise ArchiveError(f"ods 無法解析：{e}") from None
    try:
        return fs.read_xlsx(data)
    except fs.FscError as e:
        raise ArchiveError(f"xlsx 無法解析：{e}") from None


def parse_sheet(data: bytes, fmt: str = "xlsx") -> SheetParse:
    sheets = read_source_sheets(data, fmt)
    if len(sheets) != 1:
        raise ArchiveError(f"工作表數量為 {len(sheets)}（預期恰好 1 個），格式可能改版，不寫入")
    sheet = sheets[0]
    h = fs.find_header(sheet)
    if h is None:
        raise ArchiveError("前 15 列找不到含「證券代號」的表頭列，不寫入")
    header = [fs.norm_header(c.value) for c in sheet.rows[h]]
    while header and header[-1] == "":
        header.pop()
    if header != fs.EXPECTED_COLUMNS:
        missing = [c for c in fs.EXPECTED_COLUMNS if c not in header]
        extra = [c for c in header if c not in fs.EXPECTED_COLUMNS]
        raise ArchiveError(
            f"表頭與預期的 18 欄不符（欄數 {len(header)}；缺少 {missing}；多出 {extra}；"
            f"{'名稱相同但順序不同' if not missing and not extra and len(header) == len(fs.EXPECTED_COLUMNS) else '名稱或欄數不同'}），不寫入")
    n = len(fs.EXPECTED_COLUMNS)
    code_i = fs.EXPECTED_COLUMNS.index("證券代號")

    def vals(row: List[fs.XCell]) -> List[str]:
        return [(row[i].value if i < len(row) else "") for i in range(n)]

    body = [(i + 1, sheet.rows[i]) for i in range(h + 1, len(sheet.rows))]
    # 結尾的非資料列：從最後一列往回，連續「近乎空白且證券代號不像代號」的列（說明文字），中間夾的空白列略過
    tail_start = len(body)
    while tail_start > 0:
        rn, row = body[tail_start - 1]
        v = vals(row)
        if not any(fs.norm_label(x.value) for x in row) or _is_trailer_like(v, code_i):
            tail_start -= 1
        else:
            break
    trailer: List[Dict[str, Any]] = []
    blank = 0
    for rn, row in body[tail_start:]:
        cells = [{"col": fs.EXPECTED_COLUMNS[i] if i < n else f"第{i + 1}欄", "value": c.value} for i, c in enumerate(row) if fs.norm_label(c.value)]
        if cells:
            trailer.append({"row": rn, "cells": cells})
    rows: List[Tuple[int, List[str], List[str]]] = []
    for rn, row in body[:tail_start]:
        if not any(fs.norm_label(x.value) for x in row):
            blank += 1
            continue
        extra_cells = [c.value for c in row[n:] if fs.norm_label(c.value)]
        rows.append((rn, vals(row), extra_cells))
    trailing_blank = sum(1 for rn, row in body[tail_start:] if not any(fs.norm_label(x.value) for x in row))
    blank += trailing_blank
    for rn, v, _ in rows:
        if not fs.norm_label(v[code_i]):
            raise ArchiveError(f"第 {rn} 列的證券代號空白（資料列不得缺證券代號），不寫入")
    if not rows:
        raise ArchiveError("表頭之後沒有任何資料列，不寫入")
    return SheetParse(sheet.name, h + 1, rows, trailer, blank, trailing_blank, len(sheet.rows))


def build_case_rows(sp: SheetParse, ref_iso: Optional[str]) -> List[Dict[str, Any]]:
    cols = fs.EXPECTED_COLUMNS
    idx = {c: i for i, c in enumerate(cols)}
    out: List[Dict[str, Any]] = []
    seen: collections.Counter = collections.Counter()
    for rn, v, extra in sp.data:
        fields = {c: v[i] for c, i in idx.items()}
        key_parts = [fs.norm_label(fields[c]) for c in ("證券代號", "案件類別", "收文日期", "金額")]
        key = "|".join(key_parts)
        seen[tuple(key_parts)] += 1
        flags: List[str] = []
        dates: Dict[str, List[Optional[str]]] = {}
        for c in fs.DATE_COLUMNS:
            isos, fl = parse_date_cell(fields[c], multi_ok=c in MULTI_DATE_COLUMNS)
            if isos:
                dates[c] = isos
            flags.extend(f"{f}:{c}" for f in fl)
            if ref_iso and any(i and i > ref_iso for i in isos):
                flags.append(f"{F_LATER}:{c}")
        nums: Dict[str, Any] = {}
        for c, flag in (("金額", F_AMOUNT_NONNUM), ("發行價格", F_PRICE_NONNUM)):
            raw = fields[c].strip()
            if not raw:
                continue
            if NUMERIC_RE.fullmatch(raw):
                nums[c] = int(raw) if raw.isdigit() else float(raw)
            else:
                flags.append(flag)
        if extra:
            flags.append(F_EXTRA_CELLS)
        row: Dict[str, Any] = {
            "row": rn, "key": key, "key_parts": key_parts, "dup_seq": seen[tuple(key_parts)],
            "fields": fields, "dates_iso": dates, "numbers": nums, "flags": flags,
        }  # fields 內保持原表欄位順序（dump_rows_file 不排序列內的鍵）
        if extra:
            row["extra_cells"] = extra
        out.append(row)
    totals = collections.Counter(tuple(r["key_parts"]) for r in out)
    amounts_by_case: Dict[Tuple[str, str, str], set] = collections.defaultdict(set)
    for r in out:
        kp = r["key_parts"]
        amounts_by_case[(kp[0], kp[1], kp[2])].add(kp[3])
    for r in out:
        kp = r["key_parts"]
        r["dup_count"] = totals[tuple(kp)]
        if len(amounts_by_case[(kp[0], kp[1], kp[2])]) > 1:
            r["flags"].append(F_AMOUNT_CHANGE)
        r["flags"] = sorted(r["flags"])
    return [{"id": f"{r['key']}#{r['dup_seq']}", **r} for r in out]


# ─────────────────────────────────────────
# 版本比對
# ─────────────────────────────────────────
def compute_diff(old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]], year: str, old_sha: str, new_sha: str,
                 fmt_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    old = {r["id"]: r for r in old_rows}
    new = {r["id"]: r for r in new_rows}
    added = sorted(set(new) - set(old))
    vanished = sorted(set(old) - set(new))
    changed = []
    for i in sorted(set(old) & set(new)):
        for f in DIFF_FIELDS:
            a, b = old[i]["fields"].get(f, ""), new[i]["fields"].get(f, "")
            if a != b:
                changed.append({"id": i, "field": f, "old": a, "new": b})
    obj = {
        "version": STORE_VERSION, "year": year, "old_sha256": old_sha, "new_sha256": new_sha,
        "counts": {"added": len(added), "vanished": len(vanished), "changed_fields": len(changed),
                   "old_rows": len(old_rows), "new_rows": len(new_rows)},
        "added": added, "vanished": vanished, "changed": changed,
        "format_change": False,
    }
    if fmt_info:
        obj.update(fmt_info)     # format_change、old_format、new_format、old_version_date、new_version_date、switch_gate（閘門的實際數值）
    return obj


# ─────────────────────────────────────────
# 格式切換閘門
# ─────────────────────────────────────────
def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(a) - date.fromisoformat(b)).days


def check_format_switch(old_rows: List[Dict[str, Any]], new_rows: List[Dict[str, Any]], file_iso: Optional[str],
                        page_iso: Optional[str]) -> Dict[str, Any]:
    """
    上一版與本次的原始檔格式不同時才呼叫。任何一項超過門檻就 ArchiveError（該年度完全不寫入）。
    回傳實際數值（只有數字），供摘要與 diffs 檔記錄。
    """
    old = {r["id"]: r for r in old_rows}
    new = {r["id"]: r for r in new_rows}
    common = sorted(set(old) & set(new))
    stats: Dict[str, Any] = {"old_keys": len(old), "new_keys": len(new), "common_keys": len(common)}
    if not old:
        raise ArchiveError("格式切換：上一版沒有任何案件鍵，無法比對，不寫入")
    # 共有鍵比例：common ÷ old ≥ 90%（用整數比較避免浮點誤差）
    stats["common_key_pct"] = round(100 * len(common) / len(old), 2)
    if len(common) * 100 < SWITCH_MIN_COMMON_KEY_PCT * len(old):
        raise ArchiveError(f"格式切換閘門：共有案件鍵 {len(common)}／上一版 {len(old)}（{stats['common_key_pct']}%）"
                           f"低於 {SWITCH_MIN_COMMON_KEY_PCT}%，不寫入")
    mism: Dict[str, int] = {}
    for col in SWITCH_GATED_COLUMNS + SWITCH_INFO_COLUMNS:
        mism[col] = sum(1 for i in common if old[i]["fields"].get(col, "") != new[i]["fields"].get(col, ""))
    stats["mismatch"] = {c: mism[c] for c in SWITCH_GATED_COLUMNS}
    stats["info_mismatch"] = {c: mism[c] for c in SWITCH_INFO_COLUMNS}
    for col in SWITCH_GATED_COLUMNS:
        if mism[col] * 100 > SWITCH_MAX_COLUMN_MISMATCH_PCT * len(common):
            raise ArchiveError(f"格式切換閘門：共有案件鍵 {len(common)} 筆中，「{col}」不一致 {mism[col]} 筆"
                               f"（超過 {SWITCH_MAX_COLUMN_MISMATCH_PCT}%），不寫入")
    # 新鮮度：無法驗證就當作不通過（fail-closed）
    if not file_iso or not page_iso:
        raise ArchiveError("格式切換閘門：檔名日期或頁面更新日期無法解析，無法驗證新鮮度，不寫入")
    gap = abs(_days_between(file_iso, page_iso))
    stats["filename_page_gap_days"] = gap
    if gap > SWITCH_MAX_FILE_PAGE_GAP_DAYS:
        raise ArchiveError(f"格式切換閘門：檔名日期與頁面更新日期相差 {gap} 天（超過 {SWITCH_MAX_FILE_PAGE_GAP_DAYS} 天），不寫入")
    receipts = [i for r in new_rows for i in (r["dates_iso"].get("收文日期") or []) if i]
    if not receipts:
        raise ArchiveError("格式切換閘門：新版沒有可解析的收文日期，無法驗證新鮮度，不寫入")
    lag = _days_between(page_iso, max(receipts))
    stats["max_receipt_lag_days"] = lag
    if lag > SWITCH_MAX_RECEIPT_LAG_DAYS:
        raise ArchiveError(f"格式切換閘門：最大收文日期比頁面更新日早 {lag} 天（超過 {SWITCH_MAX_RECEIPT_LAG_DAYS} 天），不寫入")
    return stats


def validate_meta(meta: Any, path: str) -> Dict[str, Any]:
    ok = (isinstance(meta, dict) and isinstance(meta.get("sha256"), str) and isinstance(meta.get("history"), list)
          and isinstance(meta.get("row_counts"), dict) and isinstance(meta["row_counts"].get("data_rows"), int)
          and isinstance(meta.get("page_update_text", ""), str))
    if not ok:
        raise ArchiveError(f"既有的 meta {path} 結構不符，為避免覆蓋已留存的資料，不處理")
    return meta


def load_prev_rows(path: str) -> List[Dict[str, Any]]:
    obj = read_json(path, "parsed 檔")
    if not isinstance(obj, dict) or not isinstance(obj.get("rows"), list) or not all(isinstance(r, dict) and "id" in r and "fields" in r for r in obj["rows"]):
        raise ArchiveError(f"既有的 parsed 檔 {path} 不存在或結構不符，無法與新版本比對；為避免覆蓋已留存的資料，不處理")
    return obj["rows"]


# ─────────────────────────────────────────
# 單一年度
# ─────────────────────────────────────────
@dataclass
class YearResult:
    year: str
    label: str
    ok: bool = False
    status: str = ""                 # new-version | unchanged | first
    rows: List[Dict[str, Any]] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    ref_iso: Optional[str] = None
    wrote: List[str] = field(default_factory=list)
    diff: Optional[Dict[str, Any]] = None        # 與前一版的比對（新版本時）；含 format_change 與閘門數值


def process_year(year: str, label: str, link: fs.LinkChoice, body: bytes, page_text: str, directory: str, today: str,
                 fmt: Optional[str] = None) -> YearResult:
    fmt = fmt or getattr(link, "fmt", "xlsx")
    ext = "." + fmt
    res = YearResult(year, label)
    sha = hashlib.sha256(body).hexdigest()
    sha8 = sha[:8]
    sp = parse_sheet(body, fmt)
    if sp.sheet_name != EXPECTED_SHEET_NAME:
        log(f"::warning title=工作表名稱不同 {label}::工作表名稱與先前確認的不同（長度 {len(sp.sheet_name)}），表頭已通過比對，繼續處理並記錄在 meta")

    meta_path = os.path.join(directory, "meta", f"{year}.json")
    parsed_path = os.path.join(directory, "parsed", f"{year}.json")
    prev = read_json(meta_path, "meta")
    prev_meta = validate_meta(prev, meta_path) if prev is not None else None
    same = bool(prev_meta and prev_meta["sha256"] == sha)

    decoded = urllib.parse.unquote(link.filename)
    # 同一版本重跑：沿用首見時記錄的頁面更新日期文字，輸出才會逐位元相同（頁面文字之後變動不影響已留存版本）
    used_page_text = prev_meta.get("page_update_text", "") if same and prev_meta else page_text
    file_iso = filename_date(decoded)
    page_iso = page_text_to_iso(used_page_text)
    ref_iso = reference_date(file_iso, page_iso)
    rows = build_case_rows(sp, ref_iso)

    if prev_meta and not same:
        prev_n, new_n = prev_meta["row_counts"]["data_rows"], len(rows)
        if prev_n > 0 and (prev_n - new_n) * MAX_DROP_DEN >= prev_n * MAX_DROP_NUM:
            raise ArchiveError(f"資料列數 {new_n} 比前一版 {prev_n} 少 {prev_n - new_n} 列"
                               f"（≥{100 * MAX_DROP_NUM // MAX_DROP_DEN}%），疑似資料被截斷或改版，不寫入")

    # 格式切換閘門：上一版的來源格式與本次不同才啟用（舊 meta 沒有 source_format → 視為 xlsx）。任何寫入之前判斷。
    prev_fmt = prev_meta.get("source_format", "xlsx") if prev_meta else None
    switched = bool(prev_meta and not same and prev_fmt != fmt)
    old_rows: List[Dict[str, Any]] = load_prev_rows(parsed_path) if prev_meta and not same else []
    gate: Optional[Dict[str, Any]] = check_format_switch(old_rows, rows, file_iso, page_iso) if switched else None

    history = list(prev_meta["history"]) if prev_meta else []
    entry = next((h for h in history if h.get("sha256") == sha), None)
    if entry is None:
        entry = {"sha256": sha, "sha8": sha8, "first_seen": today, "size": len(body), "filename": decoded, "format": fmt}
        history.append(entry)

    diff_obj = None
    if prev_meta and not same:
        fmt_info = None
        if switched:
            fmt_info = {"format_change": True, "old_format": prev_fmt, "new_format": fmt,
                        "old_version_date": prev_meta.get("reference_date"), "new_version_date": ref_iso,
                        "switch_gate": gate}
        else:
            fmt_info = {"old_format": prev_fmt, "new_format": fmt,
                        "old_version_date": prev_meta.get("reference_date"), "new_version_date": ref_iso}
        diff_obj = compute_diff(old_rows, rows, year, prev_meta["sha256"], sha, fmt_info)

    meta = {
        "version": STORE_VERSION,
        "year": year,
        "year_label": label,
        "sha256": sha,
        "size": len(body),
        "source_page": fs.PAGE_URL,
        "source_url": link.url,
        "link_text": link.anchor_text,
        "filename_original": link.filename,
        "filename_decoded": decoded,
        "filename_safe": safe_filename(decoded, ext),
        "source_format": fmt,
        "source_ext": getattr(link, "ext", ext),
        "file_version": file_version(decoded),
        "page_update_text": used_page_text,
        "source_date_filename": file_iso,
        "source_date_page": page_iso,
        "reference_date": ref_iso,
        "sheet_name": sp.sheet_name,
        "header_row": sp.header_row,
        "columns": list(fs.EXPECTED_COLUMNS),
        "trailer_rows": sp.trailer,
        "row_counts": {"sheet_rows": sp.total_rows, "data_rows": len(rows), "trailer_rows": len(sp.trailer),
                       "trailing_blank_rows": sp.trailing_blank_rows, "blank_rows": sp.blank_rows},
        "first_seen": entry["first_seen"],
        "history": history,
    }
    head = {"version": STORE_VERSION, "year": year, "sha256": sha, "reference_date": ref_iso, "columns": list(fs.EXPECTED_COLUMNS)}

    raw_path = os.path.join(directory, "raw", f"{year}_{sha8}_{meta['filename_safe']}")
    if os.path.exists(raw_path):
        try:
            with open(raw_path, "rb") as f:
                existing = f.read()
        except OSError as e:
            raise ArchiveError(f"無法讀取既有的原始檔 {raw_path}：{e}") from e
        if existing != body:
            raise ArchiveError(f"既有的原始檔 {raw_path} 與本次下載內容不同（sha8 相同但內容不同），不覆蓋")
    elif write_bytes_atomic(raw_path, body):
        res.wrote.append(raw_path)
    if diff_obj is not None:
        dp = os.path.join(directory, "diffs", f"{year}_{prev_meta['sha256'][:8]}_{sha8}.json")  # type: ignore[index]
        if write_bytes_atomic(dp, dump_meta(diff_obj)):
            res.wrote.append(dp)
        c = diff_obj["counts"]
        log(f"  新版本{'（⚠ 跨格式切換 ' + str(prev_fmt) + ' → ' + fmt + '）' if switched else ''}："
            f"與前一版比對 新增 {c['added']}、消失 {c['vanished']}、同鍵欄位變更 {c['changed_fields']}")
    # meta 最後寫：它是「這個版本已完整留存」的標記，中途失敗下次會重做
    for path, data in ((parsed_path, dump_rows_file(head, rows)), (meta_path, dump_meta(meta))):
        if write_bytes_atomic(path, data):
            res.wrote.append(path)
    res.ok, res.rows, res.meta, res.ref_iso, res.diff = True, rows, meta, ref_iso, diff_obj
    res.status = "unchanged" if same else ("new-version" if prev_meta else "first")
    return res


# ─────────────────────────────────────────
# 摘要（只有數字；唯一會列出的個別資料是「生效日期晚於來源更新日」那幾列的 證券代號／案件類別／收文日期）
# ─────────────────────────────────────────
def date_shape_counts(rows: List[Dict[str, Any]], col: str) -> Dict[str, int]:
    c: collections.Counter = collections.Counter()
    for r in rows:
        raw = r["fields"][col]
        text = unicodedata.normalize("NFKC", raw or "").strip()
        if not text:
            c["空值"] += 1
            continue
        toks = [t for t in TOKEN_SPLIT.split(text) if t]
        isos = r["dates_iso"].get(col, [])
        if len(toks) == 1 and re.fullmatch(r"\d{7}", toks[0]):
            c["7碼" if isos and isos[0] else "7碼但日期不存在"] += 1
        elif len(toks) > 1 and all(re.fullmatch(r"\d{7}", t) for t in toks):
            c[f"逗號多值{len(toks)}個" + ("" if all(isos) else "含不存在日期")] += 1
        else:
            c["其他:" + pf.shape_of(raw)] += 1
    return dict(c)


def summarize_year(res: YearResult) -> List[str]:
    rows, m = res.rows, res.meta
    out = [f"### {res.label}（{res.status}）"]
    out.append(f"- 來源格式：{m.get('source_format', 'xlsx')}（連結副檔名 {m.get('source_ext', '.xlsx')}）｜原始檔 raw/{res.year}_{m['sha256'][:8]}_{m['filename_safe']}")
    d = res.diff
    if d and d.get("format_change"):
        c, g = d["counts"], d["switch_gate"]
        out.append(f"- ⚠ 跨格式切換：{d['old_format']} → {d['new_format']}｜上一版日期 {d['old_version_date'] or '（無）'} → 本版 {d['new_version_date'] or '（無）'}")
        out.append(f"  - 與上一版 diff：新增 {c['added']}、消失 {c['vanished']}、同鍵欄位變動 {c['changed_fields']}（上一版 {c['old_rows']} 列 → 本版 {c['new_rows']} 列）")
        out.append(f"  - 閘門（通過）：案件鍵 上一版 {g['old_keys']}／本版 {g['new_keys']}／共有 {g['common_keys']}"
                   f"（{g['common_key_pct']}%，門檻 ≥ {SWITCH_MIN_COMMON_KEY_PCT}%）")
        out.append("  - 閘門欄位不一致筆數（門檻 ≤ 共有鍵的 %d%%）：" % SWITCH_MAX_COLUMN_MISMATCH_PCT
                   + "；".join(f"{k}={v}" for k, v in g["mismatch"].items()))
        out.append("  - 不納入閘門、僅列出：" + "；".join(f"{k}={v}" for k, v in g["info_mismatch"].items()))
        out.append(f"  - 新鮮度：檔名日期與頁面更新日相差 {g['filename_page_gap_days']} 天（門檻 ≤ {SWITCH_MAX_FILE_PAGE_GAP_DAYS}）；"
                   f"最大收文日期比頁面更新日早 {g['max_receipt_lag_days']} 天（門檻 ≤ {SWITCH_MAX_RECEIPT_LAG_DAYS}）")
    rc = m["row_counts"]
    trail_total = rc["trailer_rows"] + rc.get("trailing_blank_rows", 0)
    out.append(f"- 版本 sha8={m['sha256'][:8]}｜大小 {m['size']} bytes｜資料列 {rc['data_rows']}、結尾列 {trail_total}"
               f"（含文字 {rc['trailer_rows']} + 全空 {rc.get('trailing_blank_rows', 0)}）、全部空白列 {rc['blank_rows']}"
               f"｜首見日期 {m['first_seen']}｜來源更新日 {m['reference_date'] or '（無法判定）'}")
    for t in m["trailer_rows"]:
        txt = " / ".join(c["value"] for c in t["cells"])
        out.append(f"- 結尾列（含文字）第 {t['row']} 列：{txt[:MAX_TRAILER_LOG_CHARS]}")
    if trail_total != 2:
        out.append(f"- ⚠ 結尾列合計 {trail_total}（含文字 {rc['trailer_rows']} + 全空 {rc.get('trailing_blank_rows', 0)}；先前確認為 2），請確認格式是否改變（提示，不是失敗）")
    for col in ("案件類別", "結案類型"):
        cnt = collections.Counter(fs.norm_label(r["fields"][col]) or "（空）" for r in rows)
        out.append(f"- {col}（{len(cnt)} 種）：" + "；".join(f"{k}×{v}" for k, v in cnt.most_common(40)))
    out.append("- 日期欄樣態：")
    for col in fs.DATE_COLUMNS:
        sh = date_shape_counts(rows, col)
        out.append(f"  - {col}：" + "；".join(f"{k}×{v}" for k, v in sorted(sh.items(), key=lambda kv: -kv[1])[:12]))
    flag_counts: collections.Counter = collections.Counter()
    for r in rows:
        for f in r["flags"]:
            flag_counts[f] += 1
    out.append("- 品質旗標（只標記，資料未修改）：" + ("；".join(f"{k}×{v}" for k, v in sorted(flag_counts.items())) if flag_counts else "無"))
    dup_groups = len({tuple(r["key_parts"]) for r in rows if r["dup_count"] > 1})
    out.append(f"- 案件鍵重複：{dup_groups} 組（全部保留，以重複序號區分）")
    cross: collections.Counter = collections.Counter()
    for r in rows:
        has = bool(fs.norm_label(r["fields"]["生效日期"]))
        cross[(fs.norm_label(r["fields"]["結案類型"]) or "（空）", has)] += 1
    out.append("- 結案類型 × 生效日期有無：" + "；".join(
        f"{k}：有{cross.get((k, True), 0)}／無{cross.get((k, False), 0)}" for k in sorted({k for k, _ in cross})))
    later = [r for r in rows if f"{F_LATER}:生效日期" in r["flags"]]
    out.append(f"- 生效日期晚於來源更新日（{res.ref_iso or '無法判定'}）：{len(later)} 列")
    for r in later[:MAX_LISTED_ROWS]:
        f = r["fields"]
        out.append(f"  - {f['證券代號']}／{f['案件類別']}／{f['收文日期']}")
    if len(later) > MAX_LISTED_ROWS:
        out.append(f"  - …另有 {len(later) - MAX_LISTED_ROWS} 列未列出")
    return out


def write_summary(env: Dict[str, str], lines: List[str]) -> None:
    path = env.get("GITHUB_STEP_SUMMARY", "")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("## 證期局申報案件彙總表留存（只有數字）\n" + "\n".join(lines) + "\n")
    except OSError as e:
        log(f"::warning title=無法寫入執行摘要::{e}")


# ─────────────────────────────────────────
# 最後成功檢查日（給報告判斷「抓取是否還在運作」：來源多日沒有新版本 ≠ 抓取壞了）
# ─────────────────────────────────────────
LAST_CHECK_NAME = "_last_check.json"
_YEAR_KEY = re.compile(r"\d{3}")
_ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _valid_day(text: Any) -> bool:
    if not isinstance(text, str) or not _ISO_DAY.fullmatch(text):
        return False
    try:
        date.fromisoformat(text)
        return True
    except ValueError:
        return False


def load_last_check(directory: str) -> Dict[str, str]:
    """讀 _last_check.json。不存在 → {}；損毀或欄位異常 → ArchiveError（呼叫端不得覆蓋它）。"""
    path = os.path.join(directory, LAST_CHECK_NAME)
    obj = read_json(path, "最後檢查紀錄")
    if obj is None:
        return {}
    ok = (isinstance(obj, dict) and obj.get("version") == STORE_VERSION and isinstance(obj.get("years"), dict)
          and set(obj) == {"version", "years"}
          and all(isinstance(k, str) and _YEAR_KEY.fullmatch(k) and _valid_day(v) for k, v in obj["years"].items()))
    if not ok:
        raise ArchiveError(f"既有的最後檢查紀錄 {path} 結構不符（欄位或日期格式異常），為避免覆蓋，不更新")
    return dict(obj["years"])


def dump_last_check(years: Dict[str, str]) -> bytes:
    """只有日期（台北 YYYY-MM-DD），沒有時間：同一台北日重跑逐位元相同。"""
    return dump_meta({"version": STORE_VERSION, "years": dict(sorted(years.items()))})


# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────
def run(
    env: Optional[Dict[str, str]] = None,
    *,
    opener: Optional[Any] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Optional[Callable[[], datetime]] = None,
) -> int:
    env = dict(os.environ if env is None else env)
    directory = env.get("ARCHIVE_FSC_DIR") or os.path.join("data", "fsc")
    page_url = (env.get("ARCHIVE_FSC_PAGE_URL") or fs.PAGE_URL).strip()
    ok, why = ps.validate_url(page_url, fs.FSC_HOSTS)
    if not ok:
        log(f"::error title=留存設定不合法::頁面網址：{why}")
        log("未送出任何請求。")
        return 2
    ua = ps.user_agent(env.get("PROBE_REPO", ""), tool="archive-fsc", purpose="daily archive of a public spreadsheet, max 3 requests")
    if env.get("ARCHIVE_DRY_RUN", "") == "1":
        log("🧪 DRY-RUN：不連線、不寫檔。最多送 3 次 GET（間隔 %.0f 秒）：" % ps.INTERVAL_SEC)
        log(f"  1. 頁面：{page_url}")
        for _, lab in YEARS:
            log(f"  - 從頁面解析「{lab}」那一列的連結後 GET 一次：有 .xlsx 用 .xlsx；沒有 .xlsx 且恰有一個 .ods 才用 .ods（連結每次從頁面解析，不硬編）")
        log(f"輸出目錄：{directory}")
        log(f"User-Agent：{ua}")
        return 0

    clock = now or (lambda: datetime.now(TAIPEI))
    today = clock().astimezone(TAIPEI).date().isoformat()
    failed = 0
    summary: List[str] = []
    succeeded: List[str] = []         # 本次下載、驗證、解析、寫入全部成功的年度（無論是否有新版本）
    last_check: Optional[Dict[str, str]]
    try:
        last_check = load_last_check(directory)
    except ArchiveError as e:
        last_check = None             # 損毀就不覆蓋；年度資料照常處理，最後讓這一步變紅
        failed += 1
        log(f"::error title=留存失敗 最後檢查紀錄::{e}")

    fr = ps.fetch_once(page_url, ua, opener, accept="text/html, */*;q=0.5")
    log(f"== [頁面] HTTP {fr.status if fr.status is not None else '無回應'}｜{fr.size} bytes")
    if fr.error or fr.truncated:
        reason = fr.error or "回應超過大小上限"
        log(f"::error title=留存失敗 頁面::{reason}" + (f"（轉址目的主機：{fr.location_host}）" if fr.location_host else ""))
        log("頁面取得失敗，兩個年度都無法解析連結；未下載任何 xlsx。")
        return 1
    html, enc = pf.decode_html(fr.body)
    page_text = fs.page_update_text(html)
    log(f"  編碼 {enc}｜頁面更新日期文字：{page_text or '（找不到）'}")
    parsed_page = fs.parse_page(html)
    labels_on_page = {c.text for r in parsed_page.rows for c in r.cells if YEAR_LABEL_RE.fullmatch(c.text)}
    unknown = sorted(labels_on_page - {lab for _, lab in YEARS})
    if unknown:
        log(f"::warning title=頁面出現未納入留存的年度::{'、'.join(unknown)}（目前只留存 {'、'.join(l for _, l in YEARS)}，需要時請更新 YEARS）")

    links: Dict[str, fs.SourceChoice] = {}
    for year, label in YEARS:
        try:
            links[year] = fs.resolve_year_source(html, page_url, label)
            log(f"  「{label}」→ {links[year].filename}（格式 {links[year].fmt}）")
        except fs.FscError as e:
            failed += 1
            log(f"::error title=留存失敗 {label}::{e}")
            for d in e.diagnostics:
                log(f"  診斷：{d}")

    for year, label in YEARS:
        if year not in links:
            continue
        sleep(max(ps.INTERVAL_SEC, ps.MIN_INTERVAL_SEC))
        link = links[year]
        xr = ps.fetch_once(link.url, ua, opener, accept=pf.XLSX_ACCEPT if link.fmt == "xlsx" else ODS_ACCEPT)
        log(f"== [{label}] HTTP {xr.status if xr.status is not None else '無回應'}｜{xr.size} bytes")
        if xr.error or xr.truncated:
            reason = xr.error or "回應超過大小上限，被截斷"
            failed += 1
            log(f"::error title=留存失敗 {label}::{reason}" + (f"（轉址目的主機：{xr.location_host}）" if xr.location_host else ""))
            continue
        try:
            res = process_year(year, label, link, xr.body, page_text, directory, today, link.fmt)
        except ArchiveError as e:
            failed += 1
            log(f"::error title=留存失敗 {label}::{e}")
            continue
        except Exception as e:  # 沒預料到的錯誤也不能靜默：該年度視為失敗，另一年度照常處理
            failed += 1
            log(f"::error title=留存失敗 {label}::未預期的錯誤 {type(e).__name__}: {e}")
            continue
        succeeded.append(year)
        lines = summarize_year(res)
        summary.extend(lines)
        for ln in lines:
            log(ln)
        log(f"  已寫入 {len(res.wrote)} 個檔案" if res.wrote else "  內容與既有相同，沒有檔案變動")

    # 失敗的年度保持原日期（不得被更新）；已成功的年度記今天（台北）。沒有任何年度成功就不動這個檔。
    if last_check is not None and succeeded:
        updated = dict(last_check)
        for y in succeeded:
            updated[y] = today
        try:
            wrote = write_bytes_atomic(os.path.join(directory, LAST_CHECK_NAME), dump_last_check(updated))
            msg = "最後成功檢查日：" + "、".join(f"{y}年={updated[y]}" for y in sorted(updated)) + ("" if wrote else "（與既有相同，沒有變動）")
            summary.append(msg)
            log(msg)
        except ArchiveError as e:
            failed += 1
            log(f"::error title=留存失敗 最後檢查紀錄::{e}")
    elif last_check is not None:
        log("沒有任何年度成功，最後成功檢查日維持不變")

    summary.append(f"\n結果：{'有失敗，請看 ::error::' if failed else '全部成功'}")
    write_summary(env, summary)
    log(f"留存完成：{'有失敗，請看上方 ::error::' if failed else '全部成功'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
