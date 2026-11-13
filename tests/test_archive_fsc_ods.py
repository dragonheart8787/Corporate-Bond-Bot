"""archive_fsc 的 ODS 退路與格式切換閘門的測試。全部離線：xlsx／ods／HTML 都是合成樣本（虛構內容）、假連線、暫存目錄。"""

import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import archive_fsc as af  # noqa: E402
import fsc_sources as fs  # noqa: E402
from test_archive_fsc import (  # noqa: E402
    TAIPEI, TRAILER, Base, TestCommitStepActuallyRuns as _CommitBase, base_rows, http_error, xlsx,
)
from test_fsc_probe import HEADER, PAGE_URL, URL114, FakeResp, RoutedOpener, data_row, page_html  # noqa: E402
from test_probe_fsc_formats import OLE2, make_ods  # noqa: E402

_real_socket = socket.socket


def setUpModule():
    def blocked(*a, **k):
        raise AssertionError("測試不得建立真的網路連線")
    socket.socket = blocked  # type: ignore[assignment]


def tearDownModule():
    socket.socket = _real_socket  # type: ignore[assignment]


U_ODS = "https://www.fsc.gov.tw/userfiles/file/1151112v1.ods"
U_XLS = "https://www.fsc.gov.tw/userfiles/file/1151112v1.xls"
U_XLSX = "https://www.fsc.gov.tw/userfiles/file/1151112v1.xlsx"
NOV = datetime(2026, 11, 13, 9, 0, tzinfo=TAIPEI)
X114 = xlsx(base_rows()[:3])

_ODS_CACHE = {}


def ods_bytes(rows, *, trailer=TRAILER, sheet="IMP0080A_Upload", header=HEADER, sheets=None, row_repeats=None):
    """同樣的參數一定回傳同樣的位元組（zipfile 會寫入當下時間）。內容與 xlsx(rows) 的合成表格相同：整數 → 數字型、字串 → 字串型。"""
    key = repr((rows, trailer, sheet, header, sheets, row_repeats))
    if key not in _ODS_CACHE:
        body = [["115年度申報案件辦理情形彙總表"] + [None] * 17, [None], header] + rows + trailer
        _ODS_CACHE[key] = make_ods(sheets or [(sheet, body)], row_repeats=row_repeats)
    return _ODS_CACHE[key]


def page_bytes(links115, text="2026-11-12", links114=None):
    html = page_html([("114年度申報案件", links114 or [(URL114, "EXCEL")]), ("115年度申報案件", links115)])
    return html.replace("2026-10-08", text).encode("utf-8")


ODS_ONLY = [(U_XLS, "EXCEL"), (U_ODS, "ODS")]


def ods_routes(body, text="2026-11-12", links115=None, x114=None, ods_url=U_ODS):
    return {
        PAGE_URL: FakeResp(page_bytes(links115 or ODS_ONLY, text)),
        ods_url: FakeResp(body, ctype="application/vnd.oasis.opendocument.spreadsheet"),
        U_XLS: FakeResp(OLE2, ctype="application/vnd.ms-excel"),
        URL114: FakeResp(x114 if x114 is not None else X114, ctype="application/octet-stream"),
    }


def xlsx_routes(rows, text="2026-10-08"):
    from test_archive_fsc import routes
    return routes(x115=xlsx(rows), page=page_bytes([("https://www.fsc.gov.tw/userfiles/file/1151008v3.xlsx", "EXCEL")], text))


def many_rows(n, base=1000, receipt_base=1151101, spread=10):
    return [data_row(code=str(base + i), category="現金增資", closing="已生效", amount=1000 + i, received=receipt_base + (i % spread)) for i in range(n)]


def strip_sha(text):
    return re.sub(r' "sha256": "[0-9a-f]{64}",\n', "", text)


class OdsBase(Base):
    def go(self, rts, clock=None):
        return self.run_archive(rts, clock=clock or NOV)

    def seed_xlsx(self, rows, text="2026-10-08"):
        code, out, *_ = self.run_archive(xlsx_routes(rows, text), clock=datetime(2026, 10, 12, 9, 0, tzinfo=TAIPEI))
        self.assertEqual(code, 0, out)

    def meta(self, y="115"):
        return self.load(f"meta/{y}.json")

    def raw_files(self, y="115"):
        d = os.path.join(self.dir, "raw")
        return sorted(f for f in os.listdir(d) if f.startswith(y + "_")) if os.path.isdir(d) else []

    def tree115(self):
        return {k: v for k, v in self.tree().items() if "115" in k and not k.startswith("_")}


# ─────────────────────────────────────────
class TestSelectionInArchive(OdsBase):
    def test_ods_used_when_115_has_only_xls_and_ods(self):
        code, out, op, sleeps = self.go(ods_routes(ods_bytes(base_rows())))
        self.assertEqual(code, 0, out)
        self.assertEqual([u for u, _, _ in op.requests], [PAGE_URL, U_ODS, URL114])     # .xls 從未被請求
        self.assertEqual(len(sleeps), 2)
        m = self.meta()
        self.assertEqual((m["source_format"], m["source_ext"], m["filename_decoded"], m["filename_safe"]), ("ods", ".ods", "1151112v1.ods", "1151112v1.ods"))
        self.assertEqual(self.meta("114")["source_format"], "xlsx")
        self.assertEqual(m["history"][-1]["format"], "ods")
        self.assertEqual(self.raw_files(), [f"115_{m['sha256'][:8]}_1151112v1.ods"])
        with open(os.path.join(self.dir, "raw", self.raw_files()[0]), "rb") as f:
            self.assertEqual(f.read(), ods_bytes(base_rows()))                 # 原檔逐位元留存
        self.assertEqual(len(self.parsed()["rows"]), 4)
        self.assertIn("來源格式：ods", out)
        self.assertIn("1151112v1.ods（格式 ods）", out)

    def test_xlsx_present_means_xlsx_even_if_ods_is_also_listed(self):
        rts = ods_routes(ods_bytes(base_rows()), links115=[(U_XLSX, "EXCEL"), (U_ODS, "ODS"), (U_XLS, "x")])
        rts[U_XLSX] = FakeResp(xlsx(base_rows()), ctype="application/octet-stream")
        code, out, op, _ = self.go(rts)
        self.assertEqual(code, 0, out)
        self.assertNotIn(U_ODS, [u for u, _, _ in op.requests])
        self.assertEqual(self.meta()["source_format"], "xlsx")
        self.assertEqual(self.raw_files()[0][-5:], ".xlsx")

    def test_xlsx_download_failure_never_falls_back_to_ods(self):
        for fail in (http_error(U_XLSX, 500), FakeResp(b"<html>blocked</html>"), FakeResp(OLE2), FakeResp(ods_bytes(base_rows()))):
            with self.subTest(kind=type(fail).__name__):
                shutil.rmtree(self.dir, ignore_errors=True)
                rts = ods_routes(ods_bytes(base_rows()), links115=[(U_XLSX, "EXCEL"), (U_ODS, "ODS")])
                rts[U_XLSX] = fail
                code, out, op, _ = self.go(rts)
                self.assertEqual(code, 1)
                self.assertNotIn(U_ODS, [u for u, _, _ in op.requests])
                self.assertEqual(self.raw_files(), [])
                self.assertFalse(os.path.exists(os.path.join(self.dir, "meta", "115.json")))
                self.assertTrue(os.path.exists(os.path.join(self.dir, "meta", "114.json")))   # 另一年度照常

    def test_neither_xlsx_nor_ods_is_error_and_xls_is_never_fetched(self):
        code, out, op, _ = self.go(ods_routes(b"", links115=[(U_XLS, "EXCEL")]))
        self.assertEqual(code, 1)
        self.assertIn("沒有 .xlsx 也沒有 .ods", out)
        self.assertNotIn(U_XLS, [u for u, _, _ in op.requests])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "meta", "115.json")))

    def test_two_distinct_ods_is_error_nothing_downloaded(self):
        other = "https://www.fsc.gov.tw/userfiles/file/other.ods"
        code, out, op, _ = self.go(ods_routes(ods_bytes(base_rows()), links115=[(U_ODS, "a"), (other, "b")]))
        self.assertEqual(code, 1)
        self.assertIn("2 個不同的 .ods", out)
        self.assertEqual([u for u, _, _ in op.requests], [PAGE_URL, URL114])

    def test_dry_run_mentions_ods_rule_and_sends_nothing(self):
        code, out, op, _ = self.run_archive(ods_routes(b""), env={"ARCHIVE_DRY_RUN": "1"})
        self.assertEqual(code, 0)
        self.assertEqual(op.requests, [])
        self.assertIn("沒有 .xlsx 且恰有一個 .ods 才用 .ods", out)


class TestOdsFailClosed(OdsBase):
    def fails(self, body, needle=None, **kw):
        code, out, *_ = self.go(ods_routes(body, **kw))
        self.assertEqual(code, 1, out)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "meta", "115.json")), "ods 有問題時不得寫入 meta")
        self.assertEqual(self.raw_files(), [], "ods 有問題時不得寫入原檔")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "meta", "114.json")))
        if needle:
            self.assertIn(needle, out)
        for s in ("機密公司甲", "機密承銷商乙", "88888888"):
            self.assertNotIn(s, out)
        return out

    def test_html_block_page(self):
        self.fails(b"<html><body>Access Denied</body></html>", "HTML")

    def test_biff_bytes_under_ods_name(self):
        self.fails(OLE2, "OLE2")

    def test_xlsx_bytes_under_ods_name_has_no_ods_mimetype(self):
        self.fails(xlsx(base_rows()), "mimetype")

    def test_wrong_or_missing_mimetype(self):
        self.fails(make_ods([("S", [HEADER])], mimetype="application/zip"), "mimetype")
        self.fails(make_ods([("S", [HEADER])], mimetype=None), "mimetype")

    def test_empty_body_and_truncated_zip(self):
        self.fails(b"")
        self.fails(ods_bytes(base_rows())[:200])

    def test_two_sheets(self):
        body = [["t"], [None], HEADER] + base_rows()
        self.fails(ods_bytes(None, sheets=[("A", body), ("B", body)]) if False else make_ods([("A", body), ("B", body)]), "工作表數量為 2")

    def test_header_wrong_name_and_wrong_order(self):
        bad = list(HEADER)
        bad[3] = "公司全名"
        self.fails(ods_bytes(base_rows(), header=bad), "表頭")
        swapped = list(HEADER)
        swapped[3], swapped[4] = swapped[4], swapped[3]
        self.fails(ods_bytes(base_rows(), header=swapped), "順序不同")

    def test_unsupported_cell_types(self):
        rows = base_rows()
        rows[0][9] = ("type", "date", "2026-01-09")
        self.fails(ods_bytes(rows), "date")
        rows = base_rows()
        rows[0][6] = ("type", "boolean", "true")
        self.fails(ods_bytes(rows), "boolean")

    def test_doctype_rejected(self):
        self.fails(make_ods([("S", [HEADER])], doctype=True), "DTD")

    def test_giant_repeat_rejected(self):
        self.fails(ods_bytes(base_rows(), row_repeats={5: 5000}), "重複")

    def test_blank_security_code_rejected(self):
        rows = base_rows()
        rows[1][0] = None
        rows[1][3] = "x"
        rows[1][4] = "y"
        rows[1][5] = "z"
        self.fails(ods_bytes(rows), "證券代號")

    def test_row_count_drop_20_percent_still_applies(self):
        code, out, *_ = self.go(ods_routes(ods_bytes(many_rows(50))))
        self.assertEqual(code, 0, out)
        before = self.tree115()
        u = "https://www.fsc.gov.tw/userfiles/file/1151113v2.ods"
        later = datetime(2026, 11, 14, 9, 0, tzinfo=TAIPEI)
        r40 = ods_routes(ods_bytes(many_rows(40)), text="2026-11-13", links115=[(U_XLS, "x"), (u, "ODS")], ods_url=u)
        code, out, *_ = self.go_at(r40, later)                                       # 50 → 40 = 少 20%：擋下（同格式，不經切換閘門）
        self.assertEqual(code, 1)
        self.assertIn("少 10 列", out)
        self.assertNotIn("格式切換閘門", out)
        self.assertEqual(self.tree115(), before)
        r41 = ods_routes(ods_bytes(many_rows(41)), text="2026-11-13", links115=[(U_XLS, "x"), (u, "ODS")], ods_url=u)
        code, out, *_ = self.go_at(r41, later)                                       # 少 18%：通過
        self.assertEqual(code, 0, out)

    def go_at(self, rts, when):
        return self.run_archive(rts, clock=when)


# ─────────────────────────────────────────
class TestSerializationParity(OdsBase):
    """同一份資料做成 xlsx 與 ods，經 archive_fsc 產生的 parsed JSON 必須逐位元相同（除了 head 的 sha256）。"""

    def parsed_text(self, kind, rows):
        shutil.rmtree(self.dir, ignore_errors=True)
        if kind == "xlsx":
            code, out, *_ = self.run_archive(xlsx_routes(rows, "2026-11-12"), clock=NOV)
        else:
            code, out, *_ = self.go(ods_routes(ods_bytes(rows)))
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.dir, "parsed", "115.json"), encoding="utf-8") as f:
            return f.read()

    def assert_same(self, rows):
        a, b = self.parsed_text("xlsx", rows), self.parsed_text("ods", rows)
        self.assertNotEqual(a, b)                                  # head 的 sha256 一定不同（不同檔）
        self.assertEqual(strip_sha(a), strip_sha(b))               # 其餘逐位元相同（含 numbers、flags、dates_iso、row）

    def test_ordinary_synthetic_table(self):
        self.assert_same(base_rows())

    def test_three_amount_shapes_from_the_real_data(self):
        # 整數（金額 11803517460）、小數（發行價格 5.6／33.18）、非純數字（金額「待定」）——都是 data/fsc/parsed/115.json 現有的樣態
        rows = [
            data_row(code="1111", amount=11803517460, received=1150105),
            data_row(code="2222", amount=500, received=1150106),
            data_row(code="3333", amount="待定", received=1150107),
            data_row(code="4444", amount=900, received=1150108),
        ]
        rows[1][8] = ("num", "5.6")
        rows[3][8] = ("num", "33.18")
        rows[0][8] = 80
        self.assert_same(rows)
        parsed = {r["fields"]["證券代號"]: r for r in json.loads(self.parsed_text("ods", rows))["rows"]}
        self.assertEqual(parsed["1111"]["fields"]["金額"], "11803517460")
        self.assertEqual(parsed["1111"]["numbers"], {"金額": 11803517460, "發行價格": 80})
        self.assertEqual(parsed["2222"]["fields"]["發行價格"], "5.6")
        self.assertEqual(parsed["2222"]["numbers"]["發行價格"], 5.6)
        self.assertEqual(parsed["4444"]["fields"]["發行價格"], "33.18")
        self.assertIn("金額非純數字", parsed["3333"]["flags"])
        self.assertNotIn("金額", parsed["3333"]["numbers"])

    def test_float_spelled_values_in_ods_are_serialised_like_excel_integers(self):
        base = [data_row(code="1111", amount=1500000000, received=1150105), data_row(code="2222", amount=88, received=1150106)]
        rows_ods = [list(r) for r in base]
        rows_ods[0][6] = ("numraw", "1.5E9")        # LibreOffice 可能寫成指數或帶 .0；整數值一律不得帶 ".0"
        rows_ods[1][6] = ("numraw", "88.0")
        rows_ods[1][8] = ("numraw", "10.50")
        rows_x = [list(r) for r in base]
        rows_x[1][8] = ("num", "10.5")
        a = self.parsed_text("xlsx", rows_x)
        shutil.rmtree(self.dir, ignore_errors=True)
        code, out, *_ = self.go(ods_routes(ods_bytes(rows_ods)))
        self.assertEqual(code, 0, out)
        with open(os.path.join(self.dir, "parsed", "115.json"), encoding="utf-8") as f:
            b = f.read()
        self.assertEqual(strip_sha(a), strip_sha(b))
        self.assertNotIn(".0", json.dumps([r["fields"]["金額"] for r in json.loads(b)["rows"]]))

    def test_multiline_and_space_text_and_dates_as_strings(self):
        rows = [data_row(code="1111", company="甲\n乙", received=1150105, amount=100), data_row(code="2222", company="  前後空白  ", amount=200, received=1150106)]
        rows[0][10] = "1150110, 1150111"
        rows[1][13] = "1150120"
        rows[1][9] = "1150106"           # 字串型的 7 碼日期（實際檔的樣態）
        self.assert_same(rows)

    def test_row_numbers_and_trailer_match(self):
        a, b = self.parsed_text("xlsx", base_rows()), self.parsed_text("ods", base_rows())
        ra = [r["row"] for r in json.loads(a)["rows"]]
        self.assertEqual(ra, [r["row"] for r in json.loads(b)["rows"]])
        self.assertEqual(ra, [4, 5, 6, 7])
        self.assertEqual(self.meta()["trailer_rows"], [{"row": 8, "cells": [{"col": "證券代號", "value": "註：本表資料以申報當日為準。"}]},
                                                      {"row": 9, "cells": [{"col": "證券代號", "value": "資料來源：證期局"}]}])


# ─────────────────────────────────────────
class TestFormatSwitchGate(OdsBase):
    def switch(self, prev_rows, new_rows, text="2026-11-12", fname="1151112v1", prev_text="2026-10-08", new_body=None):
        self.seed_xlsx(prev_rows, prev_text)
        ods_url = f"https://www.fsc.gov.tw/userfiles/file/{fname}.ods"
        rts = ods_routes(new_body or ods_bytes(new_rows), text=text, links115=[(U_XLS, "EXCEL"), (ods_url, "ODS")], ods_url=ods_url)
        before = self.tree115()
        code, out, op, _ = self.go(rts)
        return code, out, before

    def assert_blocked(self, code, out, before, needle):
        self.assertEqual(code, 1, out)
        self.assertIn("格式切換閘門", out)
        self.assertIn(needle, out)
        self.assertEqual(self.tree115(), before, "閘門擋下時，115 年不得有任何檔案新增或變動")
        self.assertEqual(self.meta()["source_format"] if "source_format" in self.meta() else "xlsx", "xlsx")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "meta", "114.json")))

    # ── 共有鍵比例 ──
    def test_common_key_ratio_boundary(self):
        prev = many_rows(100)
        ok_rows = prev[:90] + many_rows(10, base=5000)
        code, out, _ = self.switch(prev, ok_rows)
        self.assertEqual(code, 0, out)                                    # 90/100 = 90%：通過（門檻是 ≥）
        self.assertIn("共有 90", out)
        shutil.rmtree(self.dir, ignore_errors=True)
        bad_rows = prev[:89] + many_rows(11, base=5000)
        code, out, before = self.switch(prev, bad_rows)
        self.assert_blocked(code, out, before, "共有案件鍵 89／上一版 100")

    # ── 逐欄不一致 2% ──
    def modify(self, rows, n, col, fn):
        out = [list(r) for r in rows]
        for r in out[:n]:
            r[col] = fn(r[col])
        return out

    def test_each_gated_column_two_percent_boundary(self):
        prev = many_rows(100)
        cols = {"證券代號": (0, lambda v: v + " "), "案件類別": (5, lambda v: v + " "), "金額": (6, lambda v: str(v) + " "),
                "幣別": (7, lambda v: "美元"), "收文日期": (9, lambda v: str(v) + " "), "案件性質": (17, lambda v: "變"),
                "承銷商": (4, lambda v: "別家"), "公司名稱": (3, lambda v: "別名")}
        for name, (ci, fn) in cols.items():
            with self.subTest(col=name):
                shutil.rmtree(self.dir, ignore_errors=True)
                code, out, _ = self.switch(prev, self.modify(prev, 2, ci, fn))           # 2/100 = 2%：通過（門檻是「超過」）
                self.assertEqual(code, 0, out)
                self.assertIn(f"{name}=2", out)
                shutil.rmtree(self.dir, ignore_errors=True)
                code, out, before = self.switch(prev, self.modify(prev, 3, ci, fn))      # 3/100：擋下
                self.assert_blocked(code, out, before, f"「{name}」不一致 3 筆")

    def test_informational_columns_never_block_but_are_reported(self):
        prev = many_rows(100)
        new = self.modify(self.modify(prev, 100, 1, lambda v: "上櫃"), 100, 8, lambda v: 99)
        code, out, _ = self.switch(prev, new)
        self.assertEqual(code, 0, out)
        self.assertIn("公司型態=100", out)
        self.assertIn("發行價格=100", out)
        self.assertEqual(self.load(f"diffs/115_{self.diff_name()}.json")["switch_gate"]["info_mismatch"], {"公司型態": 100, "發行價格": 100})

    def diff_name(self):
        return [f for f in os.listdir(os.path.join(self.dir, "diffs")) if f.startswith("115_")][0][4:-5]

    # ── 新鮮度 ──
    def test_filename_vs_page_gap_boundary(self):
        prev = many_rows(20)
        code, out, _ = self.switch(prev, prev, fname="1151109v1")          # 差 3 天：通過
        self.assertEqual(code, 0, out)
        self.assertIn("相差 3 天", out)
        shutil.rmtree(self.dir, ignore_errors=True)
        code, out, before = self.switch(prev, prev, fname="1151108v1")     # 差 4 天：擋下
        self.assert_blocked(code, out, before, "相差 4 天")
        shutil.rmtree(self.dir, ignore_errors=True)
        code, out, before = self.switch(prev, prev, fname="1151116v1")     # 檔名比頁面晚 4 天也擋（取絕對值）
        self.assert_blocked(code, out, before, "相差 4 天")

    def test_max_receipt_lag_boundary(self):
        prev = many_rows(20, receipt_base=1151001, spread=20)             # 收文日期 2026-10-01 ~ 10-20
        new = [list(r) for r in prev]
        new[0][9] = 1151029                                               # 最大收文日期 2026-10-29；頁面更新日 2026-11-12 → 落後 14 天：通過
        code, out, _ = self.switch(prev, new)
        self.assertEqual(code, 0, out)
        self.assertIn("早 14 天", out)
        shutil.rmtree(self.dir, ignore_errors=True)
        new[0][9] = 1151028                                               # 落後 15 天：擋下
        code, out, before = self.switch(prev, new)
        self.assert_blocked(code, out, before, "早 15 天")

    def test_freshness_unverifiable_is_blocked(self):
        prev = many_rows(20)
        code, out, before = self.switch(prev, prev, fname="申報v1")          # 檔名沒有民國日期
        self.assert_blocked(code, out, before, "無法驗證新鮮度")
        shutil.rmtree(self.dir, ignore_errors=True)
        code, out, before = self.switch(prev, prev, text="")                 # 頁面沒有更新日期
        self.assert_blocked(code, out, before, "無法驗證新鮮度")

    def test_blocked_switch_is_retried_next_day_and_last_check_not_advanced(self):
        prev = many_rows(100)
        code, out, before = self.switch(prev, prev[:50] + many_rows(50, base=7000))
        self.assertEqual(code, 1)
        lc = self.load("_last_check.json")["years"]
        self.assertEqual(lc["115"], "2026-10-12")                 # 失敗的年度不更新最後成功檢查日
        self.assertEqual(lc["114"], "2026-11-13")

    # ── 只在格式切換時啟用 ──
    def test_gate_not_applied_ods_to_ods(self):
        prev = many_rows(100)
        self.switch(prev, prev)
        crazy = self.modify(prev, 100, 3, lambda v: "全換") + []
        later = datetime(2026, 11, 20, 9, 0, tzinfo=TAIPEI)
        u = "https://www.fsc.gov.tw/userfiles/file/1151120v2.ods"
        code, out, *_ = self.go_at(ods_routes(ods_bytes(crazy), text="2026-11-20", links115=[(U_XLS, "x"), (u, "ODS")], ods_url=u), later)
        self.assertEqual(code, 0, out)
        d = [f for f in os.listdir(os.path.join(self.dir, "diffs")) if f.startswith("115_")]
        self.assertEqual(len(d), 2)
        newer = self.load("diffs/" + [f for f in sorted(d) if self.load("diffs/" + f)["old_format"] == "ods"][0])
        self.assertFalse(newer["format_change"])
        self.assertNotIn("switch_gate", newer)

    def go_at(self, rts, when):
        return self.run_archive(rts, clock=when)

    def test_first_ever_ods_has_no_gate(self):
        code, out, *_ = self.go(ods_routes(ods_bytes(many_rows(5))))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.meta()["history"][0]["format"], "ods")
        self.assertNotIn("格式切換", out)

    # ── diff 內容與標示 ──
    def test_switch_diff_is_flagged_and_content_is_not_omitted(self):
        prev = many_rows(100)
        new = [list(r) for r in prev[:97]] + many_rows(3, base=9000)       # 消失 3、新增 3
        new[0][2] = "撤回"                                                  # 結案類型變動
        new[1][13] = 1151105                                               # 生效日期新增
        code, out, _ = self.switch(prev, new)
        self.assertEqual(code, 0, out)
        d = self.load(f"diffs/115_{self.diff_name()}.json")
        self.assertIs(d["format_change"], True)
        self.assertEqual((d["old_format"], d["new_format"]), ("xlsx", "ods"))
        self.assertEqual((d["old_version_date"], d["new_version_date"]), ("2026-10-08", "2026-11-12"))
        self.assertEqual(d["counts"], {"added": 3, "vanished": 3, "changed_fields": 2, "old_rows": 100, "new_rows": 100})
        self.assertEqual(len(d["added"]), 3)
        self.assertEqual(len(d["vanished"]), 3)
        self.assertEqual({c["field"] for c in d["changed"]}, {"結案類型", "生效日期"})
        self.assertEqual(d["switch_gate"]["common_keys"], 97)
        self.assertIn("⚠ 跨格式切換：xlsx → ods", out)
        self.assertIn("與上一版 diff：新增 3、消失 3、同鍵欄位變動 2", out)
        self.assertIn("跨格式切換 xlsx → ods", out)
        with open(self.summary, encoding="utf-8") as f:
            self.assertIn("跨格式切換", f.read())

    def test_same_format_diff_has_format_change_false(self):
        self.seed_xlsx(many_rows(10))
        rows = many_rows(10) + many_rows(1, base=8000)
        code, out, *_ = self.run_archive(xlsx_routes(rows, "2026-10-20"), clock=datetime(2026, 10, 20, 9, 0, tzinfo=TAIPEI))
        self.assertEqual(code, 0, out)
        d = self.load(f"diffs/115_{self.diff_name()}.json")
        self.assertIs(d["format_change"], False)
        self.assertEqual((d["old_format"], d["new_format"]), ("xlsx", "xlsx"))

    # ── 冪等、切回 xlsx ──
    def test_rerun_of_same_ods_is_byte_identical(self):
        prev = many_rows(30)
        self.switch(prev, prev[:29] + many_rows(1, base=6000))
        snap = self.tree()
        code, out, *_ = self.run_archive(ods_routes(ods_bytes(prev[:29] + many_rows(1, base=6000)), text="2030-01-01"),
                                         clock=datetime(2027, 3, 3, 9, 0, tzinfo=TAIPEI))
        self.assertEqual(code, 0, out)
        strip = lambda t: {k: v for k, v in t.items() if k != "_last_check.json"}
        self.assertEqual(strip(self.tree()), strip(snap))
        self.assertEqual(len(os.listdir(os.path.join(self.dir, "diffs"))), 1)
        self.assertIn("內容與既有相同，沒有檔案變動", out)

    def test_switch_back_to_xlsx_goes_through_the_gate_and_is_flagged(self):
        prev = many_rows(100)
        self.switch(prev, prev)
        back = prev[:95] + many_rows(5, base=4000)
        u = "https://www.fsc.gov.tw/userfiles/file/1151124v1.xlsx"
        rts = ods_routes(b"", text="2026-11-24", links115=[(U_XLS, "x"), (u, "EXCEL"), (U_ODS, "ODS")])
        rts[u] = FakeResp(xlsx(back), ctype="application/octet-stream")
        code, out, op, _ = self.run_archive(rts, clock=datetime(2026, 11, 24, 9, 0, tzinfo=TAIPEI))
        self.assertEqual(code, 0, out)
        self.assertNotIn(U_ODS, [x for x, _, _ in op.requests])
        self.assertEqual(self.meta()["source_format"], "xlsx")
        self.assertEqual([h["format"] for h in self.meta()["history"] if "format" in h], ["xlsx", "ods", "xlsx"])
        newest = [f for f in sorted(os.listdir(os.path.join(self.dir, "diffs"))) if self.meta()["sha256"][:8] in f][0]
        d = self.load("diffs/" + newest)
        self.assertEqual((d["format_change"], d["old_format"], d["new_format"]), (True, "ods", "xlsx"))
        self.assertEqual(d["switch_gate"]["common_keys"], 95)

    def test_switch_back_to_xlsx_is_blocked_when_inconsistent(self):
        prev = many_rows(100)
        self.switch(prev, prev)
        before = self.tree115()
        u = "https://www.fsc.gov.tw/userfiles/file/1151124v1.xlsx"
        rts = ods_routes(b"", text="2026-11-24", links115=[(u, "EXCEL")])
        rts[u] = FakeResp(xlsx(many_rows(100, base=3000)), ctype="application/octet-stream")
        code, out, *_ = self.run_archive(rts, clock=datetime(2026, 11, 24, 9, 0, tzinfo=TAIPEI))
        self.assertEqual(code, 1)
        self.assertIn("格式切換閘門", out)
        self.assertEqual(self.tree115(), before)

    def test_legacy_meta_without_source_format_counts_as_xlsx(self):
        self.seed_xlsx(many_rows(20))
        m = self.meta()
        for k in ("source_format", "source_ext"):
            m.pop(k, None)
        for h in m["history"]:
            h.pop("format", None)
        with open(os.path.join(self.dir, "meta", "115.json"), "wb") as f:
            f.write(af.dump_meta(m))
        code, out, *_ = self.go(ods_routes(ods_bytes(many_rows(20))))
        self.assertEqual(code, 0, out)
        d = self.load(f"diffs/115_{self.diff_name()}.json")
        self.assertEqual((d["old_format"], d["format_change"]), ("xlsx", True))


# ─────────────────────────────────────────
class TestGateConstants(unittest.TestCase):
    def test_named_constants_with_expected_values(self):
        self.assertEqual(af.SWITCH_MIN_COMMON_KEY_PCT, 90)
        self.assertEqual(af.SWITCH_MAX_COLUMN_MISMATCH_PCT, 2)
        self.assertEqual(af.SWITCH_MAX_FILE_PAGE_GAP_DAYS, 3)
        self.assertEqual(af.SWITCH_MAX_RECEIPT_LAG_DAYS, 14)
        self.assertEqual(af.SWITCH_GATED_COLUMNS, ("證券代號", "案件類別", "金額", "幣別", "收文日期", "案件性質", "承銷商", "公司名稱"))
        self.assertEqual(af.SWITCH_INFO_COLUMNS, ("公司型態", "發行價格"))

    def test_every_threshold_has_a_rationale_comment(self):
        src = open(os.path.join(ROOT, "scripts", "archive_fsc.py"), encoding="utf-8").read()
        for name in ("SWITCH_MIN_COMMON_KEY_PCT", "SWITCH_MAX_COLUMN_MISMATCH_PCT", "SWITCH_MAX_FILE_PAGE_GAP_DAYS", "SWITCH_MAX_RECEIPT_LAG_DAYS"):
            i = src.index(f"\n{name} =")
            self.assertTrue(src[:i].rstrip().splitlines()[-1].lstrip().startswith("#"), name)


# ─────────────────────────────────────────
class TestOdsRawIsCommitted(_CommitBase):
    """真實 .gitignore + 真實「提交存檔」步驟：raw/*.ods 必須入庫；被忽略時防呆必須擋下。"""

    for _n in [n for n in dir(_CommitBase) if n.startswith("test_")]:
        locals()[_n] = None
    del _n

    def ods_run(self):
        op = RoutedOpener(ods_routes(ods_bytes(base_rows())))
        with contextlib.redirect_stdout(io.StringIO()):
            return af.run({"PROBE_REPO": "o/r", "ARCHIVE_FSC_DIR": os.path.join(self.work, "data", "fsc")}, opener=op, sleep=lambda s: None, now=lambda: NOV)

    def test_real_gitignore_does_not_ignore_raw_ods_and_it_is_pushed(self):
        r = self.git(self.work, "check-ignore", "-v", "data/fsc/raw/115_abcd1234_1151112v1.ods")
        self.assertEqual(r.returncode, 1, r.stdout)           # 1 = 沒有被任何規則忽略
        self.assertEqual(self.ods_run(), 0)
        r = self.run_step()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tree = self.remote_tree()
        self.assertTrue(any(n.startswith("data/fsc/raw/115_") and n.endswith(".ods") for n in tree), tree)
        self.assertTrue(any(n.startswith("data/fsc/parsed/115") for n in tree), tree)

    def test_if_ods_were_ignored_the_guard_blocks_the_whole_commit(self):
        with open(os.path.join(self.work, ".gitignore"), "a", encoding="utf-8") as f:
            f.write("\n*.ods\n")
        self.assertEqual(self.ods_run(), 0)
        r = self.run_step()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("原始檔未被 git 追蹤", r.stdout)
        self.assertIn(".ods", r.stdout)
        self.assertFalse(any(n.startswith("data/fsc/") for n in self.remote_tree()))

    def test_guard_source_covers_every_file_in_raw(self):
        import yaml as _y
        wf = _y.safe_load(open(os.path.join(ROOT, ".github", "workflows", "archive-fsc.yml"), encoding="utf-8"))
        script = [s for s in wf["jobs"]["archive"]["steps"] if "push_with_retry.sh" in s.get("run", "")][0]["run"]
        self.assertIn("for f in data/fsc/raw/*; do", script)        # 不依副檔名挑：xlsx、ods 都涵蓋


if __name__ == "__main__":
    unittest.main()
