"""fsc_sources 的 ODS 讀取（read_ods／ods_to_xsheets／ods_number_text）與選檔規則（resolve_year_source）的測試。全部離線、合成樣本。"""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fsc_sources as fs  # noqa: E402
from test_fsc_probe import make_xlsx, page_html  # noqa: E402
from test_probe_fsc_formats import make_ods  # noqa: E402

PAGE = "https://www.sfb.gov.tw/ch/home.jsp?id=1016"
L115 = "115年度申報案件"


def xlsx_cell_values(rows):
    sh = fs.read_xlsx(make_xlsx([("S", rows)]))[0]
    return [[c.value for c in r] for r in sh.rows]


def ods_cell_values(rows, **kw):
    sh = fs.ods_to_xsheets(fs.read_ods(make_ods([("S", rows)], **kw)))[0]
    return [[c.value for c in r] for r in sh.rows]


class TestOdsRepeats(unittest.TestCase):
    def test_trailing_empty_rows_and_cols_are_trimmed_not_expanded(self):
        sh = fs.read_ods(make_ods([("S", [["a", ("rep", None, 1048000)], [("rep", None, 5000)]])], row_repeats={1: 900000}))[0]
        self.assertEqual(len(sh.rows), 1)
        self.assertEqual([c.text for c in sh.rows[0]], ["a"])

    def test_column_repeat_limit_message(self):
        with self.assertRaises(fs.FscError) as cm:
            fs.read_ods(make_ods([("S", [[("rep", "x", fs.MAX_ODS_REPEAT + 1)]])]))
        self.assertIn("重複", str(cm.exception))      # 先被「單一重複上限」擋下；寬度上限（500 欄）是另一道

    def test_repeated_nonempty_cells_expand_within_width_limit(self):
        sh = fs.read_ods(make_ods([("S", [["a", ("rep", "z", 400)]])]))[0]
        self.assertEqual(len(sh.rows[0]), 401)
        with self.assertRaises(fs.FscError) as cm:
            fs.read_ods(make_ods([("S", [["a", ("rep", "z", 500)]])]))
        self.assertIn("欄", str(cm.exception))

    def test_nonempty_row_repeat_limit(self):
        fs.read_ods(make_ods([("S", [["a"]])], row_repeats={0: fs.MAX_ODS_REPEAT}))
        with self.assertRaises(fs.FscError):
            fs.read_ods(make_ods([("S", [["a"]])], row_repeats={0: fs.MAX_ODS_REPEAT + 1}))

    def test_empty_gap_in_the_middle_must_be_materialised_or_rejected(self):
        # 中間 N 個空白列後面還有內容：不得靜默丟掉（列號會位移）
        sh = fs.read_ods(make_ods([("S", [["a"], [], ["b"]])], row_repeats={1: 7}))[0]
        self.assertEqual([r[0].text if r else "" for r in sh.rows], ["a"] + [""] * 7 + ["b"])
        with self.assertRaises(fs.FscError):
            fs.read_ods(make_ods([("S", [["a"], [], ["b"]])], row_repeats={1: fs.MAX_ODS_REPEAT + 1}))
        sh = fs.read_ods(make_ods([("S", [["a", None, "b"]])]))[0]
        self.assertEqual([c.text for c in sh.rows[0]], ["a", "", "b"])
        with self.assertRaises(fs.FscError):
            fs.read_ods(make_ods([("S", [["a", ("rep", None, fs.MAX_ODS_REPEAT + 1), "b"]])]))

    def test_total_expanded_cells_limit(self):
        rows = [[("rep", "x", 400)] for _ in range(3)]
        rr = {0: 1900, 1: 1900, 2: 1900}          # 每列 400 格 × 1900 × 3 = 2,280,000 > 2,000,000
        with self.assertRaises(fs.FscError) as cm:
            fs.read_ods(make_ods([("S", rows)], row_repeats=rr))
        self.assertIn("總數", str(cm.exception))
        rr2 = {0: 1000, 1: 1000, 2: 1000}         # 1,200,000：可以
        self.assertEqual(len(fs.read_ods(make_ods([("S", rows)], row_repeats=rr2))[0].rows), 3000)

    def test_nested_table_rejected(self):
        import io, zipfile
        from test_probe_fsc_formats import NS
        xml = (f'<?xml version="1.0"?><office:document-content {NS}><office:body><office:spreadsheet>'
               '<table:table table:name="A"><table:table-row><table:table-cell><table:table table:name="B"/></table:table-cell></table:table-row></table:table>'
               '</office:spreadsheet></office:body></office:document-content>')
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("mimetype", fs.ODS_MIME)
            z.writestr("content.xml", xml)
        with self.assertRaises(fs.FscError):
            fs.read_ods(b.getvalue())

    def test_entity_and_zipbomb_guards_kept(self):
        with self.assertRaises(fs.FscError):
            fs.read_ods(make_ods([("S", [["a"]])], doctype=True))
        old = fs.MAX_UNCOMPRESSED
        fs.MAX_UNCOMPRESSED = 10
        try:
            with self.assertRaises(fs.FscError):
                fs.read_ods(make_ods([("S", [["a"]])]))
        finally:
            fs.MAX_UNCOMPRESSED = old


class TestOdsTextMatchesXlsx(unittest.TestCase):
    def test_multi_paragraph_cell_equals_xlsx_newline(self):
        self.assertEqual(ods_cell_values([[("paras", ["甲", "乙"])]]), xlsx_cell_values([["甲\n乙"]]))

    def test_spaces_leading_trailing_and_runs(self):
        cases = {
            "a   b": 'a<text:s text:c="3"/>b',
            " x": '<text:s/>x',
            "x  ": 'x<text:s text:c="2"/>',
            "a b": "a b",
        }
        for want, xml in cases.items():
            with self.subTest(want=want):
                self.assertEqual(ods_cell_values([[("xml", xml)]]), xlsx_cell_values([[want]]))

    def test_tab_and_line_break_and_span(self):
        self.assertEqual(ods_cell_values([[("xml", "a<text:tab/>b")]]), xlsx_cell_values([["a\tb"]]))
        self.assertEqual(ods_cell_values([[("xml", "a<text:line-break/>b")]]), xlsx_cell_values([["a\nb"]]))
        self.assertEqual(ods_cell_values([[("xml", "a<text:span>b</text:span>c")]]), xlsx_cell_values([["abc"]]))

    def test_chinese_text_roundtrip(self):
        self.assertEqual(ods_cell_values([["轉換公司債(無擔保)"]]), xlsx_cell_values([["轉換公司債(無擔保)"]]))


class TestNumberText(unittest.TestCase):
    def test_integers_have_no_point_or_exponent(self):
        for raw, want in [("88888888", "88888888"), ("88888888.0", "88888888"), ("1.1803517460E10", "11803517460"),
                          ("11803517460.000", "11803517460"), ("0", "0"), ("-5", "-5"), ("1E+3", "1000")]:
            with self.subTest(raw=raw):
                self.assertEqual(fs.ods_number_text(raw), want)

    def test_non_integers_use_shortest_plain_decimal_like_excel(self):
        for raw, want in [("5.6", "5.6"), ("5.60", "5.6"), ("33.18", "33.18"), ("0.5", "0.5"), ("1.25E1", "12.5"), ("1E-2", "0.01")]:
            with self.subTest(raw=raw):
                self.assertEqual(fs.ods_number_text(raw), want)

    def test_matches_xlsx_numeric_text_for_ordinary_values(self):
        raws = ["11803517460", "5.6", "12.5", "33.18", "80", "31.25"]
        x = xlsx_cell_values([[("num", r) for r in raws]])[0]
        o = ods_cell_values([[("numraw", r) for r in raws]])[0]
        self.assertEqual(o, x)

    def test_garbage_rejected(self):
        for raw in ["abc", "", "NaN", "Infinity"]:
            with self.subTest(raw=raw), self.assertRaises(fs.FscError):
                fs.ods_number_text(raw)

    def test_unsupported_cell_types_rejected_without_content(self):
        for vt, v in [("date", "2026-01-09"), ("boolean", "true"), ("time", "PT1H"), ("currency", "5"), ("percentage", "0.5")]:
            with self.subTest(vt=vt), self.assertRaises(fs.FscError) as cm:
                fs.ods_to_xsheets(fs.read_ods(make_ods([("S", [[("type", vt, v)]])])))
            self.assertNotIn(v, str(cm.exception)) if vt != "currency" else None


def page(links115, links114=None):
    return page_html([("114年度申報案件", links114 or [("https://www.fsc.gov.tw/f/114.xlsx", "EXCEL")]), (L115, links115)])


class TestSourceSelection(unittest.TestCase):
    def pick(self, links115, **kw):
        return fs.resolve_year_source(page(links115), PAGE, L115, **kw)

    def test_xlsx_preferred_over_ods(self):
        c = self.pick([("https://www.fsc.gov.tw/f/a.ods", "ODS"), ("https://www.fsc.gov.tw/f/a.xlsx", "EXCEL"), ("https://www.fsc.gov.tw/f/a.xls", "x")])
        self.assertEqual((c.fmt, c.ext, c.filename), ("xlsx", ".xlsx", "a.xlsx"))

    def test_ods_when_no_xlsx_and_xls_ignored(self):
        c = self.pick([("https://www.fsc.gov.tw/f/a.xls", "EXCEL"), ("https://www.fsc.gov.tw/f/a.ods", "ODS")])
        self.assertEqual((c.fmt, c.ext, c.filename, c.anchor_text), ("ods", ".ods", "a.ods", "ODS"))

    def test_only_xls_fails(self):
        with self.assertRaises(fs.FscError) as cm:
            self.pick([("https://www.fsc.gov.tw/f/a.xls", "EXCEL")])
        self.assertIn("沒有 .xlsx 也沒有 .ods", str(cm.exception))
        self.assertTrue(cm.exception.diagnostics)

    def test_multiple_distinct_ods_fail_but_duplicate_same_url_ok(self):
        with self.assertRaises(fs.FscError) as cm:
            self.pick([("https://www.fsc.gov.tw/f/a.ods", "1"), ("https://www.fsc.gov.tw/f/b.ods", "2")])
        self.assertIn("2 個不同的 .ods", str(cm.exception))
        c = self.pick([("https://www.fsc.gov.tw/f/a.ods", "1"), ("https://www.fsc.gov.tw/f/a.ods", "2")])
        self.assertEqual(c.fmt, "ods")

    def test_multiple_xlsx_still_fail_and_do_not_fall_back_to_ods(self):
        with self.assertRaises(fs.FscError) as cm:
            self.pick([("https://www.fsc.gov.tw/f/a.xlsx", "1"), ("https://www.fsc.gov.tw/f/b.xlsx", "2"), ("https://www.fsc.gov.tw/f/a.ods", "ODS")])
        self.assertIn("xlsx", str(cm.exception))
        self.assertNotIsInstance(cm.exception, fs.NoXlsxLink)

    def test_xlsx_on_wrong_host_fails_and_does_not_fall_back(self):
        with self.assertRaises(fs.FscError):
            self.pick([("https://evil.example.com/a.xlsx", "x"), ("https://www.fsc.gov.tw/f/a.ods", "ODS")])

    def test_ods_offsite_or_http_rejected(self):
        for href in ("https://evil.example.com/a.ods", "http://www.fsc.gov.tw/f/a.ods"):
            with self.subTest(href=href), self.assertRaises(fs.FscError):
                self.pick([(href, "ODS")])

    def test_missing_or_duplicate_row_fail(self):
        with self.assertRaises(fs.FscError):
            fs.resolve_year_source(page_html([("114年度申報案件", [("/a.ods", "x")])]), PAGE, L115)
        dup = page_html([(L115, [("https://www.fsc.gov.tw/a.ods", "x")]), (L115, [("https://www.fsc.gov.tw/b.ods", "y")])])
        with self.assertRaises(fs.FscError):
            fs.resolve_year_source(dup, PAGE, L115)

    def test_ods_link_with_extension_in_query_is_found(self):
        c = self.pick([("https://www.fsc.gov.tw/dl?file=a/b.ods&x=1", "ODS")])
        self.assertEqual(c.fmt, "ods")


if __name__ == "__main__":
    unittest.main()
