#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日留存「可轉債相關」重大訊息的完整內文（只留存，不解析、不接進任何報告）。

來源：證交所、櫃買中心的「每日重大訊息」OpenAPI（2026-10 探測確認：有「說明」內文欄位，
但每次只回傳最新一個發言日，所以必須每天抓、冪等地累積，錯過的日子無法回頭補）。

目的：累積幾週的真實內文，離線評估內文是否真的含送件、申報生效、競拍日期，再決定要不要做解析。

連線安全規則沿用 probe_sources.py：只允許 https、白名單主機、不跟隨轉址、每來源只請求一次、
來源之間至少間隔 6 秒、User-Agent 標示專案名稱。

輸出（預設 data/announcements/，可用 ARCHIVE_DIR 改）：
  YYYYMMDD.json   該發言日、篩選後的記錄（原始欄位值，不改寫；欄位名統一，見 FIELD_MAPS）
  _index.json     每日統計（只有數字）：總筆數、篩選後筆數、含「送件」「申報生效」「競價拍賣」「董事會」的筆數
冪等：同一天重跑，檔案內容逐位元相同（不寫任何時間戳；以 發言日期+發言時間+公司代號+主旨 去重）。

fail-closed：
  - API 回空陣列、不是陣列、缺必要欄位、發言日期無法解析 → 該來源整個不寫入，明確 ::error::，整個程式以非 0 結束。
  - 某來源失敗不影響另一個來源已成功的部分（會寫入），但結束碼仍是 1，讓 run 變紅。
  - 既有的日檔或索引檔損毀 → 不覆蓋、明確報錯。

環境變數：
  ARCHIVE_DIR          輸出目錄（預設 data/announcements）
  ARCHIVE_TWSE_URL / ARCHIVE_TPEX_URL   覆寫來源網址（仍須通過白名單）
  ARCHIVE_DRY_RUN=1    只驗證設定、印出將送出的請求，不連線、不寫檔
  PROBE_REPO           User-Agent 用（owner/repo）
結束碼：0 全部成功；1 至少一個來源或寫入失敗；2 設定不合法（未送出請求）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import probe_sources as ps   # 同一個目錄：沿用連線安全規則與日期解析

OUT_FIELDS = ["出表日期", "發言日期", "發言時間", "公司代號", "公司名稱", "主旨", "符合條款", "事實發生日", "說明"]
# 兩個來源欄位名稱不同：統一成同一組欄位名（值不改寫）。證交所的「主旨」欄名後面帶一個空白，一併去掉。
FIELD_MAPS: Dict[str, Dict[str, str]] = {
    "twse": {},
    "tpex": {"Date": "出表日期", "SecuritiesCompanyCode": "公司代號", "CompanyName": "公司名稱"},
}
FILTER_KEYWORDS = ("轉換公司債", "可轉換", "可轉債", "募集與發行", "公司債")
INDEX_KEYWORDS = ("送件", "申報生效", "競價拍賣", "董事會")
STORE_VERSION = 1
INDEX_NAME = "_index.json"


def log(msg: str) -> None:
    print(msg, flush=True)


class ArchiveError(Exception):
    """該來源（或檔案）無法安全處理。訊息會原樣變成 ::error::。"""


# ─────────────────────────────────────────
# 解析與驗證
# ─────────────────────────────────────────
def record_key(rec: Dict[str, str]) -> Tuple[str, str, str, str]:
    return (rec["發言日期"].strip(), rec["發言時間"].strip(), rec["公司代號"].strip(), rec["主旨"].strip())


def parse_records(source: str, body: bytes) -> List[Dict[str, str]]:
    """API 回應 → 統一欄位的記錄。任何結構問題都拋 ArchiveError（整個來源不寫入）。"""
    try:
        data = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise ArchiveError(f"{source}：回應不是有效的 JSON") from None
    if not isinstance(data, list):
        raise ArchiveError(f"{source}：回應不是陣列（結構異常，API 可能改版）")
    if not data:
        raise ArchiveError(f"{source}：API 回傳空陣列。重大訊息每個發言日都應有資料，空陣列視為異常，不寫入空檔")
    mapping = FIELD_MAPS[source]
    out: List[Dict[str, str]] = []
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ArchiveError(f"{source}：第 {i + 1} 筆不是物件（結構異常）")
        norm: Dict[str, Any] = {}
        for k, v in raw.items():
            k2 = str(k).strip()
            norm[mapping.get(k2, k2)] = v
        missing = [f for f in OUT_FIELDS if f not in norm]
        if missing:
            raise ArchiveError(f"{source}：第 {i + 1} 筆缺少欄位 {missing}（API 欄位可能改版）")
        rec = {f: ("" if norm[f] is None else str(norm[f])) for f in OUT_FIELDS}
        if not ps.parse_full_date(rec["發言日期"]):
            raise ArchiveError(f"{source}：第 {i + 1} 筆的發言日期無法解析（{len(rec['發言日期'])} 字元），不寫入")
        rec["來源"] = source
        out.append(rec)
    return out


def day_of(rec: Dict[str, str]) -> str:
    iso = ps.parse_full_date(rec["發言日期"]) or ""
    return iso.replace("-", "")


def is_cb_related(rec: Dict[str, str]) -> bool:
    text = rec["主旨"] + "\n" + rec["說明"]
    return any(k in text for k in FILTER_KEYWORDS)


def keyword_counts(records: List[Dict[str, str]]) -> Dict[str, int]:
    return {k: sum(1 for r in records if k in (r["主旨"] + "\n" + r["說明"])) for k in INDEX_KEYWORDS}


def sort_records(records: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return sorted(records, key=lambda r: (r["發言時間"], r["公司代號"], r["主旨"], r["來源"]))


# ─────────────────────────────────────────
# 檔案讀寫
# ─────────────────────────────────────────
def write_json_atomic(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise ArchiveError(f"寫入 {path} 失敗：{e}") from e


def read_json(path: str, what: str) -> Optional[Any]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise ArchiveError(f"既有的{what} {path} 無法讀取（{type(e).__name__}），為避免覆蓋已留存的資料，不處理") from e


@dataclass
class DayResult:
    day: str
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    raw_by_source: Dict[str, int] = field(default_factory=dict)


def merge_day(directory: str, day: str, new_records: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], DayResult]:
    """把新記錄併進當日檔（以 發言日期+發言時間+公司代號+主旨 去重；內容不同則以新抓到的為準）。"""
    path = os.path.join(directory, f"{day}.json")
    existing = read_json(path, "日檔")
    old: List[Dict[str, str]] = []
    if existing is not None:
        if not isinstance(existing, dict) or not isinstance(existing.get("records"), list):
            raise ArchiveError(f"既有的日檔 {path} 結構不符，為避免覆蓋已留存的資料，不處理")
        old = existing["records"]
    by_key: Dict[Tuple[str, str, str, str], Dict[str, str]] = {}
    for r in old:
        by_key[record_key(r)] = r
    res = DayResult(day)
    for r in new_records:
        k = record_key(r)
        if k not in by_key:
            res.added += 1
        elif by_key[k] != r:
            res.updated += 1
        else:
            res.unchanged += 1
        by_key[k] = r
    return sort_records(list(by_key.values())), res


# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────
def build_sources(env: Dict[str, str]) -> List[Tuple[str, str]]:
    return [
        ("twse", (env.get("ARCHIVE_TWSE_URL") or ps.DEFAULT_TWSE_URL).strip()),
        ("tpex", (env.get("ARCHIVE_TPEX_URL") or ps.DEFAULT_TPEX_URL).strip()),
    ]


def run(
    env: Optional[Dict[str, str]] = None,
    *,
    opener: Optional[Any] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    env = dict(os.environ if env is None else env)
    directory = env.get("ARCHIVE_DIR") or os.path.join("data", "announcements")
    sources = build_sources(env)
    invalid = False
    for n, u in sources:
        ok, why = ps.validate_url(u)
        if not ok:
            invalid = True
            log(f"::error title=留存設定不合法::{n}：{why}")
    if invalid:
        log("未送出任何請求。")
        return 2

    ua = ps.user_agent(env.get("PROBE_REPO", ""), tool="archive", purpose="daily archive of public announcements")
    if env.get("ARCHIVE_DRY_RUN", "") == "1":
        log("🧪 DRY-RUN：不連線、不寫檔。將依序對下列來源各送一次 GET（間隔 %.0f 秒）：" % ps.INTERVAL_SEC)
        for n, u in sources:
            log(f"  - {n}: {u}")
        log(f"輸出目錄：{directory}")
        log(f"User-Agent：{ua}")
        return 0

    failed = 0
    fetched: Dict[str, List[Dict[str, str]]] = {}
    for i, (name, url) in enumerate(sources):
        if i > 0:
            sleep(max(ps.INTERVAL_SEC, ps.MIN_INTERVAL_SEC))
        fr = ps.fetch_once(url, ua, opener)
        log(f"== [{name}] HTTP {fr.status if fr.status is not None else '無回應'}｜{fr.size} bytes")
        if fr.error:
            log(f"::error title=留存失敗 {name}::{fr.error}")
            failed += 1
            continue
        if fr.truncated:
            log(f"::error title=留存失敗 {name}::回應超過大小上限，被截斷，不寫入")
            failed += 1
            continue
        try:
            fetched[name] = parse_records(name, fr.body)
        except ArchiveError as e:
            log(f"::error title=留存失敗 {name}::{e}")
            failed += 1
            continue
        log(f"  原始 {len(fetched[name])} 筆")

    if not fetched:
        log("沒有任何來源可寫入。")
        return 1

    # 依發言日分組（API 只回傳最新一個發言日，但不假設只有一天）
    raw_count: Dict[str, Dict[str, int]] = {}
    kept: Dict[str, List[Dict[str, str]]] = {}
    for name, recs in fetched.items():
        for r in recs:
            d = day_of(r)
            raw_count.setdefault(d, {}).setdefault(name, 0)
            raw_count[d][name] += 1
            if is_cb_related(r):
                kept.setdefault(d, []).append(r)

    index_path = os.path.join(directory, INDEX_NAME)
    try:
        index = read_json(index_path, "索引檔")
        if index is None:
            index = {"version": STORE_VERSION, "days": {}}
        if not isinstance(index, dict) or not isinstance(index.get("days"), dict):
            raise ArchiveError(f"既有的索引檔 {index_path} 結構不符，為避免覆蓋，不處理")
    except ArchiveError as e:
        log(f"::error title=留存失敗 索引檔::{e}")
        return 1

    for day in sorted(raw_count):
        try:
            merged = None
            res = DayResult(day)
            if day in kept or os.path.exists(os.path.join(directory, f"{day}.json")):
                merged, res = merge_day(directory, day, kept.get(day, []))
            else:
                merged = []
            if merged:
                write_json_atomic(os.path.join(directory, f"{day}.json"), {"version": STORE_VERSION, "date": day, "records": merged})
            # 索引：總筆數取「各來源歷次回應中該日的最大值」，同日重跑不會變小；其餘由日檔內容算出（冪等）
            entry = index["days"].get(day, {})
            totals = dict(entry.get("total_by_source", {}))
            for src, n in raw_count[day].items():
                totals[src] = max(int(totals.get(src, 0)), n)
            by_src = {s: sum(1 for r in merged if r["來源"] == s) for s in sorted({r["來源"] for r in merged})}
            index["days"][day] = {
                "total": sum(totals.values()),
                "total_by_source": dict(sorted(totals.items())),
                "filtered": len(merged),
                "filtered_by_source": by_src,
                "keywords_in_filtered": keyword_counts(merged),
            }
            log(f"  {day}：原始 {sum(raw_count[day].values())} 筆，可轉債相關 {len(merged)} 筆"
                f"（新增 {res.added}、更新 {res.updated}、未變 {res.unchanged}）")
        except ArchiveError as e:
            log(f"::error title=留存失敗 {day}::{e}")
            failed += 1
    try:
        write_json_atomic(index_path, index)
    except ArchiveError as e:
        log(f"::error title=留存失敗 索引檔::{e}")
        failed += 1

    summary = env.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("### 重大訊息留存（只有數字）\n| 發言日 | 原始 | 可轉債相關 | 送件 | 申報生效 | 競價拍賣 | 董事會 |\n|---|---|---|---|---|---|---|\n")
                for d in sorted(raw_count):
                    e = index["days"].get(d, {})
                    kw = e.get("keywords_in_filtered", {})
                    f.write(f"| {d} | {e.get('total', 0)} | {e.get('filtered', 0)} | {kw.get('送件', 0)} | {kw.get('申報生效', 0)} | {kw.get('競價拍賣', 0)} | {kw.get('董事會', 0)} |\n")
        except OSError as e:
            log(f"::warning title=無法寫入執行摘要::{e}")
    log(f"留存完成：{'有失敗，請看上方 ::error::' if failed else '全部成功'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
