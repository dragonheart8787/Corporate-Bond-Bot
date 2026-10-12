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

環境變數：PROBE_FSC_PAGE_URL（覆寫頁面網址，仍須通過白名單）、PROBE_DRY_RUN=1、PROBE_REPO。
結束碼：0 全部成功；1 至少一項失敗；2 設定不合法。
"""

from __future__ import annotations

import collections
import os
import re
import sys
import time
import unicodedata
import urllib.parse
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

import fsc_sources as fs
import probe_sources as ps

LABELS = ("115年度申報案件", "114年度申報案件")
CATEGORY_COLUMNS = ("案件類別", "結案類型", "公司型態", "案件性質")
MAX_DISTINCT_LISTED = 40
MAX_LABELS_SHOWN = 12
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
