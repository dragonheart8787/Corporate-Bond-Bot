"""證期局申報案件彙總表：頁面連結解析、xlsx 讀取（stdlib）、唯讀探測的測試。全部離線，
xlsx 與 HTML 都是測試裡用 zipfile／字串合成的樣本（內容為虛構，欄位依使用者提供的 18 欄規格）。"""

import contextlib
import io
import json
import os
import re
import sys
import unittest
import urllib.error
import zipfile
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import fsc_sources as fs  # noqa: E402
import probe_fsc as pf  # noqa: E402
import probe_sources as ps  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SENTINELS = ["機密公司甲", "機密承銷商乙", "9876", "88888888"]


# ─────────────────────────────────────────
# 合成樣本
# ─────────────────────────────────────────
def col_letter(i):
    s, n = "", i + 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def make_xlsx(sheets, *, doctype=False, extra_members=None):
    """sheets: [(name, rows)]；rows 的元素：str | int | None | ("link", text, url) | ("flink", text, url) | ("loc", text, location)"""
    sst, sst_idx = [], {}

    def s_index(t):
        if t not in sst_idx:
            sst_idx[t] = len(sst)
            sst.append(t)
        return sst_idx[t]

    wb_sheets, wb_rels, files = [], [], {}
    for n, (name, rows) in enumerate(sheets, 1):
        wb_sheets.append(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>')
        wb_rels.append(f'<Relationship Id="rId{n}" Type="x" Target="worksheets/sheet{n}.xml"/>')
        rows_xml, links, rels = [], [], []
        for ri, row in enumerate(rows, 1):
            cells = []
            for ci, v in enumerate(row):
                ref = f"{col_letter(ci)}{ri}"
                if v is None:
                    continue
                if isinstance(v, tuple):
                    kind, text, target = v
                    if kind == "link":
                        rid = f"rId{len(rels) + 1}"
                        rels.append(f'<Relationship Id="{rid}" Type="h" Target="{escape(target)}" TargetMode="External"/>')
                        links.append(f'<hyperlink ref="{ref}" r:id="{rid}"/>')
                        cells.append(f'<c r="{ref}" t="s"><v>{s_index(text)}</v></c>')
                    elif kind == "loc":
                        links.append(f'<hyperlink ref="{ref}" location="{escape(target)}"/>')
                        cells.append(f'<c r="{ref}" t="s"><v>{s_index(text)}</v></c>')
                    else:
                        cells.append(f'<c r="{ref}" t="str"><f>HYPERLINK("{escape(target)}","{escape(text)}")</f><v>{escape(text)}</v></c>')
                elif isinstance(v, int):
                    cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="s"><v>{s_index(v)}</v></c>')
            rows_xml.append(f'<row r="{ri}">{"".join(cells)}</row>')
        hl = f'<hyperlinks>{"".join(links)}</hyperlinks>' if links else ""
        files[f"xl/worksheets/sheet{n}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheetData>{"".join(rows_xml)}</sheetData>{hl}</worksheet>')
        if rels:
            files[f"xl/worksheets/_rels/sheet{n}.xml.rels"] = (
                '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'{"".join(rels)}</Relationships>')
    dt = '<!DOCTYPE lolz [<!ENTITY a "aaaa">]>' if doctype else ""
    files["xl/workbook.xml"] = (
        f'<?xml version="1.0" encoding="UTF-8"?>{dt}<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{"".join(wb_sheets)}</sheets></workbook>')
    files["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(wb_rels)}</Relationships>')
    files["xl/sharedStrings.xml"] = (
        '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        + "".join(f'<si><t xml:space="preserve">{escape(t)}</t></si>' for t in sst) + "</sst>")
    files.update(extra_members or {})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


HEADER = list(fs.EXPECTED_COLUMNS)


def data_row(code="9876", company="機密公司甲", underwriter="機密承銷商乙", category="現金增資", closing="已生效", amount=88888888,
             received=1150109, other_dates=None, nature="申報"):
    d = other_dates or {}
    row = [code, "上市", closing, company, underwriter, category, amount, "新台幣", 10,
           received, d.get("auto"), d.get("stop"), d.get("lift"), d.get("effective"), d.get("revoke"), d.get("withdraw"), d.get("reject"), nature]
    return row


def good_xlsx(rows=None, title_rows=1, sheet_name="115年度申報案件"):
    body = [["115年度申報案件辦理情形彙總表"] + [None] * 17 for _ in range(title_rows)] + [HEADER] + (rows if rows is not None else [data_row(), data_row(code="1111")])
    return make_xlsx([(sheet_name, body)])


def page_html(rows):
    """rows: [(label, [(href, text), ...])]"""
    trs = []
    for label, links in rows:
        a = "".join(f'<a href="{h}">{t}</a>' for h, t in links)
        trs.append(f"<tr><td>{label}</td><td>{a}</td></tr>")
    return f"<html><body><p>更新日期：2026-10-08</p><table>{''.join(trs)}</table></body></html>"


PAGE_URL = fs.PAGE_URL
GOOD_ROWS = [
    ("114年度申報案件", [("https://www.fsc.gov.tw/userfiles/file/1141231v2.ods", "ods"), ("https://www.fsc.gov.tw/userfiles/file/1141231v2.xlsx", "114.12.31(v2)")]),
    ("115年度申報案件", [("https://www.fsc.gov.tw/userfiles/file/1151008v3.ods", "ods"), ("https://www.fsc.gov.tw/userfiles/file/1151008v3.xlsx", "115.10.8(v3)")]),
    ("115年度公開發行公司無償配發新股申報生效案件彙總表", [("https://www.fsc.gov.tw/userfiles/file/other.xlsx", "x")]),
]


# ─────────────────────────────────────────
# 假連線
# ─────────────────────────────────────────
class FakeHeaders(dict):
    def get(self, k, d=None):
        for key, v in self.items():
            if key.lower() == k.lower():
                return v
        return d


class FakeResp:
    def __init__(self, body=b"", status=200, ctype="text/html"):
        self._body, self.status, self.headers = body, status, FakeHeaders({"Content-Type": ctype})

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RoutedOpener:
    def __init__(self, routes):
        self.routes, self.requests = routes, []

    def open(self, req, timeout=None):
        self.requests.append((req.full_url, req.get_method(), dict(req.header_items())))
        item = self.routes.get(req.full_url)
        if item is None:
            raise urllib.error.URLError(f"no route {req.full_url}")
        if isinstance(item, Exception):
            raise item
        return item


def run_probe(routes, env=None):
    op = RoutedOpener(routes)
    out, sleeps = io.StringIO(), []
    with contextlib.redirect_stdout(out):
        code = pf.run({"PROBE_REPO": "o/r", **(env or {})}, opener=op, sleep=sleeps.append)
    return code, out.getvalue(), op, sleeps


URL115 = "https://www.fsc.gov.tw/userfiles/file/1151008v3.xlsx"
URL114 = "https://www.fsc.gov.tw/userfiles/file/1141231v2.xlsx"


def happy_routes(x115=None, x114=None):
    return {
        PAGE_URL: FakeResp(page_html(GOOD_ROWS).encode("utf-8")),
        URL115: FakeResp(x115 or good_xlsx(), ctype="application/octet-stream"),
        URL114: FakeResp(x114 or good_xlsx(sheet_name="114年度申報案件"), ctype="application/octet-stream"),
    }


# ─────────────────────────────────────────
# 頁面連結解析
# ─────────────────────────────────────────
class TestResolveYearLink(unittest.TestCase):
    def resolve(self, rows, label="115年度申報案件"):
        return fs.resolve_year_link(page_html(rows), PAGE_URL, label)

    def test_picks_the_exact_label_row_and_its_xlsx_not_ods(self):
        ch = self.resolve(GOOD_ROWS)
        self.assertEqual(ch.url, URL115)
        self.assertEqual(ch.filename, "1151008v3.xlsx")
        self.assertEqual(ch.anchor_text, "115.10.8(v3)")

    def test_other_year_row(self):
        self.assertEqual(self.resolve(GOOD_ROWS, "114年度申報案件").url, URL114)

    def test_substring_labels_are_never_matched(self):
        rows = [("115年度公開發行公司無償配發新股申報生效案件彙總表", [("https://www.fsc.gov.tw/a.xlsx", "x")]),
                ("115年度申報案件(補充)", [("https://www.fsc.gov.tw/b.xlsx", "x")]),
                ("前115年度申報案件", [("https://www.fsc.gov.tw/c.xlsx", "x")])]
        with self.assertRaises(fs.FscError) as cm:
            self.resolve(rows)
        self.assertIn("找不到", str(cm.exception))

    def test_surrounding_whitespace_is_ignored_but_inner_differences_are_not_exact(self):
        for cell in ("\xa0115年度申報案件\n", "  115年度申報案件  ", "\n\t115年度申報案件"):
            rows = [(cell, [("https://www.fsc.gov.tw/a.xlsx", "x")])]
            self.assertEqual(self.resolve(rows).filename, "a.xlsx", repr(cell))
        for cell in ("115年度\xa0申報案件", " 115 年度申報案件 ", "115年度 申報案件"):
            with self.assertRaises(fs.FscError, msg=repr(cell)):        # 內部有空白 = 不是完全等於
                self.resolve([(cell, [("https://www.fsc.gov.tw/a.xlsx", "x")])])

    def test_filename_version_is_not_hardcoded(self):
        for name in ("1151008v3.xlsx", "1151120.xlsx", "x_v10.XLSX"):
            rows = [("115年度申報案件", [(f"https://www.fsc.gov.tw/userfiles/file/{name}", "t")])]
            self.assertEqual(self.resolve(rows).filename, name)

    def test_relative_and_query_links_resolved_against_page(self):
        rows = [("115年度申報案件", [("/userfiles/file/1151008v3.xlsx?v=2", "t")])]
        self.assertEqual(self.resolve(rows).url, "https://www.sfb.gov.tw/userfiles/file/1151008v3.xlsx?v=2")

    def test_no_xlsx_in_the_row_is_an_error(self):
        rows = [("115年度申報案件", [("https://www.fsc.gov.tw/a.ods", "ods"), ("https://www.fsc.gov.tw/a.pdf", "pdf")])]
        with self.assertRaises(fs.FscError) as cm:
            self.resolve(rows)
        self.assertIn("沒有 .xlsx", str(cm.exception))
        self.assertTrue(any("連結副檔名" in d for d in cm.exception.diagnostics))

    def test_two_different_xlsx_candidates_is_an_error(self):
        rows = [("115年度申報案件", [("https://www.fsc.gov.tw/a.xlsx", "a"), ("https://www.fsc.gov.tw/b.xlsx", "b")])]
        with self.assertRaises(fs.FscError) as cm:
            self.resolve(rows)
        self.assertIn("2 個不同的 .xlsx", str(cm.exception))

    def test_same_xlsx_linked_twice_is_one_candidate(self):
        rows = [("115年度申報案件", [("https://www.fsc.gov.tw/a.xlsx", "a"), ("https://www.fsc.gov.tw/a.xlsx", "again")])]
        self.assertEqual(self.resolve(rows).filename, "a.xlsx")

    def test_two_rows_with_the_exact_label_is_an_error(self):
        rows = [("115年度申報案件", [("https://www.fsc.gov.tw/a.xlsx", "a")]), ("115年度申報案件", [("https://www.fsc.gov.tw/b.xlsx", "b")])]
        with self.assertRaises(fs.FscError) as cm:
            self.resolve(rows)
        self.assertIn("2 列", str(cm.exception))

    def test_host_and_scheme_are_enforced(self):
        for href, word in (("https://evil.example.com/a.xlsx", "白名單"), ("http://www.fsc.gov.tw/a.xlsx", "https")):
            with self.assertRaises(fs.FscError, msg=href) as cm:
                self.resolve([("115年度申報案件", [(href, "t")])])
            self.assertIn(word, str(cm.exception))

    def test_page_without_tables_gives_diagnostics(self):
        with self.assertRaises(fs.FscError) as cm:
            fs.resolve_year_link("<html><div>115年度申報案件</div></html>", PAGE_URL, "115年度申報案件")
        self.assertTrue(any("0 個表格" in d for d in cm.exception.diagnostics))

    def test_update_text(self):
        self.assertEqual(fs.page_update_text(page_html(GOOD_ROWS)), "2026-10-08")
        self.assertEqual(fs.page_update_text("<p>沒有</p>"), "")


# 真實頁面的形狀：分類格跨多列（rowspan），所以「115年度申報案件」那一列的儲存格比其他列少，
# 而且它的「壓縮檔」欄是空的。以下是合成樣本，重現這個形狀。
ROWSPAN_PAGE = """
<html><body><p>更新日期：2026-10-08</p>
<table border="1">
<tr><th>類別</th><th>年度</th><th>ODS</th><th>XLSX</th><th>壓縮檔</th></tr>
<tr>
  <td rowspan="3">公開發行公司申報案件</td>
  <td>114年度申報案件</td>
  <td><a href="/userfiles/file/1141231v2.ods">ods</a></td>
  <td><a href="/userfiles/file/1141231v2.xlsx">114.12.31(v2)</a></td>
  <td><a href="/userfiles/file/114.zip">zip</a></td>
</tr>
<tr>
  <td>115年度申報案件</td>
  <td><a href="/userfiles/file/1151008v3.ods">ods</a></td>
  <td><a href="/userfiles/file/1151008v3.xlsx">115.10.8(v3)</a></td>
  <td></td>
</tr>
<tr>
  <td>115年度公開發行公司無償配發新股申報生效案件彙總表</td>
  <td><a href="/userfiles/file/free.ods">ods</a></td>
  <td><a href="/userfiles/file/free.xlsx">x</a></td>
  <td>&nbsp;</td>
</tr>
<tr>
  <td rowspan="2">外國發行人案件</td>
  <td>114年度外國發行人申報案件</td>
  <td></td><td><a href="/userfiles/file/foreign114.xlsx">x</a></td><td></td>
</tr>
<tr><td>115年度外國發行人申報案件</td><td></td><td><a href="/userfiles/file/foreign115.xlsx">x</a></td><td></td></tr>
</table></body></html>
"""


class TestRowspanAndSloppyHtml(unittest.TestCase):
    def resolve(self, html, label):
        return fs.resolve_year_link(html, PAGE_URL, label)

    def test_row_with_fewer_cells_and_empty_trailing_cell_is_found_by_label_text(self):
        ch = self.resolve(ROWSPAN_PAGE, "115年度申報案件")
        self.assertEqual(ch.url, "https://www.sfb.gov.tw/userfiles/file/1151008v3.xlsx")
        self.assertEqual(ch.anchor_text, "115.10.8(v3)")

    def test_cell_counts_really_differ_between_rows(self):
        rows = {r.cells[0].text: r for r in fs.parse_page(ROWSPAN_PAGE).rows if r.cells}
        r114 = [r for r in fs.parse_page(ROWSPAN_PAGE).rows if r.cells and r.cells[0].text == "公開發行公司申報案件"][0]
        r115 = rows["115年度申報案件"]
        self.assertEqual(len(r114.cells), 5)
        self.assertEqual(len(r115.cells), 4)                      # 少了跨列的分類格
        self.assertEqual(r114.cells[0].rowspan, 3)
        self.assertEqual([c.text for c in r115.cells][-1], "")    # 最後一欄是空的

    def test_the_114_row_is_found_even_though_its_first_cell_is_the_rowspan_category(self):
        ch = self.resolve(ROWSPAN_PAGE, "114年度申報案件")
        self.assertEqual(ch.filename, "1141231v2.xlsx")             # 不是同列的 zip，也不是別列的

    def test_never_borrows_a_link_from_a_neighbouring_row(self):
        # 115 那列沒有 xlsx：不能因為鄰列有 xlsx（欄位位置相同）就拿來用
        html = ROWSPAN_PAGE.replace('<td><a href="/userfiles/file/1151008v3.xlsx">115.10.8(v3)</a></td>', "<td></td>")
        with self.assertRaises(fs.FscError) as cm:
            self.resolve(html, "115年度申報案件")
        self.assertIn("沒有 .xlsx", str(cm.exception))

    def test_similar_labels_in_other_sections_are_not_candidates(self):
        # 頁面上另有「115年度外國發行人申報案件」等相似標籤：只有完全相等的那一列算
        self.assertEqual(self.resolve(ROWSPAN_PAGE, "115年度申報案件").filename, "1151008v3.xlsx")
        self.assertEqual(self.resolve(ROWSPAN_PAGE, "115年度外國發行人申報案件").filename, "foreign115.xlsx")
        renamed = ROWSPAN_PAGE.replace("115年度申報案件", "115年度申報案件（補）", 1)
        with self.assertRaises(fs.FscError) as cm:
            self.resolve(renamed, "115年度申報案件")
        self.assertIn("找不到", str(cm.exception))

    def test_xlsx_link_in_any_column_position_is_found(self):
        for pos in range(1, 5):
            cells = ["<td></td>"] * 4
            cells[pos - 1] = '<td><a href="/f/x.xlsx">t</a></td>'
            html = "<table><tr><td rowspan='2'>分類</td><td>115年度申報案件</td>" + "".join(cells) + "</tr></table>"
            self.assertEqual(self.resolve(html, "115年度申報案件").filename, "x.xlsx", pos)

    def test_unclosed_td_and_tr_tags(self):
        html = ("<table>"
                "<tr><td rowspan=3>公開發行公司申報案件<td>114年度申報案件<td><a href='/a/114.xlsx'>114</a><td>"
                "<tr><td>115年度申報案件<td><a href='/a/115.ods'>o</a><td><a href='/a/115v3.xlsx'>115</a><td>"
                "<tr><td>115年度其他<td><a href='/a/other.xlsx'>x</a>"
                "</table>")
        self.assertEqual(self.resolve(html, "115年度申報案件").filename, "115v3.xlsx")
        self.assertEqual(self.resolve(html, "114年度申報案件").filename, "114.xlsx")

    def test_td_without_tr_and_stray_closing_tags_do_not_crash(self):
        html = "</td></tr></table><table><td>115年度申報案件<td><a href='/x.xlsx'>x</a></table></tr>"
        self.assertEqual(self.resolve(html, "115年度申報案件").filename, "x.xlsx")

    def test_uppercase_tags_attributes_and_whitespace_in_href(self):
        html = "<TABLE><TR><TD>115年度申報案件</TD><TD><A HREF=' /f/Y.XLSX '>t</A></TD></TR></TABLE>"
        self.assertEqual(self.resolve(html, "115年度申報案件").filename, "Y.XLSX")

    def test_nested_table_inside_a_cell_does_not_break_outer_rows(self):
        html = ("<table><tr><td>說明<table><tr><td>內層</td></tr></table></td><td>x</td></tr>"
                "<tr><td>115年度申報案件</td><td><a href='/n.xlsx'>t</a></td></tr></table>")
        self.assertEqual(self.resolve(html, "115年度申報案件").filename, "n.xlsx")

    def test_nested_table_keeps_outer_row_cells_intact(self):
        html = ("<table><tr><td>說明<table><tr><td>內層</td></tr></table></td><td>x</td></tr>"
                "<tr><td>115年度申報案件</td><td><a href='/n.xlsx'>t</a></td></tr></table>")
        rows = fs.parse_page(html).rows
        outer = [r for r in rows if len(r.cells) == 2 and r.cells[1].text == "x"]
        self.assertEqual(len(outer), 1)                           # 外層列沒有被內層的 <tr> 截斷
        self.assertIn("說明", outer[0].cells[0].text)
        self.assertTrue(any(len(r.cells) == 1 and r.cells[0].text == "內層" for r in rows))

    def test_rows_without_closing_tags_do_not_leak_across_tables(self):
        html = "<table><tr><td>A<td>B</table><table><tr><td>115年度申報案件<td><a href='/x.xlsx'>t</table>"
        rows = fs.parse_page(html).rows
        self.assertEqual([[c.text for c in r.cells] for r in rows], [["A", "B"], ["115年度申報案件", "t"]])
        self.assertEqual(self.resolve(html, "115年度申報案件").filename, "x.xlsx")

    def test_page_truncated_in_the_middle_of_the_last_row_is_still_parsed(self):
        html = "<table><tr><td>115年度申報案件<td><a href='/x.xlsx'>t"
        self.assertEqual(self.resolve(html, "115年度申報案件").filename, "x.xlsx")

    def test_link_outside_any_table_is_ignored(self):
        html = "<a href='/loose.xlsx'>loose</a><table><tr><td>115年度申報案件</td><td></td></tr></table>"
        with self.assertRaises(fs.FscError):
            self.resolve(html, "115年度申報案件")

    def test_diagnostics_describe_the_matched_row_shape(self):
        html = ROWSPAN_PAGE.replace('<a href="/userfiles/file/1151008v3.xlsx">115.10.8(v3)</a>', "")
        with self.assertRaises(fs.FscError) as cm:
            self.resolve(html, "115年度申報案件")
        self.assertTrue(any("目標列有 4 個儲存格" in d and "空白" in d for d in cm.exception.diagnostics))

    def test_probe_over_a_rowspan_page_requests_the_two_resolved_files(self):
        u115 = "https://www.sfb.gov.tw/userfiles/file/1151008v3.xlsx"
        u114 = "https://www.sfb.gov.tw/userfiles/file/1141231v2.xlsx"
        routes = {PAGE_URL: FakeResp(ROWSPAN_PAGE.encode("utf-8")),
                  u115: FakeResp(good_xlsx(), ctype="application/octet-stream"),
                  u114: FakeResp(good_xlsx(), ctype="application/octet-stream")}
        code, out, op, _ = run_probe(routes)
        self.assertEqual(code, 0, out)
        self.assertEqual([r[0] for r in op.requests], [PAGE_URL, u115, u114])
        self.assertNotIn("::error", out)


class TestPageUrlForm(unittest.TestCase):
    def test_default_page_url_uses_the_pages_own_percent_encoded_form(self):
        self.assertEqual(fs.PAGE_URL, "https://www.sfb.gov.tw/ch/home.jsp?id=1016&parentpath=0%2C6%2C52")
        self.assertNotIn(",", fs.PAGE_URL)
        self.assertTrue(ps.validate_url(fs.PAGE_URL, fs.FSC_HOSTS)[0])

    def test_probe_requests_exactly_that_url_and_a_redirect_on_it_is_a_failure_not_followed(self):
        code, out, op, _ = run_probe(happy_routes())
        self.assertEqual(op.requests[0][0], "https://www.sfb.gov.tw/ch/home.jsp?id=1016&parentpath=0%2C6%2C52")
        h = FakeHeaders({"Location": "https://www.sfb.gov.tw/ch/home.jsp?id=1016&parentpath=0%2C6%2C52"})
        err = urllib.error.HTTPError(PAGE_URL, 301, "Moved", h, io.BytesIO(b""))
        code, out, op, _ = run_probe({PAGE_URL: err})
        self.assertEqual(code, 1)
        self.assertEqual(len(op.requests), 1)                    # 轉址沒有被跟隨
        self.assertIn("被轉址", out)

    def test_workflow_default_is_the_same_encoded_url(self):
        text = open(os.path.join(ROOT, ".github", "workflows", "probe-fsc.yml"), encoding="utf-8").read()
        self.assertIn("parentpath=0%2C6%2C52", text)
        self.assertNotIn("parentpath=0,6,52", text)


class TestReadXlsx(unittest.TestCase):
    def test_roundtrip_strings_numbers_and_sheet_names(self):
        sheets = fs.read_xlsx(good_xlsx())
        self.assertEqual([s.name for s in sheets], ["115年度申報案件"])
        h = fs.find_header(sheets[0])
        self.assertEqual(h, 1)
        self.assertEqual([fs.norm_label(c.value) for c in sheets[0].rows[h]], fs.EXPECTED_COLUMNS)
        row = sheets[0].rows[h + 1]
        self.assertEqual((row[0].value, row[0].kind), ("9876", "str"))
        self.assertEqual((row[9].value, row[9].kind), ("1150109", "num"))       # 日期是數字型儲存格也要看得出來
        self.assertEqual(row[10].kind, "empty")

    def test_multiple_sheets_and_header_not_in_first_row(self):
        data = make_xlsx([("說明", [["只是說明"]]), ("資料", [["標題"], [], ["x"], HEADER, data_row()])])
        sheets = fs.read_xlsx(data)
        self.assertEqual([s.name for s in sheets], ["說明", "資料"])
        self.assertIsNone(fs.find_header(sheets[0]))
        self.assertEqual(fs.find_header(sheets[1]), 3)

    def test_header_with_newline_and_spaces_is_normalised(self):
        hdr = list(HEADER)
        hdr[0] = "證券\n代號 "
        sheets = fs.read_xlsx(make_xlsx([("s", [hdr, data_row()])]))
        self.assertEqual(fs.find_header(sheets[0]), 0)

    def test_hyperlinks_external_internal_and_formula(self):
        rows = [HEADER, data_row(received=("link", "1150109", "https://www.fsc.gov.tw/doc/1.pdf")),
                data_row(received=("loc", "1150110", "Sheet2!A1")),
                data_row(received=("flink", "1150111", "https://other.example.org/x"))]
        sheets = fs.read_xlsx(make_xlsx([("s", rows)]))
        r = sheets[0].rows
        self.assertEqual((r[1][9].link, r[1][9].link_kind, r[1][9].value), ("https://www.fsc.gov.tw/doc/1.pdf", "external", "1150109"))
        self.assertEqual((r[2][9].link, r[2][9].link_kind), ("Sheet2!A1", "internal"))
        self.assertEqual((r[3][9].link, r[3][9].link_kind), ("https://other.example.org/x", "formula"))
        self.assertIsNone(r[1][8].link)

    def test_inline_strings_and_rich_text_and_phonetic_text(self):
        extra = {
            "xl/sharedStrings.xml": '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                                    '<si><r><t>甲</t></r><r><t>乙</t></r><rPh><t>ㄅㄆ</t></rPh></si></sst>',
        }
        base = make_xlsx([("s", [["x"]])])
        # 換掉 sharedStrings，並讓 A1 指向第 0 個字串
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(base)) as zin, zipfile.ZipFile(buf, "w") as zout:
            for n in zin.namelist():
                zout.writestr(n, extra.get(n, zin.read(n)))
        sheets = fs.read_xlsx(buf.getvalue())
        self.assertEqual(sheets[0].rows[0][0].value, "甲乙")                     # 注音不算內容

    def test_corrupt_and_hostile_files_fail_explicitly(self):
        for blob, word in ((b"not a zip", "損毀"), (b"", "損毀")):
            with self.assertRaises(fs.FscError, msg=blob) as cm:
                fs.read_xlsx(blob)
            self.assertIn(word, str(cm.exception))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("hello.txt", "x")
        with self.assertRaises(fs.FscError) as cm:
            fs.read_xlsx(buf.getvalue())
        self.assertIn("workbook", str(cm.exception))
        with self.assertRaises(fs.FscError) as cm:
            fs.read_xlsx(make_xlsx([("s", [["x"]])], doctype=True))
        self.assertIn("DTD", str(cm.exception))

    def test_zip_bomb_limits(self):
        old = fs.MAX_UNCOMPRESSED
        fs.MAX_UNCOMPRESSED = 100
        try:
            with self.assertRaises(fs.FscError) as cm:
                fs.read_xlsx(good_xlsx())
        finally:
            fs.MAX_UNCOMPRESSED = old
        self.assertIn("過大", str(cm.exception))

    def test_workbook_pointing_at_a_missing_sheet_part_is_an_error(self):
        base = make_xlsx([("s", [["x"]])])
        buf = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(base)) as zin, zipfile.ZipFile(buf, "w") as zout:
            for n in zin.namelist():
                if n != "xl/worksheets/sheet1.xml":
                    zout.writestr(n, zin.read(n))
        with self.assertRaises(fs.FscError) as cm:
            fs.read_xlsx(buf.getvalue())
        self.assertIn("不存在", str(cm.exception))

    def test_xlsx_with_no_sheets_is_an_error(self):
        data = make_xlsx([])
        with self.assertRaises(fs.FscError) as cm:
            fs.read_xlsx(data)
        self.assertIn("沒有任何工作表", str(cm.exception))


# ─────────────────────────────────────────
# 樣態分類
# ─────────────────────────────────────────
class TestDateShapes(unittest.TestCase):
    def cls(self, value, kind="str"):
        return pf.classify_date_cell(fs.XCell(value=value, kind=kind if value != "" else "empty"))

    def test_roc7_string_and_number(self):
        self.assertEqual(self.cls("1150109"), ("7碼[字串]", ["2026-01-09"]))
        self.assertEqual(self.cls("1150109", "num"), ("7碼[數字]", ["2026-01-09"]))

    def test_comma_separated_multiple_dates(self):
        for raw in ("1150109,1150210", "1150109，1150210", "1150109、1150210", "1150109, 1150210", "1150109\n1150210"):
            lab, isos = self.cls(raw)
            self.assertEqual(lab, "逗號多值2個[字串]", raw)
            self.assertEqual(isos, ["2026-01-09", "2026-02-10"], raw)
        self.assertEqual(self.cls("1150109,1150210,1150311")[0], "逗號多值3個[字串]")

    def test_empty(self):
        self.assertEqual(self.cls(""), ("空值", []))
        self.assertEqual(self.cls("   ", "str"), ("空值", []))

    def test_nonexistent_dates_are_flagged_not_dropped(self):
        lab, isos = self.cls("1150230")
        self.assertEqual((lab, isos), ("7碼但日期不存在[字串]", []))
        lab, isos = self.cls("1150109,1151399")
        self.assertIn("含不存在日期", lab)
        self.assertEqual(isos, ["2026-01-09"])

    def test_other_shapes_are_reported_as_structure_only(self):
        lab, _ = self.cls("115/01/09")
        self.assertEqual(lab, "其他:9{3}/9{2}/9{2}[字串]")
        lab, _ = self.cls("已補正 1150109")
        self.assertTrue(lab.startswith("其他:C{3} 9{7}"))
        self.assertNotIn("補正", lab)
        lab, _ = self.cls("46000", "num")
        self.assertEqual(lab, "其他:9{5}[數字]")

    def test_fullwidth_digits(self):
        self.assertEqual(self.cls("１１５０１０９")[0], "7碼[字串]")


# ─────────────────────────────────────────
# 探測整體
# ─────────────────────────────────────────
class TestProbeOutput(unittest.TestCase):
    def rows(self):
        return [
            data_row(category="現金增資", closing="已生效", received=1150109, other_dates={"effective": "1150120"}),
            data_row(code="1111", category="現金增資", closing="已生效", received=("link", "1150110", "https://www.fsc.gov.tw/d/1.pdf"),
                     other_dates={"effective": "1150121,1150205", "withdraw": "115/02/01"}),
            data_row(code="2222", category="公司債", closing="撤回", received=1150111, other_dates={"withdraw": "1150115"}),
            data_row(code="3333", category="轉換公司債", closing="", received="", other_dates={"reject": "1150230"}),
        ]

    def test_happy_path_prints_all_requested_structure(self):
        code, out, op, sleeps = run_probe(happy_routes(good_xlsx(self.rows())))
        self.assertEqual(code, 0, out)
        self.assertIn("HTTP 狀態：200", out)
        self.assertIn("回應大小：", out)
        self.assertIn("工作表數量 1：「115年度申報案件」", out)
        self.assertIn("表頭在第 2 列，欄數 18", out)
        self.assertIn("與預期的 18 欄完全相同", out)
        self.assertIn("資料列數（表頭之後、非全空）：4", out)
        self.assertIn("案件類別（3 種）：現金增資×2；公司債×1；轉換公司債×1", out)
        self.assertIn("結案類型（3 種）", out)
        self.assertIn("已生效×2", out)
        self.assertIn("日期欄「生效日期」：", out)
        self.assertIn("逗號多值2個[字串]×1", out)
        self.assertIn("7碼[數字]×", out)
        self.assertIn("7碼但日期不存在[字串]×1", out)                      # 退件日期 1150230
        self.assertIn("日期範圍 2026-01-09", out)
        self.assertIn("收文日期", out)
        self.assertIn("www.fsc.gov.tw（external）×1", out)
        self.assertIn("探測完成：全部成功", out)

    def test_exactly_three_gets_at_least_6s_apart_and_project_user_agent(self):
        code, out, op, sleeps = run_probe(happy_routes())
        self.assertEqual(len(op.requests), 3)
        self.assertEqual([r[0] for r in op.requests], [PAGE_URL, URL115, URL114])
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(s >= 6 for s in sleeps))
        for url, method, headers in op.requests:
            self.assertEqual(method, "GET")
            self.assertIn("Corporate-Bond-Bot-probe-fsc", headers["User-agent"])
            self.assertNotIn("Authorization", headers)
        self.assertIn("spreadsheetml", op.requests[1][2]["Accept"])

    def test_no_content_is_ever_printed(self):
        code, out, op, sleeps = run_probe(happy_routes(good_xlsx(self.rows())))
        for s in SENTINELS:
            self.assertNotIn(s, out)
        self.assertNotIn("機密", out)
        for m in ("1150109,", "1150120"):
            self.assertNotIn(m, out)                                         # 原始日期字串也不輸出（只有樣態與範圍）

    def test_many_distinct_category_values_are_not_listed(self):
        rows = [data_row(code=str(i), category=f"類別{i}") for i in range(60)]
        code, out, *_ = run_probe(happy_routes(good_xlsx(rows)))
        self.assertIn("60 種不同的值（超過 40 種，不列出）", out)
        self.assertNotIn("類別7", out)

    def test_header_mismatch_is_reported(self):
        hdr = list(HEADER)
        hdr[3] = "公司全名"
        x = make_xlsx([("s", [hdr, data_row()])])
        code, out, *_ = run_probe(happy_routes(x))
        self.assertIn("與預期的 18 欄不同", out)
        self.assertIn("缺少 ['公司名稱']", out)

    def test_header_row_not_found_is_reported(self):
        x = make_xlsx([("s", [["a", "b"], ["c", "d"]])])
        code, out, *_ = run_probe(happy_routes(x))
        self.assertIn("找不到含「證券代號」的表頭列", out)

    def test_page_failure_stops_before_any_xlsx_request(self):
        for outcome in (urllib.error.HTTPError(PAGE_URL, 403, "e", FakeHeaders({}), io.BytesIO(b"")), urllib.error.URLError("dns")):
            code, out, op, _ = run_probe({PAGE_URL: outcome})
            self.assertEqual(code, 1)
            self.assertEqual(len(op.requests), 1)
            self.assertIn("::error title=探測失敗 頁面", out)

    def test_page_redirect_is_reported_not_followed(self):
        h = FakeHeaders({"Location": "https://www.sfb.gov.tw/ch/other.jsp"})
        err = urllib.error.HTTPError(PAGE_URL, 301, "Moved", h, io.BytesIO(b""))
        code, out, op, _ = run_probe({PAGE_URL: err})
        self.assertEqual(code, 1)
        self.assertIn("被轉址", out)
        self.assertEqual(len(op.requests), 1)

    def test_label_not_found_or_ambiguous_is_error_but_other_year_still_probed(self):
        rows = [r for r in GOOD_ROWS if r[0] != "115年度申報案件"]
        routes = {PAGE_URL: FakeResp(page_html(rows).encode("utf-8")), URL114: FakeResp(good_xlsx(), ctype="application/octet-stream")}
        code, out, op, _ = run_probe(routes)
        self.assertEqual(code, 1)
        self.assertIn("找不到儲存格文字完全等於「115年度申報案件」", out)
        self.assertIn("診斷：", out)
        self.assertEqual([r[0] for r in op.requests], [PAGE_URL, URL114])
        self.assertIn("::error title=探測失敗 115年度申報案件", out)

    def test_xlsx_http_error_and_corrupt_file_are_explicit(self):
        routes = happy_routes()
        routes[URL115] = urllib.error.HTTPError(URL115, 404, "nf", FakeHeaders({}), io.BytesIO(b""))
        routes[URL114] = FakeResp(b"not a zip", ctype="application/octet-stream")
        code, out, op, _ = run_probe(routes)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 404", out)
        self.assertIn("zip 結構損毀", out)
        self.assertEqual(len([l for l in out.splitlines() if l.startswith("::error")]), 2)
        self.assertEqual(len(op.requests), 3)                                # 沒有重試

    def test_html_instead_of_xlsx_is_an_error(self):
        routes = happy_routes()
        routes[URL115] = FakeResp(b"<html>blocked</html>", ctype="text/html")
        code, out, *_ = run_probe(routes)
        self.assertEqual(code, 1)
        self.assertIn("::error title=探測失敗 115年度申報案件", out)

    def test_hostile_page_url_is_rejected_before_any_request(self):
        for url in ("https://evil.example.com/x", "http://www.sfb.gov.tw/x", "https://www.sfb.gov.tw.evil.com/x"):
            code, out, op, _ = run_probe(happy_routes(), {"PROBE_FSC_PAGE_URL": url})
            self.assertEqual(code, 2, url)
            self.assertEqual(op.requests, [])
            self.assertIn("未送出任何請求", out)

    def test_dry_run_connects_to_nothing(self):
        code, out, op, sleeps = run_probe(happy_routes(), {"PROBE_DRY_RUN": "1"})
        self.assertEqual(code, 0)
        self.assertEqual(op.requests, [])
        self.assertEqual(sleeps, [])
        self.assertIn("DRY-RUN", out)

    def test_script_writes_nothing_but_the_step_summary(self):
        src = open(os.path.join(ROOT, "scripts", "probe_fsc.py"), encoding="utf-8").read()
        self.assertEqual(len(re.findall(r'open\([^)]*"a"', src)), 1)
        self.assertNotRegex(src, r'open\([^)]*"w"')
        for m in ("openpyxl", "pandas", "requests"):
            self.assertNotRegex(src, rf"(?m)^\s*(import|from)\s+{m}\b")
        self.assertNotIn("openpyxl", open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read())


@unittest.skipIf(yaml is None, "需要 PyYAML")
class TestWorkflowContract(unittest.TestCase):
    def setUp(self):
        self.text = open(os.path.join(ROOT, ".github", "workflows", "probe-fsc.yml"), encoding="utf-8").read()
        self.wf = yaml.safe_load(self.text)
        self.code = re.sub(r"(?m)^\s*#.*$", "", self.text)

    def test_manual_only_read_only_no_secrets(self):
        self.assertEqual(set(self.wf[True]), {"workflow_dispatch"})
        self.assertEqual(self.wf["permissions"], {"contents": "read"})
        for f in ("secrets.", "TELEGRAM", "git push", "git commit", "/dispatches", "gh api", "actions: write", "contents: write", "GITHUB_TOKEN"):
            self.assertNotIn(f, self.code, f)

    def test_checkout_without_credentials_and_inputs_via_env(self):
        steps = self.wf["jobs"]["probe"]["steps"]
        co = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")][0]
        self.assertIs(co["with"]["persist-credentials"], False)
        run = [s for s in steps if "probe_fsc.py" in s.get("run", "")][0]
        self.assertNotIn("${{", run["run"])
        self.assertEqual(set(run["env"]), {"PROBE_FSC_PAGE_URL", "PROBE_DRY_RUN", "PROBE_REPO"})

    def test_default_page_url_matches_script_and_is_allowlisted(self):
        d = self.wf[True]["workflow_dispatch"]["inputs"]["page_url"]["default"]
        self.assertEqual(d, fs.PAGE_URL)
        self.assertTrue(ps.validate_url(d, fs.FSC_HOSTS)[0])
        self.assertEqual(self.wf["concurrency"]["group"], "probe-fsc")


if __name__ == "__main__":
    unittest.main()
