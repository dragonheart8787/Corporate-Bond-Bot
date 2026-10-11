#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
唯讀探測：公開資料來源的「結構」（只用標準函式庫）。

目的：確認有內文的公開資料來源能不能取得（送件日、申報生效日、競拍日），所以只回答結構性問題——
  HTTP 狀態碼、回應大小、欄位名稱、筆數、日期欄位的最早／最晚、是否看得到「送件」「申報生效」字樣的筆數。

硬性規則：
  - 每個來源只送一次 GET，不重試、不迴圈；來源之間至少間隔 6 秒。
  - 不跟隨轉址（3xx 直接回報狀態與目的主機，不替你再打一次）。
  - 只允許 https 與白名單主機。
  - 絕不輸出完整內文：除了欄位名稱與日期的最早／最晚，不印任何欄位值。
  - 不寫檔、不使用任何密鑰；只有 $GITHUB_STEP_SUMMARY（執行頁面的摘要，不是 repo）會被寫入同樣的結構性資訊。
  - 失敗（連線被拒、403、逾時、轉址、非預期格式）一律明確輸出原因，並讓整個程式以非 0 結束，不吞掉。

環境變數：
  PROBE_TWSE_URL     證交所 OpenAPI「上市公司每日重大訊息」
  PROBE_TPEX_URL     櫃買中心 OpenAPI 的對應資料集
  PROBE_DATAGOV_IDS  政府資料開放平臺資料集編號，逗號分隔（預設 11406,11407）
  PROBE_DRY_RUN=1    只驗證設定並印出將送出的請求，不實際連線
  PROBE_REPO         用於 User-Agent（owner/repo）
結束碼：0 全部成功；1 至少一個來源失敗；2 設定不合法（未送出任何請求）。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_TWSE_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
DEFAULT_TPEX_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"
DEFAULT_DATAGOV_IDS = "11406,11407"
DATAGOV_URL_TMPL = "https://data.gov.tw/api/v2/rest/dataset/{id}"

ALLOWED_HOSTS = {
    "openapi.twse.com.tw",
    "www.twse.com.tw",
    "openapi.tpex.org.tw",
    "www.tpex.org.tw",
    "data.gov.tw",
    "mops.twse.com.tw",
}
MIN_INTERVAL_SEC = 6.0
INTERVAL_SEC = 7.0          # 實際使用的間隔（> MIN_INTERVAL_SEC，留餘裕）
TIMEOUT_SEC = 30.0
MAX_BYTES = 30 * 1024 * 1024
KEYWORDS = ("送件", "申報生效")
MAX_FIELDS_SHOWN = 200
MAX_RECORDS_SCANNED = 20000


def log(msg: str) -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────
# 設定與驗證
# ─────────────────────────────────────────
@dataclass
class Source:
    name: str
    url: str


def validate_url(url: str) -> Tuple[bool, str]:
    """只接受 https、白名單主機、無帳密、無自訂埠。回傳 (合法?, 原因)。"""
    try:
        u = urllib.parse.urlparse(url or "")
    except ValueError:
        return False, "網址無法解析"
    if u.scheme != "https":
        return False, "只允許 https"
    if u.username or u.password:
        return False, "網址不得含帳號密碼"
    if u.port not in (None, 443):
        return False, "不允許自訂連接埠"
    if (u.hostname or "").lower() not in ALLOWED_HOSTS:
        return False, f"主機 {u.hostname!r} 不在白名單"
    return True, ""


def build_sources(env: Dict[str, str]) -> List[Source]:
    sources = [
        Source("twse", (env.get("PROBE_TWSE_URL") or DEFAULT_TWSE_URL).strip()),
        Source("tpex", (env.get("PROBE_TPEX_URL") or DEFAULT_TPEX_URL).strip()),
    ]
    ids = [x.strip() for x in (env.get("PROBE_DATAGOV_IDS") or DEFAULT_DATAGOV_IDS).split(",") if x.strip()]
    for i in ids:
        if not re.fullmatch(r"\d{1,8}", i):
            raise ValueError(f"data.gov.tw 資料集編號只能是數字：{i!r}")
        sources.append(Source(f"datagov-{i}", DATAGOV_URL_TMPL.format(id=i)))
    return sources


# ─────────────────────────────────────────
# 單次請求（不重試、不跟隨轉址）
# ─────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None   # 讓 3xx 變成 HTTPError，由 fetch_once 回報，不自動再打一次


@dataclass
class FetchResult:
    status: Optional[int] = None
    content_type: str = ""
    size: int = 0
    body: bytes = b""
    truncated: bool = False
    error: str = ""
    location_host: str = ""


def default_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect())


def user_agent(repo: str) -> str:
    base = f"https://github.com/{repo}" if repo else "https://github.com/dragonheart8787/Corporate-Bond-Bot"
    return f"Corporate-Bond-Bot-probe/1.0 (+{base}; read-only schema check, 1 request per source)"


def _hint(status: int) -> str:
    if status == 403:
        return "（被拒絕：可能是來源擋雲端／資料中心 IP，或需要特定標頭）"
    if status == 404:
        return "（找不到：資料集路徑可能不對，請確認後用 workflow 輸入覆寫網址）"
    if status == 429:
        return "（請求過於頻繁）"
    if 300 <= status < 400:
        return "（被轉址；為遵守「每來源一次請求」，不會跟隨）"
    if status >= 500:
        return "（來源伺服器錯誤）"
    return ""


def fetch_once(url: str, ua: str, opener: Optional[Any] = None) -> FetchResult:
    opener = opener or default_opener()
    req = urllib.request.Request(url, method="GET", headers={"User-Agent": ua, "Accept": "application/json, text/csv;q=0.9, */*;q=0.5"})
    res = FetchResult()
    try:
        with opener.open(req, timeout=TIMEOUT_SEC) as resp:
            res.status = int(resp.status)
            res.content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            data = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        res.status = e.code
        res.content_type = (e.headers.get("Content-Type") or "").split(";")[0].strip().lower() if e.headers else ""
        loc = e.headers.get("Location") if e.headers else None
        if loc:
            res.location_host = (urllib.parse.urlparse(loc).hostname or "（相對路徑）")
        res.error = f"HTTP {e.code} {_hint(e.code)}".strip()
        return res
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        res.error = f"連線失敗：{type(reason).__name__}: {reason}"
        return res
    except TimeoutError:
        res.error = f"逾時（{int(TIMEOUT_SEC)} 秒）"
        return res
    except OSError as e:
        res.error = f"連線失敗：{type(e).__name__}: {e}"
        return res
    if len(data) > MAX_BYTES:
        res.truncated = True
        data = data[:MAX_BYTES]
    res.body = data
    res.size = len(data)
    if not 200 <= (res.status or 0) < 300:
        res.error = f"HTTP {res.status} {_hint(res.status or 0)}".strip()
    return res


# ─────────────────────────────────────────
# 結構分析（絕不回傳欄位值，除了日期的最早／最晚）
# ─────────────────────────────────────────
@dataclass
class Analysis:
    kind: str = "unknown"                       # json | csv | html | other | empty
    rows: int = 0
    rows_source: str = ""                       # 筆數是從哪一層算的
    fields: List[str] = field(default_factory=list)
    date_ranges: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    keyword_counts: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


_ISO = re.compile(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T].*)?$")
_ROC_SLASH = re.compile(r"^(\d{2,3})[/.\-](\d{1,2})[/.\-](\d{1,2})$")
_CJK = re.compile(r"^(\d{2,4})年(\d{1,2})月(\d{1,2})日?$")


def _mk(y: int, m: int, d: int) -> Optional[str]:
    if 100 <= y <= 199:
        y += 1911
    if not 1990 <= y <= 2100:
        return None
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_full_date(value: Any) -> Optional[str]:
    """整個值就是一個日期才算（避免把一般數字或內文當日期）。支援西元、民國、有無分隔符。"""
    if isinstance(value, bool) or value is None:
        return None
    s = str(value).strip()
    if not s or len(s) > 40:
        return None
    m = _ISO.match(s)
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _CJK.match(s)
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _ROC_SLASH.match(s)
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if re.fullmatch(r"\d{8}", s):
        return _mk(int(s[:4]), int(s[4:6]), int(s[6:]))
    if re.fullmatch(r"\d{7}", s):
        return _mk(int(s[:3]), int(s[3:5]), int(s[5:]))
    return None


def _flatten(obj: Any, prefix: str, out: Dict[str, List[Any]], depth: int = 0) -> None:
    """把巢狀物件攤平成 {欄位路徑: [值…]}；陣列以 [] 表示。只用來統計，值不會被輸出。"""
    if depth > 4:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:200]:
            _flatten(v, f"{prefix}[]", out, depth + 1)
        if not obj:
            out.setdefault(f"{prefix}[]", [])
    else:
        out.setdefault(prefix, []).append(obj)


def _find_records(obj: Any) -> Tuple[List[Any], str]:
    if isinstance(obj, list):
        return obj, "最上層陣列"
    if isinstance(obj, dict):
        best: Tuple[List[Any], str] = ([], "")

        def walk(o: Any, path: str, depth: int) -> None:
            nonlocal best
            if depth > 3:
                return
            if isinstance(o, list) and o and all(isinstance(x, dict) for x in o) and len(o) > len(best[0]):
                best = (o, path or "（根）")
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, f"{path}.{k}" if path else str(k), depth + 1)

        walk(obj, "", 0)
        if best[0]:
            return best
        return [obj], "單一物件"
    return [], ""


def analyze(body: bytes, content_type: str) -> Analysis:
    a = Analysis()
    if not body:
        a.kind = "empty"
        a.notes.append("回應沒有內容")
        return a
    head = body[:512].lstrip().lower()
    text: Optional[str] = None
    for enc in ("utf-8-sig", "big5"):
        try:
            text = body.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        a.kind = "other"
        a.notes.append("無法解碼為文字（既非 UTF-8 也非 Big5）")
        return a

    records: List[Any]
    if head.startswith((b"<!doctype html", b"<html")) or "html" in content_type:
        a.kind = "html"
        a.notes.append("回應是 HTML 網頁而不是資料（可能是登入頁、阻擋頁或說明頁）")
        return a
    if head[:1] in (b"{", b"[") or "json" in content_type:
        try:
            parsed = json.loads(text)
        except ValueError:
            a.kind = "other"
            a.notes.append("宣稱是 JSON 但解析失敗")
            return a
        a.kind = "json"
        records, a.rows_source = _find_records(parsed)
    elif "csv" in content_type or ("," in text.split("\n", 1)[0] and "\n" in text):
        a.kind = "csv"
        try:
            records = list(csv.DictReader(io.StringIO(text)))
        except csv.Error:
            a.kind = "other"
            a.notes.append("CSV 解析失敗")
            return a
        a.rows_source = "CSV 資料列"
    else:
        a.kind = "other"
        a.notes.append(f"無法辨識的格式（content-type={content_type or '未提供'}）")
        return a

    a.rows = len(records)
    scanned = records[:MAX_RECORDS_SCANNED]
    if a.rows > len(scanned):
        a.notes.append(f"只掃描前 {len(scanned)} 筆的欄位與日期統計")
    flat: Dict[str, List[Any]] = {}
    kw = {k: 0 for k in KEYWORDS}
    for rec in scanned:
        per: Dict[str, List[Any]] = {}
        _flatten(rec, "", per)
        for k, vals in per.items():
            flat.setdefault(k, []).extend(vals)
        strings = [v for vals in per.values() for v in vals if isinstance(v, str)]
        for k in KEYWORDS:
            if any(k in s for s in strings):
                kw[k] += 1
    a.keyword_counts = kw
    names = [n for n in flat.keys() if n]
    a.fields = names[:MAX_FIELDS_SHOWN]
    if len(names) > MAX_FIELDS_SHOWN:
        a.notes.append(f"欄位過多，只列前 {MAX_FIELDS_SHOWN} 個（共 {len(names)}）")
    for name, vals in flat.items():
        non_empty = [v for v in vals if v not in (None, "")]
        if len(non_empty) < 1:
            continue
        parsed_dates = [d for d in (parse_full_date(v) for v in non_empty) if d]
        if len(parsed_dates) >= max(1, int(len(non_empty) * 0.8)):
            a.date_ranges[name] = (min(parsed_dates), max(parsed_dates))
    return a


# ─────────────────────────────────────────
# 輸出（結構性資訊限定）
# ─────────────────────────────────────────
def render(src: Source, fr: FetchResult, an: Optional[Analysis]) -> Tuple[List[str], List[str], bool]:
    """回傳 (終端機行, 摘要表格行, 是否成功)。"""
    lines = [f"== [{src.name}] {src.url}"]
    status = fr.status if fr.status is not None else "無回應"
    lines.append(f"HTTP 狀態：{status}｜回應大小：{fr.size} bytes｜content-type：{fr.content_type or '（未提供）'}"
                 + ("｜已截斷至上限" if fr.truncated else ""))
    if fr.error:
        lines.append(f"結果：失敗 — {fr.error}" + (f"（轉址目的主機：{fr.location_host}）" if fr.location_host else ""))
        lines.append(f"::error title=探測失敗 {src.name}::{fr.error}")
        return lines, [f"| {src.name} | {status} | {fr.size} | 失敗：{fr.error} | | | | |"], False
    assert an is not None
    lines.append(f"格式：{an.kind}｜筆數：{an.rows}" + (f"（算自：{an.rows_source}）" if an.rows_source else ""))
    if an.kind not in ("json", "csv"):
        reason = "；".join(an.notes) or "不是結構化資料"
        lines.append(f"結果：失敗 — {reason}")
        lines.append(f"::error title=探測失敗 {src.name}::{reason}")
        return lines, [f"| {src.name} | {status} | {fr.size} | 失敗：{reason} | | | | |"], False
    lines.append(f"欄位名稱（{len(an.fields)}）：" + (", ".join(an.fields) if an.fields else "（無）"))
    if an.date_ranges:
        for name, (lo, hi) in an.date_ranges.items():
            lines.append(f"日期欄位範圍：{name}：{lo} ～ {hi}")
    else:
        lines.append("日期欄位範圍：（沒有整欄都是日期的欄位）")
    lines.append("關鍵字筆數：" + "、".join(f"含「{k}」的記錄 {v} 筆" for k, v in an.keyword_counts.items()))
    for n in an.notes:
        lines.append(f"備註：{n}")
    dr = "; ".join(f"{k}: {lo}~{hi}" for k, (lo, hi) in an.date_ranges.items()) or "—"
    row = f"| {src.name} | {status} | {fr.size} | {an.kind} | {an.rows} | {', '.join(an.fields)[:300]} | {dr} | " \
          + " / ".join(f"{k}={v}" for k, v in an.keyword_counts.items()) + " |"
    return lines, [row], True


SUMMARY_HEADER = [
    "### 唯讀探測結果（只有結構，沒有內文）",
    "| 來源 | HTTP | 大小(bytes) | 格式 | 筆數 | 欄位 | 日期範圍 | 關鍵字筆數 |",
    "|---|---|---|---|---|---|---|---|",
]


# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────
def run(
    env: Optional[Dict[str, str]] = None,
    *,
    opener: Optional[Any] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    env = dict(os.environ if env is None else env)
    try:
        sources = build_sources(env)
    except ValueError as e:
        log(f"::error title=探測設定不合法::{e}")
        return 2
    bad = [(s, validate_url(s.url)[1]) for s in sources if not validate_url(s.url)[0]]
    if bad:
        for s, why in bad:
            log(f"::error title=探測設定不合法::{s.name}：{why}")
        log("未送出任何請求。")
        return 2

    ua = user_agent(env.get("PROBE_REPO", ""))
    if env.get("PROBE_DRY_RUN", "") == "1":
        log("🧪 DRY-RUN：不連線。將依序對下列來源各送一次 GET（間隔 %.0f 秒）：" % INTERVAL_SEC)
        for s in sources:
            log(f"  - {s.name}: {s.url}")
        log(f"User-Agent：{ua}")
        return 0

    summary = list(SUMMARY_HEADER)
    failed = 0
    for i, src in enumerate(sources):
        if i > 0:
            sleep(max(INTERVAL_SEC, MIN_INTERVAL_SEC))
        fr = fetch_once(src.url, ua, opener)
        an = analyze(fr.body, fr.content_type) if (not fr.error and fr.body is not None) else None
        lines, rows, ok = render(src, fr, an)
        for ln in lines:
            log(ln)
        log("")
        summary.extend(rows)
        failed += 0 if ok else 1

    path = env.get("GITHUB_STEP_SUMMARY", "")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n".join(summary) + "\n")
        except OSError as e:
            log(f"::warning title=無法寫入執行摘要::{e}")
    log(f"探測完成：{len(sources) - failed}/{len(sources)} 個來源成功。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
