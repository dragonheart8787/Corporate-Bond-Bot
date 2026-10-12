#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
「金管會申報進度」報告區塊（轉換公司債三類）。資料全部來自 repo 內的 data/fsc/（parsed、meta、diffs），
由 scripts/archive_fsc.py 每日留存；這裡只讀本機檔案，**絕不發出任何網路請求**，也不與公告標題比對。

範圍：案件類別以「轉換公司債(」開頭的三類（無擔保、有擔保、海外無擔保）。
  「交換公司債(…)」不納入：它是發行人以「持有的他公司股票」為標的的交換債，不是可轉換成發行人自家股票的可轉債，
  性質與風險不同，且每年只有 1 筆；混進來會讓「轉換公司債」的筆數與狀態統計失真。要納入需另行決定。

狀態只看「結案類型」，不用日期欄推測：
  生效 → 已生效（生效日期為實際日）
  空值 → 審理中（生效日期若有值只標「預定」；停止生效日期有值且生效日期無值 → 標「停止生效」）
  自行撤回／退件／廢止/撤銷 → 已終止（顯示對應日期欄）
  其他任何值 → 「資料異常」（不猜）
欄位名稱一律照原表；任何日期都不推估、不補值，缺值顯示「—」。

fail-closed：parsed／meta 缺失、損毀、版本不一致、或此版本首次看到日距今 > 7 天 → 區塊明確顯示「申報進度資料缺失/過舊」與原因，
不靜默省略；單一列異常只讓該列顯示「資料異常」並計數，不讓整個報告失敗；任何例外都轉成區塊內的錯誤說明，不往外丟。
時區一律台北（不依賴 runner 的本地時區）。每個區段超過 30 筆時列出前 30 筆並寫「另有 N 筆」，不靜默截斷。

注意（給 filter_cb_only）：本區塊的文字刻意避開報告切段用的標記（「轉換公司債相關公告」「【轉換公司債」「無轉換公司債」
「📋 重大訊息」「📄 其他」等），並以固定的標題行開頭、固定的來源註記行結尾，供 extract_progress_block 整段取出。
"""

from __future__ import annotations

import collections
import glob
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

TW = ZoneInfo("Asia/Taipei")

# 與 scripts/archive_fsc.py 的 YEARS 一致（測試會比對）。第一個是「目前年度」，其後是前一年度（跨年時近期案件可能落在前一年的檔）
FSC_YEARS: Tuple[str, ...] = ("115", "114")
CB_PREFIX = "轉換公司債("
EXCLUDED_NOTE = "交換公司債不納入（標的是持有的他公司股票，非可轉換成自家股票的可轉債；每年僅 1 筆）"
EXPECTED_COLUMNS = [
    "證券代號", "公司型態", "結案類型", "公司名稱", "承銷商", "案件類別", "金額", "幣別", "發行價格",
    "收文日期", "自動補正日期", "停止生效日期", "解除生效日期", "生效日期", "廢止/撤銷日期",
    "自行撤回日期", "退件日期", "案件性質",
]
TERMINATION_COLUMNS = {"自行撤回": "自行撤回日期", "退件": "退件日期", "廢止/撤銷": "廢止/撤銷日期"}
DIFF_FIELDS = ("結案類型", "自動補正日期", "停止生效日期", "解除生效日期", "生效日期", "收文日期",
               "廢止/撤銷日期", "自行撤回日期", "退件日期")

RECENT_DAYS = 14        # 近期已生效／近期終止：今日（台北）往前 14 天（含）
DIFF_DAYS = 3           # 較上次版本的變動：最新版本首次看到日在 3 天（含）內才顯示
STALE_DAYS = 7          # 「沒有檢查紀錄」時的備用規則：目前年度檔的此版本首次看到日距今超過 7 天 → 過舊
CHECK_STALE_DAYS = 3    # 主要規則：某年度「最後成功檢查日」距今（台北日）超過 3 天 → 過舊（抓取可能失敗）
SOURCE_NOTE_MIN_DAYS = 2    # 檢查正常但此版本已 N（≥2）天沒有新版本 → 只註明「來源 N 天未更新」，不算過舊
LAST_CHECK_NAME = "_last_check.json"   # 由 scripts/archive_fsc.py 寫入：{"version":1,"years":{"115":"YYYY-MM-DD",…}}
MAX_LIST = 30
TITLE = "📑 金管會申報進度（轉換公司債）"
FOOTER = "（以上為申報進度；資料來源：金管會證期局「申報案件辦理情形彙總表」，僅供參考，請以原表為準）"
DASH = "—"

_ROC7 = re.compile(r"\d{7}")
_NUM = re.compile(r"\d+(?:\.\d+)?")
_TOKEN_SPLIT = re.compile(r"[,，、;；\s]+")


def default_data_dir() -> str:
    env = os.environ.get("FSC_DATA_DIR", "").strip()
    return env or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "fsc")


def norm(text: Any) -> str:
    s = unicodedata.normalize("NFKC", "" if text is None else str(text)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


# ─────────────────────────────────────────
# 日期
# ─────────────────────────────────────────
def roc7_to_date(token: str) -> Optional[date]:
    if not _ROC7.fullmatch(token):
        return None
    try:
        return date(int(token[:3]) + 1911, int(token[3:5]), int(token[5:]))
    except ValueError:
        return None


def date_tokens(raw: Any) -> List[str]:
    text = norm(raw)
    return [t for t in _TOKEN_SPLIT.split(text) if t] if text else []


def fmt_date_cell(raw: Any) -> str:
    """原樣的日期欄 → 顯示字串。空值「—」；可解析的顯示 ISO；無法解析的顯示原值並註明；多值用「、」。不推估、不補值。"""
    toks = date_tokens(raw)
    if not toks:
        return DASH
    out = []
    for t in toks:
        d = roc7_to_date(t)
        out.append(d.isoformat() if d else f"{t}（原值，無法解析）")
    return "、".join(out)


def single_date(raw: Any) -> Tuple[Optional[date], str]:
    """要求恰好一個可解析的日期。回傳 (日期或 None, 失敗原因)。"""
    toks = date_tokens(raw)
    if not toks:
        return None, "欄位無值"
    if len(toks) > 1:
        return None, "欄位含多個日期"
    d = roc7_to_date(toks[0])
    return (d, "") if d else (None, "日期無法解析")


def parse_iso(text: Any) -> Optional[date]:
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(text or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def taipei_today(now: Optional[datetime]) -> date:
    """今日（台北）。aware 的時間一律換算成台北；naive 視為已經是台北時間。不使用 runner 本地時區。"""
    if now is None:
        return datetime.now(TW).date()
    if now.tzinfo is None:
        return now.date()
    return now.astimezone(TW).date()


# ─────────────────────────────────────────
# 欄位顯示
# ─────────────────────────────────────────
def short_category(cat: str) -> str:
    m = re.fullmatch(r"轉換公司債\((.*)\)", norm(cat))
    return m.group(1) if m else norm(cat)


def fmt_amount(fields: Dict[str, Any]) -> str:
    """金額：純數字時以「億」顯示（來源表未載明單位；與幣別為台幣的典型發行規模對照，視為「元」）；否則原樣顯示並標示。"""
    raw = norm(fields.get("金額"))
    cur = norm(fields.get("幣別"))
    cur_txt = cur if cur else "幣別未載明"
    if not raw:
        return f"金額 {DASH}"
    if _NUM.fullmatch(raw):
        return f"{float(raw) / 1e8:.2f}億 {cur_txt}"
    return f"金額 {raw}（非純數字，原樣顯示；{cur_txt}）"


# ─────────────────────────────────────────
# 資料載入與驗證
# ─────────────────────────────────────────
@dataclass
class YearData:
    year: str
    ok: bool = False
    error: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)
    rows: List[Any] = field(default_factory=list)
    first_seen: Optional[date] = None


def _read_json(path: str) -> Tuple[Any, str]:
    if not os.path.isfile(path):
        return None, "檔案不存在"
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), ""
    except (OSError, ValueError) as e:
        return None, f"無法讀取或不是有效的 JSON（{type(e).__name__}）"


def load_year(data_dir: str, year: str) -> YearData:
    yd = YearData(year)
    meta, err = _read_json(os.path.join(data_dir, "meta", f"{year}.json"))
    if err:
        yd.error = f"meta/{year}.json {err}"
        return yd
    if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str) or not isinstance(meta.get("first_seen"), str):
        yd.error = f"meta/{year}.json 結構不符（缺 sha256 或 first_seen）"
        return yd
    fs = parse_iso(meta["first_seen"])
    if fs is None:
        yd.error = f"meta/{year}.json 的首次看到日期無法解析"
        return yd
    parsed, err = _read_json(os.path.join(data_dir, "parsed", f"{year}.json"))
    if err:
        yd.error = f"parsed/{year}.json {err}"
        return yd
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
        yd.error = f"parsed/{year}.json 結構不符（缺 rows 陣列）"
        return yd
    if parsed.get("sha256") != meta["sha256"]:
        yd.error = f"parsed/{year}.json 與 meta/{year}.json 的版本（sha256）不一致"
        return yd
    yd.ok, yd.meta, yd.rows, yd.first_seen = True, meta, parsed["rows"], fs
    return yd


# ─────────────────────────────────────────
# 列分類
# ─────────────────────────────────────────
class RowError(Exception):
    pass


@dataclass
class Case:
    year: str
    xlsx_row: Any
    seq: int
    fields: Dict[str, Any]
    flags: List[str]
    status: str                   # reviewing | effective | terminated
    closing: str
    code: str
    category: str
    receipt: Optional[date]
    key: str
    same_n: int = 1               # 同一年度檔內「18 欄完全相同」的列數（含自己）

    def get(self, col: str) -> str:
        return norm(self.fields.get(col))


def classify_row(year: str, row: Any, today: date) -> Optional[Case]:
    """回傳 Case；非轉換公司債三類回傳 None；異常拋 RowError。"""
    if not isinstance(row, dict):
        raise RowError("列不是物件")
    fields = row.get("fields")
    if not isinstance(fields, dict):
        raise RowError("缺少 fields")
    missing = [c for c in ("證券代號", "案件類別", "結案類型", "收文日期") if c not in fields]
    if missing:
        raise RowError(f"缺少欄位 {missing}")
    category = norm(fields.get("案件類別"))
    if not category.startswith(CB_PREFIX):
        return None
    code = norm(fields.get("證券代號"))
    if not code:
        raise RowError("證券代號空白")
    closing = norm(fields.get("結案類型"))
    flags = row.get("flags") if isinstance(row.get("flags"), list) else []
    seq = row.get("dup_seq") if isinstance(row.get("dup_seq"), int) else 1
    receipt, _ = single_date(fields.get("收文日期"))
    c = Case(year, row.get("row"), seq, fields, [str(f) for f in flags], "", closing, code, category, receipt,
             f"{year}|{code}|{category}|{norm(fields.get('收文日期'))}|{seq}")
    if closing == "":
        c.status = "reviewing"
    elif closing == "生效":
        c.status = "effective"
        d, why = single_date(fields.get("生效日期"))
        if d is None:
            raise RowError(f"結案類型=生效，但生效日期{why}")
        if d > today:
            raise RowError("結案類型=生效，但生效日期晚於今日")
    elif closing in TERMINATION_COLUMNS:
        c.status = "terminated"
        col = TERMINATION_COLUMNS[closing]
        d, why = single_date(fields.get(col))
        if d is None:
            raise RowError(f"結案類型={closing}，但{col}{why}")
    else:
        raise RowError(f"未知的結案類型（長度 {len(closing)}）")
    return c


def dup_note(c: Optional[Case]) -> str:
    """同案件鍵且各欄位完全相同的重複列：不合併，每一列都各自顯示，並在該列尾端註明有幾列完全相同。"""
    return f"（來源表中完全相同的列 ×{c.same_n}）" if c is not None and c.same_n >= 2 else ""


def row_label(c: Case) -> str:
    name = c.get("公司名稱") or DASH
    return f"{c.code} {name}｜{short_category(c.category)}"


def reviewing_lines(c: Case) -> List[str]:
    f = c.fields
    stop, lift, eff = c.get("停止生效日期"), c.get("解除生效日期"), c.get("生效日期")
    tags = []
    if stop and not eff:
        tags.append("停止生效")
    if "同日同類別多筆" in c.flags:
        tags.append("同日同類別多筆")
    tag_txt = "｜" + "、".join(tags) if tags else ""
    first = f"  • {row_label(c)}｜{fmt_amount(f)}{tag_txt}"
    n_fix = len(date_tokens(f.get("自動補正日期")))
    parts = [f"收文日期（送件）{fmt_date_cell(f.get('收文日期'))}", f"自動補正日期 {n_fix} 次"]
    if stop:
        parts.append(f"停止生效日期 {fmt_date_cell(f.get('停止生效日期'))}")
    if lift:
        parts.append(f"解除生效日期 {fmt_date_cell(f.get('解除生效日期'))}")
    parts.append(f"生效日期（預定）{fmt_date_cell(f.get('生效日期'))}" if eff else f"生效日期（預定）{DASH}")
    parts.append(c.get("案件性質") or DASH)
    return [first, "      " + "｜".join(parts) + dup_note(c)]


def effective_line(c: Case) -> str:
    return (f"  • {row_label(c)}｜{fmt_amount(c.fields)}｜收文日期（送件）{fmt_date_cell(c.fields.get('收文日期'))}"
            f"｜生效日期 {fmt_date_cell(c.fields.get('生效日期'))}{dup_note(c)}")


def terminated_line(c: Case) -> str:
    col = TERMINATION_COLUMNS[c.closing]
    return (f"  • {row_label(c)}｜{c.closing}｜{col} {fmt_date_cell(c.fields.get(col))}"
            f"｜收文日期（送件）{fmt_date_cell(c.fields.get('收文日期'))}{dup_note(c)}")


def _recv_key(c: Case) -> Tuple[int, int, str, int]:
    # 收文日期新到舊；無法解析的排最後；同日依證券代號、重複序號
    return (0 if c.receipt else 1, -(c.receipt.toordinal() if c.receipt else 0), c.code, c.seq)


def _cap_items(items: List[List[str]]) -> List[str]:
    out: List[str] = []
    for it in items[:MAX_LIST]:
        out.extend(it)
    if len(items) > MAX_LIST:
        out.append(f"  …另有 {len(items) - MAX_LIST} 筆未列出")
    return out


# ─────────────────────────────────────────
# diffs
# ─────────────────────────────────────────
def find_diff_file(data_dir: str, yd: YearData) -> Optional[str]:
    sha = yd.meta["sha256"]
    cands = sorted(glob.glob(os.path.join(data_dir, "diffs", f"{yd.year}_*_{sha[:8]}.json")))
    if not cands:
        return None
    hist = yd.meta.get("history")
    if isinstance(hist, list) and len(hist) >= 2 and isinstance(hist[-2], dict) and isinstance(hist[-2].get("sha8"), str):
        prev8 = hist[-2]["sha8"]
        for p in cands:
            if os.path.basename(p) == f"{yd.year}_{prev8}_{sha[:8]}.json":
                return p
    return cands[-1]


def _id_parts(i: Any) -> Optional[Tuple[str, str, str, str, str]]:
    if not isinstance(i, str) or "#" not in i:
        return None
    key, seq = i.rsplit("#", 1)
    p = key.split("|")
    return (p[0], p[1], p[2], p[3], seq) if len(p) == 4 else None


def _diff_value(col: str, v: Any) -> str:
    t = norm(v)
    if not t:
        return DASH
    return fmt_date_cell(t) if col.endswith("日期") else t


def diff_section(data_dir: str, yd: YearData, lookup: Dict[str, Case]) -> Tuple[List[str], Dict[str, int]]:
    """CB 三類限定的新增／消失／欄位變動。回傳 (行, 計數)。"""
    counts = {"added": 0, "vanished": 0, "changed": 0, "bad": 0, "dup_listed": 0}
    path = find_diff_file(data_dir, yd)
    head = f"🔄 較上次資料版本的變動（{yd.year}年檔，此版本於 {yd.first_seen} 首次看到）"
    if path is None:
        hist = yd.meta.get("history")
        if isinstance(hist, list) and len(hist) >= 2:       # 明明有前一版卻找不到比對檔：要說出來，不能當成沒有變動
            counts["bad"] = 1
            return [head, "  ⚠ 資料異常：此版本有前一版，但找不到對應的 diffs 檔"], counts
        return [], counts                                    # 首次留存：沒有「較上次的變動」可言，整段不顯示
    obj, err = _read_json(path)
    if err or not isinstance(obj, dict) or not all(isinstance(obj.get(k), list) for k in ("added", "vanished", "changed")):
        counts["bad"] = 1
        return [head, f"  ⚠ 資料異常：diffs 檔無法使用（{err or '結構不符'}）"], counts
    old8 = str(obj.get("old_sha256", ""))[:8]
    head = f"🔄 較上次資料版本的變動（{yd.year}年檔 {old8}→{yd.meta['sha256'][:8]}，此版本於 {yd.first_seen} 首次看到）"
    items: List[List[str]] = []

    def is_cb(parts: Tuple[str, ...]) -> bool:
        return norm(parts[1]).startswith(CB_PREFIX)

    for i in obj["added"]:
        p = _id_parts(i)
        if p is None:
            counts["bad"] += 1
            continue
        if not is_cb(p):
            continue
        counts["added"] += 1
        c = lookup.get(f"{yd.year}|{p[0]}|{norm(p[1])}|{p[2]}|{p[4]}")
        who = f"{c.code} {c.get('公司名稱') or DASH}｜{short_category(c.category)}｜{fmt_amount(c.fields)}" if c else f"{p[0]}｜{short_category(p[1])}"
        counts["dup_listed"] += 1 if c is not None and c.same_n >= 2 else 0
        items.append([f"  • 新增：{who}｜收文日期（送件）{fmt_date_cell(p[2])}{dup_note(c)}"])
    for i in obj["vanished"]:
        p = _id_parts(i)
        if p is None:
            counts["bad"] += 1
            continue
        if not is_cb(p):
            continue
        counts["vanished"] += 1
        items.append([f"  • 消失：{p[0]}｜{short_category(p[1])}｜收文日期（送件）{fmt_date_cell(p[2])}"])
    for ch in obj["changed"]:
        if not isinstance(ch, dict):
            counts["bad"] += 1
            continue
        p = _id_parts(ch.get("id"))
        col = ch.get("field")
        if p is None or col not in DIFF_FIELDS:
            counts["bad"] += 1
            continue
        if not is_cb(p):
            continue
        counts["changed"] += 1
        c = lookup.get(f"{yd.year}|{p[0]}|{norm(p[1])}|{p[2]}|{p[4]}")
        name = c.get("公司名稱") if c and c.get("公司名稱") else DASH
        counts["dup_listed"] += 1 if c is not None and c.same_n >= 2 else 0
        items.append([f"  • 變動：{p[0]} {name}｜{short_category(p[1])}｜收文日期（送件）{fmt_date_cell(p[2])}｜"
                      f"{col}：{_diff_value(col, ch.get('old'))} → {_diff_value(col, ch.get('new'))}{dup_note(c)}"])
    lines = [head, f"  新增 {counts['added']}、消失 {counts['vanished']}、欄位變動 {counts['changed']}"
                   + (f"（其中 {counts['dup_listed']} 筆屬於來源表中完全相同的重複列，各自獨立計入）" if counts["dup_listed"] else "")
                   + (f"（另有 {counts['bad']} 筆資料異常）" if counts["bad"] else "")]
    lines.extend(_cap_items(items))
    return lines, counts


# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────
@dataclass
class ProgressReport:
    text: str = ""
    state: str = "正常"                 # 正常 | 過舊 | 缺失（區塊警示用）
    label: str = "正常"                 # /status 用：正常 | 來源 N 天未更新 | 過舊 | 缺失（可能帶「尚無檢查紀錄／檢查紀錄異常」說明）
    reasons: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    counts: Dict[str, Any] = field(default_factory=dict)
    versions: List[str] = field(default_factory=list)


@dataclass
class CheckRecord:
    status: str = "missing"             # ok | missing | corrupt
    years: Dict[str, date] = field(default_factory=dict)
    reason: str = ""


def load_last_check(data_dir: str) -> CheckRecord:
    """讀 data/fsc/_last_check.json（每個年度的最後成功檢查日）。缺失與損毀分開回報，呼叫端不得把它們當成正常。"""
    obj, err = _read_json(os.path.join(data_dir, LAST_CHECK_NAME))
    if err == "檔案不存在":
        return CheckRecord("missing", reason=err)
    if err:
        return CheckRecord("corrupt", reason=err)
    ok = isinstance(obj, dict) and obj.get("version") == 1 and isinstance(obj.get("years"), dict) and set(obj) == {"version", "years"}
    years: Dict[str, date] = {}
    if ok:
        for k, v in obj["years"].items():
            d = parse_iso(v) if isinstance(v, str) else None
            if not (isinstance(k, str) and re.fullmatch(r"\d{3}", k) and d):
                ok = False
                break
            years[k] = d
    if not ok:
        return CheckRecord("corrupt", reason="結構不符（版本、欄位或日期格式異常）")
    return CheckRecord("ok", years)


def year_check(rec: CheckRecord, year: str, today: date) -> Tuple[str, Optional[date], str]:
    """回傳 (狀態, 最後成功檢查日, 說明)；狀態 ok | missing | corrupt。"""
    if rec.status == "corrupt":
        return "corrupt", None, f"檢查紀錄異常（{rec.reason}）"
    if rec.status == "missing":
        return "missing", None, "尚無檢查紀錄"
    d = rec.years.get(year)
    if d is None:
        return "missing", None, "尚無檢查紀錄"
    if d > today:
        return "corrupt", None, f"檢查紀錄異常（最後成功檢查日 {d} 晚於今日 {today}）"
    return "ok", d, ""


def _version_line(yd: YearData, today: date, check_txt: str) -> str:
    m = yd.meta
    age = (today - yd.first_seen).days if yd.first_seen else 0
    age_txt = "今日" if age == 0 else f"{age} 天前"
    ver = norm(m.get("file_version"))
    return (f"  {yd.year}年檔 {'v' + ver if ver else '無版本號'}｜來源更新 {norm(m.get('page_update_text')) or DASH}"
            f"｜此版本首次看到 {yd.first_seen}（{age_txt}）｜{check_txt}｜sha {str(m.get('sha256'))[:8]}｜{norm(m.get('filename_decoded')) or DASH}")


def build_report(now: Optional[datetime] = None, data_dir: Optional[str] = None) -> ProgressReport:
    data_dir = data_dir or default_data_dir()
    today = taipei_today(now)
    rep = ProgressReport()
    years = [load_year(data_dir, y) for y in FSC_YEARS]
    rec = load_last_check(data_dir)
    reasons_missing: List[str] = []
    reasons_stale: List[str] = []
    fallback_labels: List[str] = []
    version_lines: List[str] = []
    primary_source_age: Optional[int] = None
    for yd in years:
        if not yd.ok:
            reasons_missing.append(f"{yd.year}年檔：{yd.error}")
            continue
        if yd.first_seen > today:
            reasons_missing.append(f"{yd.year}年檔：此版本首次看到日期 {yd.first_seen} 晚於今日 {today}（時鐘或資料異常）")
            yd.ok = False
            continue
        src_age = (today - yd.first_seen).days
        cstate, last, why = year_check(rec, yd.year, today)
        extra_line = ""
        if cstate == "ok":
            check_txt = f"最近成功檢查 {last}"
            if (today - last).days > CHECK_STALE_DAYS:
                reasons_stale.append(f"{yd.year}年檔：申報進度資料過舊(最後成功檢查 {last}),抓取可能失敗")
            elif src_age >= SOURCE_NOTE_MIN_DAYS:
                extra_line = f"    └ 來源 {src_age} 天未更新(最近檢查 {last},正常)"
                if yd.year == FSC_YEARS[0]:
                    primary_source_age = src_age
        else:
            # 沒有可用的檢查紀錄：不得靜默當成正常；暫時沿用舊規則（只看目前年度檔、此版本首次看到日超過 7 天）
            check_txt = f"最近成功檢查 {DASH}"
            fallback_labels.append(why.split("（")[0])
            if yd.year == FSC_YEARS[0] and src_age > STALE_DAYS:
                reasons_stale.append(f"{yd.year}年檔：{why}，暫以此版本首次看到日判斷：{src_age} 天前（{yd.first_seen}），超過 {STALE_DAYS} 天，可能已不是最新")
            else:
                rep.notes.append(f"⚠️ {yd.year}年檔：{why}，暫以此版本首次看到日判斷（{src_age} 天前，未超過 {STALE_DAYS} 天）")
        version_lines.append(_version_line(yd, today, check_txt))
        if extra_line:
            version_lines.append(extra_line)
    rep.state = "缺失" if reasons_missing else ("過舊" if reasons_stale else "正常")
    rep.reasons = reasons_missing + reasons_stale
    suffix = ""
    if fallback_labels:
        suffix = "（" + "、".join(dict.fromkeys(fallback_labels)) + "，沿用舊規則）"
    if rep.state != "正常":
        rep.label = rep.state + suffix
    elif primary_source_age is not None:
        rep.label = f"來源 {primary_source_age} 天未更新" + suffix
    else:
        rep.label = "正常" + suffix

    lines: List[str] = [TITLE, "=" * 60]
    if rep.state != "正常":
        lines.append(f"⚠️ 申報進度資料缺失/過舊：目前狀態「{rep.state}」")
        lines.extend(f"  • {r}" for r in rep.reasons)
        if any(y.ok for y in years):
            lines.append("  以下只列出仍可讀取的版本內容，請當作可能過時的參考，不要當成最新狀態")
        else:
            lines.append("  沒有可用的資料版本，本次不列出任何案件；請檢查 data/fsc（archive-fsc 是否成功、bot/日報的 checkout 是否太舊）")
    good = [y for y in years if y.ok]
    rep.versions = [ln for ln in version_lines if not ln.startswith("    └")]
    if good:
        lines.append("📦 資料版本（本報告實際使用）：")
        lines.extend(version_lines)
        lines.extend(f"  {n}" for n in rep.notes)

    cases: List[Case] = []
    anomalies: List[str] = []
    lookup: Dict[str, Case] = {}
    for yd in good:
        for idx, row in enumerate(yd.rows):
            try:
                c = classify_row(yd.year, row, today)
            except RowError as e:
                xr = row.get("row") if isinstance(row, dict) else None
                anomalies.append(f"  • 資料異常：{yd.year}年檔 xlsx 第 {xr if xr is not None else '?'} 列（陣列位置 {idx}）— {e}")
                continue
            except Exception as e:  # 任何未預期的內容問題都只影響該列
                anomalies.append(f"  • 資料異常：{yd.year}年檔 陣列位置 {idx} — 解析例外 {type(e).__name__}")
                continue
            if c is not None:
                cases.append(c)
                lookup[c.key] = c
    same = collections.Counter((c.year, tuple(str(c.fields.get(col, "")) for col in EXPECTED_COLUMNS)) for c in cases)
    for c in cases:
        c.same_n = same[(c.year, tuple(str(c.fields.get(col, "")) for col in EXPECTED_COLUMNS))]

    reviewing = sorted((c for c in cases if c.status == "reviewing"), key=_recv_key)
    lo = today - timedelta(days=RECENT_DAYS)
    eff_recent = sorted((c for c in cases if c.status == "effective" and lo <= single_date(c.fields.get("生效日期"))[0] <= today),
                        key=lambda c: (-single_date(c.fields.get("生效日期"))[0].toordinal(), c.code, c.seq))
    term_recent = sorted((c for c in cases if c.status == "terminated"
                          and lo <= single_date(c.fields.get(TERMINATION_COLUMNS[c.closing]))[0] <= today),
                         key=lambda c: (-single_date(c.fields.get(TERMINATION_COLUMNS[c.closing]))[0].toordinal(), c.code, c.seq))
    by_cat = collections.Counter(short_category(c.category) for c in cases)
    n_eff = sum(1 for c in cases if c.status == "effective")
    n_term = sum(1 for c in cases if c.status == "terminated")
    rep.counts = {"in_scope": len(cases), "reviewing": len(reviewing), "effective_total": n_eff, "terminated_total": n_term,
                  "effective_recent": len(eff_recent), "terminated_recent": len(term_recent), "anomalies": len(anomalies),
                  "identical_rows": sum(1 for c in cases if c.same_n >= 2),
                  "by_category": dict(by_cat), "diff": {}}

    if good:
        scope_txt = "＋".join(y.year for y in good) + "年累計"
        lines.append(f"📊 範圍內 {len(cases)} 列（{scope_txt}；" + "／".join(f"{k} {v}" for k, v in sorted(by_cat.items())) + "）："
                     f"審理中 {len(reviewing)}、已生效 {n_eff}、已終止 {n_term}、資料異常 {len(anomalies)}")
        lines.append("")
        lines.append(f"⏳ 審理中 {len(reviewing)} 筆（依收文日期新到舊）")
        lines.extend(_cap_items([reviewing_lines(c) for c in reviewing]) if reviewing else ["  （無）"])
        lines.append("")
        lines.append(f"✅ 近期已生效（近 {RECENT_DAYS} 天，{lo} ～ {today}）{len(eff_recent)} 筆")
        lines.extend(_cap_items([[effective_line(c)] for c in eff_recent]) if eff_recent else ["  （無）"])
        lines.append("")
        lines.append(f"🛑 近期終止（撤回／退件／廢止/撤銷，近 {RECENT_DAYS} 天）{len(term_recent)} 筆")
        lines.extend(_cap_items([[terminated_line(c)] for c in term_recent]) if term_recent else ["  （無）"])
        for yd in good:
            if (today - yd.first_seen).days <= DIFF_DAYS:
                dl, dc = diff_section(data_dir, yd, lookup)
                if not dl:                 # 首次留存（沒有前一版）：不顯示這一段
                    continue
                rep.counts["diff"][yd.year] = dc
                lines.append("")
                lines.extend(dl)
        if anomalies:
            lines.append("")
            lines.append(f"⚠ 資料異常 {len(anomalies)} 列（該列未納入上列統計，請以原表為準）")
            lines.extend(anomalies[:MAX_LIST])
            if len(anomalies) > MAX_LIST:
                lines.append(f"  …另有 {len(anomalies) - MAX_LIST} 列未列出")
    lines.append("")
    lines.append(FOOTER)
    rep.text = "\n".join(lines)
    return rep


def build_progress_block(now: Optional[datetime] = None, data_dir: Optional[str] = None) -> str:
    """給報告用：絕不拋例外。任何失敗都變成區塊內明確的說明。"""
    try:
        return build_report(now, data_dir).text
    except Exception as e:
        return "\n".join([TITLE, "=" * 60,
                          f"⚠️ 申報進度資料缺失/過舊：區塊產生失敗（{type(e).__name__}）。請檢查 data/fsc 與 fsc_progress.py",
                          FOOTER])


def status_line(now: Optional[datetime] = None, data_dir: Optional[str] = None) -> str:
    """給 /status：一行資料版本與狀態（正常／來源 N 天未更新／過舊／缺失）。絕不拋例外。"""
    try:
        rep = build_report(now, data_dir)
        ver = rep.versions[0].strip() if rep.versions else "（無可用版本）"
        extra = f"｜{rep.reasons[0]}" if rep.reasons else ""
        return f"{rep.label}｜{ver}{extra}"
    except Exception as e:
        return f"缺失｜讀取失敗（{type(e).__name__}）"


def extract_progress_block(full_report: str) -> str:
    """從完整報告取出申報進度區塊（標題行到來源註記行）。找不到回空字串；有標題但缺註記行時取到下一個空白段落分隔的「其他公告」標題前。"""
    if not full_report:
        return ""
    start = full_report.find(TITLE)
    if start == -1:
        return ""
    end = full_report.find(FOOTER, start)
    if end != -1:
        return full_report[start:end + len(FOOTER)].strip()
    stop = full_report.find("\n\n📋 其他公告", start)
    return full_report[start:stop if stop != -1 else len(full_report)].strip()


def append_progress_block(message: str, full_report: str) -> str:
    """沒有轉換公司債公告的日子：在「今日無轉換公司債相關公告」訊息後面接上申報進度區塊（報告裡有的話）。
    訊息本身（含供 _is_empty_result 判斷的那句話）原樣保留。"""
    block = extract_progress_block(full_report)
    return f"{message}\n\n{block}" if block else message
