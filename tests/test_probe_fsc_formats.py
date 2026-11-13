"""probe_fsc 的 formats 模式、fsc_sources.read_ods／row_links 的測試。全部離線；
ODS、OLE2 標頭、HTML 都是測試裡合成的虛構樣本。"""

import contextlib
import io
import json
import os
import socket
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fsc_sources as fs  # noqa: E402
import probe_fsc as pf  # noqa: E402
from test_fsc_probe import (FakeResp, RoutedOpener, HEADER, data_row, good_xlsx, page_html, PAGE_URL)  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_real_connect = socket.socket.connect


def _block(*a, **k):  # pragma: no cover
    raise AssertionError("測試不得連網")


def setUpModule():
    socket.socket.connect = _block


def tearDownModule():
    socket.socket.connect = _real_connect


SECRETS = ["機密公司甲", "機密承銷商乙", "88888888"]
OLE2 = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 400 + "Workbook".encode("utf-16-le") + b"\x00" * 100

NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
      'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
      'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"')


def ods_cell(v):
    if v is None:
        return "<table:table-cell/>"
    if isinstance(v, tuple) and v[0] == "date":
        return f'<table:table-cell office:value-type="date" office:date-value="{v[1]}"><text:p>{v[1]}</text:p></table:table-cell>'
    if isinstance(v, tuple) and v[0] == "rep":
        return ods_cell(v[1]).replace("<table:table-cell", f'<table:table-cell table:number-columns-repeated="{v[2]}"', 1)
    if isinstance(v, (int, float)):
        return f'<table:table-cell office:value-type="float" office:value="{v}"><text:p>{v}</text:p></table:table-cell>'
    return f'<table:table-cell office:value-type="string"><text:p>{escape(v)}</text:p></table:table-cell>'


def make_ods(sheets, *, filler=True, doctype=False, mimetype="application/vnd.oasis.opendocument.spreadsheet", content=True):
    tables = []
    for name, rows in sheets:
        trs = "".join("<table:table-row>" + "".join(ods_cell(v) for v in r) + "</table:table-row>" for r in rows)
        if filler:
            trs += '<table:table-row table:number-rows-repeated="1048500"><table:table-cell table:number-columns-repeated="1024"/></table:table-row>'
        tables.append(f'<table:table table:name="{escape(name)}">{trs}</table:table>')
    dt = '<!DOCTYPE x [<!ENTITY a "b">]>' if doctype else ""
    xml = f'<?xml version="1.0" encoding="UTF-8"?>{dt}<office:document-content {NS}><office:body><office:spreadsheet>{"".join(tables)}</office:spreadsheet></office:body></office:document-content>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if mimetype is not None:
            z.writestr("mimetype", mimetype, compress_type=zipfile.ZIP_STORED)
        if content:
            z.writestr("content.xml", xml)
    return buf.getvalue()


def ods_rows(rows=None, title=True):
    return ([["115年度申報案件辦理情形彙總表"]] if title else []) + [HEADER] + (
        rows if rows is not None else [ods_data(), ods_data(code="1111", received="1150110")])


def ods_data(code="9876", company="機密公司甲", underwriter="機密承銷商乙", category="現金增資", amount=88888888, received="1150109", price=10):
    r = data_row(code=code, company=company, underwriter=underwriter, category=category, amount=amount, received=received)
    r[8] = price
    return [str(x) if isinstance(x, str) else x for x in r]


class FormatsBase(unittest.TestCase):
    def tmp_old(self, rows, sha="0ea53962" + "0" * 56, ref="2026-10-20"):
        d = tempfile.mkdtemp()
        p = Path(d) / "115.json"
        out = []
        for r in rows:
            f = {c: ("" if v is None else str(v)) for c, v in zip(HEADER, r)}
            out.append({"fields": f, "dates_iso": {"收文日期": [pf.roc7_to_iso(f["收文日期"])]} if pf.roc7_to_iso(f["收文日期"]) else {}})
        p.write_text(json.dumps({"sha256": sha, "reference_date": ref, "rows": out}), encoding="utf-8")
        return p

    def run_formats(self, routes, env=None, old=None):
        op = RoutedOpener(routes)
        out, sleeps = io.StringIO(), []
        old = old or self.tmp_old([ods_data(), ods_data(code="1111", received="1150110")])
        with mock.patch.object(pf, "OLD_PARSED", old), contextlib.redirect_stdout(out):
            code = pf.run({"PROBE_REPO": "o/r", "PROBE_MODE": "formats", **(env or {})}, opener=op, sleep=sleeps.append)
        return code, out.getvalue(), op, sleeps


ROW115 = ("115年度申報案件", [("https://www.sfb.gov.tw/uploaddowndoc?file=a/1151105v1.ods&flag=doc", "ods"),
                          ("https://www.sfb.gov.tw/uploaddowndoc?file=a/1151105v1.xls", "115.11.5(v1)")])
ROW114 = ("114年度申報案件", [("https://www.sfb.gov.tw/uploaddowndoc?file=a/1141231v2.xlsx", "114.12.31(v2)"),
                          ("https://www.sfb.gov.tw/uploaddowndoc?file=a/1141231v2.ods", "ods")])
U_ODS = "https://www.sfb.gov.tw/uploaddowndoc?file=a/1151105v1.ods&flag=doc"
U_XLS = "https://www.sfb.gov.tw/uploaddowndoc?file=a/1151105v1.xls"
U_114X = "https://www.sfb.gov.tw/uploaddowndoc?file=a/1141231v2.xlsx"


def routes(ods=None, xls=None, rows=None):
    return {
        PAGE_URL: FakeResp(page_html(rows or [ROW114, ROW115]).encode("utf-8")),
        U_ODS: FakeResp(ods if ods is not None else make_ods([("115年度申報案件", ods_rows())]), ctype="application/vnd.oasis.opendocument.spreadsheet"),
        U_XLS: FakeResp(xls if xls is not None else OLE2, ctype="application/vnd.ms-excel"),
    }


# ─────────────────────────────────────────
class TestRowLinks(unittest.TestCase):
    def test_lists_all_links_with_decoded_names(self):
        h = page_html([("115年度申報案件", [("/f/%E6%A1%88%E4%BB%B6.ods", "ods"), ("/f/b.xls", "xls"), ("/f/b.xls", "dup"),
                                       ("http://www.sfb.gov.tw/c.pdf", "pdf"), ("https://evil.example.com/d.xlsx", "x")])])
        ls = fs.row_links(h, "https://www.sfb.gov.tw/ch/home.jsp", "115年度申報案件")
        self.assertEqual([l.ext for l in ls], [".ods", ".xls", ".pdf", ".xlsx"])
        self.assertEqual(ls[0].filename, "案件.ods")
        self.assertEqual(ls[0].host, "www.sfb.gov.tw")
        self.assertTrue(ls[0].allowed and ls[0].https)
        self.assertFalse(ls[2].allowed)
        self.assertFalse(ls[3].allowed)
        self.assertEqual(ls[0].anchor_text, "ods")

    def test_extension_from_query_string(self):
        h = page_html([("115年度申報案件", [("/uploaddowndoc?file=a/b%201.ods&flag=doc", "x"), ("/dl?id=7", "y")])])
        ls = fs.row_links(h, "https://www.sfb.gov.tw/ch/home.jsp", "115年度申報案件")
        self.assertEqual((ls[0].ext, ls[0].filename), (".ods", "b 1.ods"))
        self.assertEqual(ls[1].ext, "（無）")

    def test_missing_and_duplicate_rows(self):
        with self.assertRaises(fs.FscError):
            fs.row_links(page_html([("114年度申報案件", [("/a.xlsx", "x")])]), PAGE_URL, "115年度申報案件")
        dup = page_html([("115年度申報案件", [("/a.ods", "x")]), ("115年度申報案件", [("/b.ods", "y")])])
        with self.assertRaises(fs.FscError) as cm:
            fs.row_links(dup, PAGE_URL, "115年度申報案件")
        self.assertIn("2 列", str(cm.exception))


class TestReadOds(unittest.TestCase):
    def test_types_and_filler(self):
        data = make_ods([("S1", [["a", 1, ("date", "2026-01-09"), None, "x"], [], ["b"]])])
        sh = fs.read_ods(data)
        self.assertEqual([s.name for s in sh], ["S1"])
        self.assertEqual([c.vtype for c in sh[0].rows[0]], ["string", "float", "date", "", "string"])
        self.assertEqual(sh[0].rows[0][1].value, "1")
        self.assertEqual(sh[0].rows[0][2].value, "2026-01-09")
        self.assertEqual(len(sh[0].rows), 3)          # 結尾的 1048500 空列填充不展開
        self.assertEqual(len(sh[0].rows[0]), 5)       # 列尾 1024 欄空填充不展開

    def test_repeated_cells_expand(self):
        sh = fs.read_ods(make_ods([("S", [["a", ("rep", "z", 3)]])]))
        self.assertEqual([c.text for c in sh[0].rows[0]], ["a", "z", "z", "z"])

    def test_multiple_sheets(self):
        sh = fs.read_ods(make_ods([("A", [["1"]]), ("B", [["2"]])]))
        self.assertEqual([s.name for s in sh], ["A", "B"])

    def test_rejects_bad_inputs(self):
        for bad, why in [
            (make_ods([("S", [["a"]])], doctype=True), "DTD"),
            (make_ods([("S", [["a"]])], mimetype="application/zip"), "mimetype"),
            (make_ods([("S", [["a"]])], content=False), "content.xml"),
            (b"not a zip at all", "zip"),
        ]:
            with self.subTest(why=why), self.assertRaises(fs.FscError) as cm:
                fs.read_ods(bad)
            self.assertIn(why, str(cm.exception))

    def test_nonempty_huge_repeat_rejected(self):
        with self.assertRaises(fs.FscError):
            fs.read_ods(make_ods([("S", [[("rep", "x", 100000)]])]))

    def test_xlsx_still_rejects_dtd_with_original_message(self):
        bad = zipfile.ZipFile
        from test_fsc_probe import make_xlsx
        with self.assertRaises(fs.FscError) as cm:
            fs.read_xlsx(make_xlsx([("S", [["a"]])], doctype=True))
        self.assertIn("xlsx", str(cm.exception))
        self.assertTrue(bad)


class TestSniff(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(pf.sniff(OLE2)[0], "ole2")
        self.assertIn("BIFF8", pf.sniff(OLE2)[1])
        self.assertEqual(pf.sniff(bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 50)[0], "ole2")
        self.assertEqual(pf.sniff(b"PK\x03\x04rest")[0], "zip")
        self.assertEqual(pf.sniff(b"  \n<!DOCTYPE html><html>")[0], "html")
        self.assertEqual(pf.sniff(b"<HTML><body>")[0], "html")
        self.assertEqual(pf.sniff(b'<?xml version="1.0"?><Workbook/>')[0], "xml")
        self.assertEqual(pf.sniff(b"%PDF-1.4")[0], "other")
        self.assertEqual(pf.sniff(b"")[0], "empty")

    def test_zip_kind(self):
        self.assertEqual(pf.zip_kind(make_ods([("S", [["a"]])])), "ods")
        self.assertEqual(pf.zip_kind(good_xlsx()), "xlsx")
        self.assertEqual(pf.zip_kind(b"PK\x03\x04junk"), "other")

    def test_canon(self):
        self.assertEqual(pf.canon_value("1.5E9"), "1500000000")
        self.assertEqual(pf.canon_value("1500000000.0"), "1500000000")
        self.assertEqual(pf.canon_value(" １２３ "), "123")
        self.assertEqual(pf.canon_value("2.5"), "2.5")
        self.assertEqual(pf.canon_value("1150109"), "1150109")


class TestFormatsRun(FormatsBase):
    def test_happy_path(self):
        code, out, op, sleeps = self.run_formats(routes())
        self.assertEqual(code, 0, out)
        urls = [u for u, _, _ in op.requests]
        self.assertEqual(urls, [PAGE_URL, U_ODS, U_XLS])                 # 頁面 + 115 的 .ods、.xls；114 的任何檔都不下載
        self.assertTrue(all(m == "GET" for _, m, _ in op.requests))
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(s >= 6 for s in sleeps))
        for want in ["「115年度申報案件」那一列的全部連結（共 2 個）", "「114年度申報案件」那一列的全部連結（共 2 個）",
                     "副檔名 .ods", "副檔名 .xls", "副檔名 .xlsx", "檔名 1151105v1.ods",
                     "前 8 bytes：504B", "前 8 bytes：D0CF11E0A1B11AE1", "OLE2 複合檔，含「Workbook」目錄項",
                     "表頭比對：與預期的 18 欄完全相同", "資料列數（表頭之後、結尾說明列之前、非全空）：2；其中證券代號為空 0；結尾／說明列數：0",
                     "7碼[字串]", "「金額」儲存格型別：float×2", "收文日期", "探測完成：全部成功"]:
            self.assertIn(want, out)
        self.assertIn("日期範圍 2026-01-09 ～ 2026-01-10", out.replace("日期範圍 2026-01-09", "日期範圍 2026-01-09"))
        for s in SECRETS:
            self.assertNotIn(s, out)
        for _, _, headers in op.requests:
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)

    def test_consistency_numbers_and_no_content(self):
        code, out, *_ = self.run_formats(routes())
        self.assertIn("兩邊都有 2｜只在 ODS 0｜只在舊版 0", out)
        self.assertIn("金額：不一致 0", out)
        for c in pf.STABLE_COLUMNS:
            self.assertIn(f"{c}：不一致 0", out)

    def test_consistency_detects_shape_differences_without_content(self):
        ods = make_ods([("S", ods_rows([ods_data(company="機密公司乙"), ods_data(code="1111", received="1150110", price="10.5")]))])
        # 舊版第一列公司名稱不同；第二列發行價格為數字 10 → 與 ODS 的 "10.5" 不同
        code, out, *_ = self.run_formats(routes(ods=ods))
        self.assertEqual(code, 0, out)
        self.assertIn("公司名稱：不一致 1", out)
        self.assertIn("發行價格：不一致 1", out)
        self.assertIn("ODS:C{5} ≠ 舊:C{5}", out)
        self.assertNotIn("機密公司乙", out)
        for s in SECRETS:
            self.assertNotIn(s, out)

    def test_consistency_key_sets(self):
        ods = make_ods([("S", ods_rows([ods_data(), ods_data(code="2222", received="1150111")]))])
        code, out, *_ = self.run_formats(routes(ods=ods))
        self.assertIn("兩邊都有 1｜只在 ODS 1｜只在舊版 1", out)

    def test_numeric_amount_vs_old_string_is_not_a_mismatch(self):
        ods = make_ods([("S", ods_rows([ods_data(amount=1500000000.0), ods_data(code="1111", received="1150110")]))])
        old = self.tmp_old([ods_data(amount="1.5E9"), ods_data(code="1111", received="1150110")])
        _, out, *_ = self.run_formats(routes(ods=ods), old=old)
        self.assertIn("金額：不一致 0", out)
        self.assertIn("僅表示法不同", out)

    def test_date_cells_typed_as_date_or_number_are_reported(self):
        rows = ods_rows([ods_data(), ods_data(code="1111", received="1150110")])
        rows[2][9] = ("date", "2026-01-09")
        rows[3][9] = 1150110
        _, out, *_ = self.run_formats(routes(ods=make_ods([("S", rows)])))
        self.assertIn("ods日期型[日期型]×1", out)
        self.assertIn("7碼[數字]×1", out)

    def test_trailer_rows_counted(self):
        rows = ods_rows() + [["資料來源：本會"], []]
        _, out, *_ = self.run_formats(routes(ods=make_ods([("S", rows)])))
        self.assertIn("資料列數（表頭之後、結尾說明列之前、非全空）：2；其中證券代號為空 0；結尾／說明列數：1", out)

    def test_header_mismatch_reported_not_crash(self):
        rows = ods_rows()
        rows[1] = HEADER[:-1]
        code, out, *_ = self.run_formats(routes(ods=make_ods([("S", rows)])))
        self.assertIn("表頭比對：與預期的 18 欄不同", out)
        self.assertIn("缺少 ['案件性質']", out)

    def test_html_block_page_for_ods_is_error(self):
        r = routes(ods=b"<html><head><title>Access Denied</title></head><body>blocked</body></html>")
        code, out, *_ = self.run_formats(r)
        self.assertEqual(code, 1)
        self.assertIn("::error title=探測失敗 115年度申報案件 .ods::副檔名 .ods 但回應是 HTML", out)
        self.assertIn("前 8 bytes：3C68746D6C3E", out)
        self.assertIn("Access Denied", out)
        self.assertIn("實際格式：HTML 文件", out)
        self.assertIn("探測完成：有失敗", out)

    def test_ole2_under_ods_name_is_warning_with_format(self):
        _, out, *_ = self.run_formats(routes(ods=OLE2))
        self.assertIn("::warning title=副檔名與格式不符 .ods::", out)
        self.assertIn("副檔名與實際格式不符：副檔名 .ods，實際是 OLE2", out)

    def test_zip_ods_under_xls_name_is_warning_and_analyzed(self):
        _, out, *_ = self.run_formats(routes(xls=make_ods([("S", ods_rows())])))
        self.assertIn("::warning title=副檔名與格式不符 .xls::", out)
        self.assertIn("zip 內容種類：ods", out)
        self.assertGreaterEqual(out.count("表頭比對：與預期的 18 欄完全相同"), 2)

    def test_xlsx_zip_under_ods_name_analysed_as_xlsx(self):
        code, out, *_ = self.run_formats(routes(ods=good_xlsx()))
        self.assertIn("zip 內容種類：xlsx", out)
        self.assertIn("::warning title=副檔名與格式不符 .ods::", out)

    def test_unknown_blob_is_error(self):
        code, out, *_ = self.run_formats(routes(ods=b"%PDF-1.4 hello"))
        self.assertEqual(code, 1)
        self.assertIn("::error title=探測失敗 115年度申報案件 .ods::", out)

    def test_corrupt_ods_is_error(self):
        code, out, *_ = self.run_formats(routes(ods=make_ods([("S", [["a"]])], doctype=True)))
        self.assertEqual(code, 1)
        self.assertIn("DTD", out)
        self.assertIn("::error", out)

    def test_http_error_is_error_and_other_file_still_probed(self):
        import urllib.error
        r = routes()
        r[U_ODS] = urllib.error.HTTPError(U_ODS, 403, "Forbidden", {}, None)
        code, out, op, sleeps = self.run_formats(r)
        self.assertEqual(code, 1)
        self.assertIn("::error title=探測失敗 115年度申報案件 .ods::HTTP 403", out)
        self.assertEqual(len(op.requests), 3)         # 不重試：ods 只有一次，之後仍探 xls

    def test_redirect_is_not_followed(self):
        import urllib.error

        class H(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)
        r = routes()
        r[U_ODS] = urllib.error.HTTPError(U_ODS, 302, "Found", H({"Location": "https://elsewhere.example.org/x"}), None)
        code, out, op, _ = self.run_formats(r)
        self.assertEqual(code, 1)
        self.assertIn("elsewhere.example.org", out)
        self.assertEqual([u for u, _, _ in op.requests].count(U_ODS), 1)
        self.assertNotIn("https://elsewhere.example.org/x", [u for u, _, _ in op.requests])

    def test_no_ods_xls_in_115_row_is_error(self):
        rows = [ROW114, ("115年度申報案件", [("https://www.sfb.gov.tw/a/only.pdf", "pdf")])]
        code, out, op, _ = self.run_formats(routes(rows=rows))
        self.assertEqual(code, 1)
        self.assertIn("沒有任何 .ods 或 .xls 連結", out)
        self.assertEqual(len(op.requests), 1)

    def test_xlsx_present_is_noted(self):
        rows = [ROW114, ("115年度申報案件", ROW115[1] + [("https://www.sfb.gov.tw/a/1151105v1.xlsx", "x")])]
        _, out, *_ = self.run_formats(routes(rows=rows))
        self.assertIn("現在有 .xlsx 連結", out)

    def test_missing_115_row_is_error(self):
        code, out, op, _ = self.run_formats(routes(rows=[ROW114]))
        self.assertEqual(code, 1)
        self.assertIn("::error title=探測失敗 115年度申報案件::", out)
        self.assertEqual(len(op.requests), 1)

    def test_offsite_link_not_downloaded(self):
        rows = [ROW114, ("115年度申報案件", [("https://evil.example.com/a.ods", "x")])]
        code, out, op, _ = self.run_formats(routes(rows=rows))
        self.assertEqual(code, 1)
        self.assertEqual(len(op.requests), 1)
        self.assertIn("不可下載", out)

    def test_download_cap(self):
        links = [(f"https://www.sfb.gov.tw/a/{i}.xls", "x") for i in range(7)]
        r = routes(rows=[ROW114, ("115年度申報案件", links)])
        for _, _, _ in []:
            pass
        for i in range(7):
            r[f"https://www.sfb.gov.tw/a/{i}.xls"] = FakeResp(OLE2)
        code, out, op, _ = self.run_formats(r)
        self.assertEqual(len(op.requests), 1 + pf.MAX_DOWNLOADS)

    def test_page_failure(self):
        r = routes()
        r[PAGE_URL] = FakeResp(b"x", status=500)
        code, out, op, _ = self.run_formats(r)
        self.assertEqual(code, 1)
        self.assertIn("::error title=探測失敗 頁面::", out)

    def test_dry_run_makes_no_request(self):
        code, out, op, sleeps = self.run_formats(routes(), {"PROBE_DRY_RUN": "1"})
        self.assertEqual(code, 0)
        self.assertEqual(op.requests, [])
        self.assertEqual(sleeps, [])
        self.assertIn("DRY-RUN（formats）", out)
        self.assertIn("最多送 5 次 GET", out)

    def test_bad_mode_exits_2(self):
        code, out, op, _ = self.run_formats(routes(), {"PROBE_MODE": "bogus"})
        self.assertEqual(code, 2)
        self.assertEqual(op.requests, [])

    def test_default_mode_is_unchanged_xlsx(self):
        op = RoutedOpener({})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = pf.run({"PROBE_REPO": "o/r", "PROBE_DRY_RUN": "1"}, opener=op, sleep=lambda s: None)
        self.assertEqual(code, 0)
        self.assertIn("最多送 3 次 GET", out.getvalue())

    def test_summary_written_only_to_step_summary(self):
        d = tempfile.mkdtemp()
        sm = os.path.join(d, "sum.md")
        before = set(os.listdir(ROOT))
        _, out, *_ = self.run_formats(routes(), {"GITHUB_STEP_SUMMARY": sm})
        self.assertTrue(os.path.exists(sm))
        self.assertEqual(set(os.listdir(ROOT)), before)
        for s in SECRETS:
            self.assertNotIn(s, Path(sm).read_text(encoding="utf-8"))


class TestFreshness(unittest.TestCase):
    def setUp(self):
        self.old = FormatsBase().tmp_old([ods_data(received="1150109"), ods_data(code="1111", received="1150110")], ref="2026-10-20")

    def f(self, mx, n, update="更新日期：115年11月5日"):
        return "\n".join(pf.freshness(mx, n, {".ods": "1151105v1.ods", ".xls": "1151105v1.xls"}, update, self.old))

    def test_keeps_up(self):
        t = self.f("2026-11-03", 700)
        self.assertIn("ODS 看起來有跟上", t)
        self.assertIn("頁面更新日（ISO）：2026-11-05", t)

    def test_iso_page_date(self):
        t = self.f("2026-11-11", 718, update="2026-11-12")
        self.assertIn("頁面更新日（ISO）：2026-11-12", t)
        self.assertIn("ODS 看起來有跟上", t)

    def test_lagging_when_not_newer(self):
        t = self.f("2026-01-10", 2)
        self.assertIn("判定落後", t)

    def test_close_to_page_date_but_not_newer_is_still_lagging(self):
        t = self.f("2026-01-10", 2, update="更新日期：115年1月12日")
        self.assertIn("判定落後", t)
        self.assertNotIn("看起來有跟上", t)

    def test_newer_but_far_from_page_date(self):
        t = self.f("2026-10-15", 700)
        self.assertIn("無法確認已跟上最新", t)

    def test_no_dates(self):
        self.assertIn("無法判斷", self.f(None, 0))

    def test_unparsable_page_date(self):
        t = self.f("2026-11-03", 700, update="")
        self.assertIn("無法確認已跟上最新", t)


class TestWorkflow(unittest.TestCase):
    @unittest.skipIf(yaml is None, "no yaml")
    def test_contract(self):
        wf = yaml.safe_load(Path(ROOT, ".github/workflows/probe-fsc.yml").read_text(encoding="utf-8"))
        on = wf.get(True) or wf.get("on")
        self.assertEqual(set(on), {"workflow_dispatch"})
        inputs = on["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["mode"]["default"], "formats")
        self.assertEqual(inputs["mode"]["options"], ["formats", "xlsx"])
        self.assertEqual(wf["permissions"], {"contents": "read"})
        text = Path(ROOT, ".github/workflows/probe-fsc.yml").read_text(encoding="utf-8")
        self.assertNotIn("secrets.", text)
        self.assertIn("PROBE_MODE", text)


if __name__ == "__main__":
    unittest.main()
