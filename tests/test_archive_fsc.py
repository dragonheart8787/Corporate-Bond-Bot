"""證期局申報案件彙總表留存（scripts/archive_fsc.py 與 archive-fsc.yml）的測試。
全部離線：xlsx 與 HTML 都是合成樣本（虛構內容）、假連線物件、暫存目錄；socket 在本模組內被封鎖，真的連線會直接失敗。"""

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
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
import probe_sources as ps  # noqa: E402
from test_fsc_probe import (  # noqa: E402
    GOOD_ROWS, HEADER, PAGE_URL, URL114, URL115, FakeHeaders, FakeResp, RoutedOpener, data_row, make_xlsx, page_html,
)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

TAIPEI = timezone(timedelta(hours=8))
SENT = ["機密公司甲", "機密承銷商乙", "88888888"]
_real_socket = socket.socket


def setUpModule():
    def blocked(*a, **k):
        raise AssertionError("測試不得建立真的網路連線")
    socket.socket = blocked  # type: ignore[assignment]


def tearDownModule():
    socket.socket = _real_socket  # type: ignore[assignment]


# ─────────────────────────────────────────
# 樣本
# ─────────────────────────────────────────
TRAILER = [["註：本表資料以申報當日為準。"], ["資料來源：證期局"]]


_XLSX_CACHE = {}


def xlsx(rows, *, trailer=TRAILER, sheet="IMP0080A_Upload", header=HEADER, extra_sheets=()):
    """同樣的參數一定回傳同樣的位元組（zipfile 會寫入當下時間，不快取的話跨秒的兩次呼叫 sha 會不同，等於「伺服器換了檔」）。"""
    key = repr((rows, trailer, sheet, header, extra_sheets))
    if key not in _XLSX_CACHE:
        _XLSX_CACHE[key] = _build_xlsx(rows, trailer, sheet, header, extra_sheets)
    return _XLSX_CACHE[key]


def _build_xlsx(rows, trailer, sheet, header, extra_sheets):
    body = [["115年度申報案件辦理情形彙總表"] + [None] * 17, [None], header] + rows + trailer
    return make_xlsx([(sheet, body)] + list(extra_sheets))


def base_rows():
    return [
        data_row(code="1111", category="現金增資", closing="已生效", amount=100, received=1150105, other_dates={"effective": 1150120}),
        data_row(code="2222", category="公司債", closing="審查中", amount=200, received=1150106),
        data_row(code="3333", category="海外存託憑證", closing="已撤回", amount=300, received=1150107, other_dates={"withdraw": 1150110}),
        data_row(code="4444", category="現金增資", closing="退件", amount=400, received=1150108, other_dates={"reject": 1150111}),
    ]


def page_with(text="2026-10-08", rows=None):
    return page_html(rows or GOOD_ROWS).replace("2026-10-08", text).encode("utf-8")


def routes(x115=None, x114=None, page=None):
    return {
        PAGE_URL: FakeResp(page or page_with()),
        URL115: FakeResp(x115 if x115 is not None else xlsx(base_rows()), ctype="application/octet-stream"),
        URL114: FakeResp(x114 if x114 is not None else xlsx(base_rows()[:3]), ctype="application/octet-stream"),
    }


def http_error(url, code, loc=None):
    h = FakeHeaders({"Location": loc} if loc else {})
    return urllib.error.HTTPError(url, code, "x", h, io.BytesIO(b""))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "fsc")
        self.summary = os.path.join(self.tmp, "summary.md")
        self.clock = datetime(2026, 10, 12, 9, 0, tzinfo=TAIPEI)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_archive(self, rts, env=None, clock=None):
        op = RoutedOpener(rts)
        out, sleeps = io.StringIO(), []
        c = clock or self.clock
        with contextlib.redirect_stdout(out):
            code = af.run({"PROBE_REPO": "o/r", "ARCHIVE_FSC_DIR": self.dir, "GITHUB_STEP_SUMMARY": self.summary, **(env or {})},
                          opener=op, sleep=sleeps.append, now=lambda: c)
        return code, out.getvalue(), op, sleeps

    def tree(self):
        snap = {}
        for r, _, fl in os.walk(self.dir):
            for f in fl:
                p = os.path.join(r, f)
                with open(p, "rb") as fh:
                    snap[os.path.relpath(p, self.dir)] = hashlib.sha256(fh.read()).hexdigest()
        return snap

    def load(self, rel):
        with open(os.path.join(self.dir, rel), encoding="utf-8") as f:
            return json.load(f)

    def parsed(self, year="115"):
        return self.load(f"parsed/{year}.json")

    def rows_by_code(self, year="115"):
        return {r["fields"]["證券代號"]: r for r in self.parsed(year)["rows"]}


# ─────────────────────────────────────────
# 解析
# ─────────────────────────────────────────
class TestParsing(Base):
    def test_happy_path_writes_all_outputs_and_keeps_every_case_type(self):
        code, out, op, sleeps = self.run_archive(routes())
        self.assertEqual(code, 0, out)
        names = sorted(self.tree())
        self.assertEqual([n.split("/")[0] for n in names], ["meta", "meta", "parsed", "parsed", "raw", "raw"])
        rows = self.parsed()["rows"]
        self.assertEqual(len(rows), 4)                                   # 沒有依案件類別篩選
        self.assertEqual({r["fields"]["案件類別"] for r in rows}, {"現金增資", "公司債", "海外存託憑證"})
        self.assertEqual(list(rows[0]["fields"]), HEADER)                # 18 欄、原順序（檔案裡也是）
        with open(os.path.join(self.dir, "parsed", "115.json"), encoding="utf-8") as f:
            line = [l for l in f if '"fields"' in l][0]
        self.assertLess(line.index('"證券代號"'), line.index('"公司型態"'))
        self.assertLess(line.index('"退件日期"'), line.index('"案件性質"'))
        self.assertEqual(rows[0]["fields"]["收文日期"], "1150105")        # 原始字串（數字型儲存格也是原樣文字）
        self.assertEqual(rows[0]["dates_iso"]["收文日期"], ["2026-01-05"])

    def test_header_on_row_3_trailer_rows_go_to_meta_not_data(self):
        self.run_archive(routes())
        meta = self.load("meta/115.json")
        self.assertEqual(meta["header_row"], 3)
        self.assertEqual(meta["row_counts"]["data_rows"], 4)
        self.assertEqual(meta["row_counts"]["trailer_rows"], 2)
        self.assertEqual([t["row"] for t in meta["trailer_rows"]], [8, 9])
        self.assertEqual(meta["trailer_rows"][1]["cells"][0]["value"], "資料來源：證期局")
        self.assertNotIn("資料來源：證期局", json.dumps(self.parsed(), ensure_ascii=False))
        self.assertIn("結尾列（含文字）第 8 列：註：本表資料以申報當日為準。", self.run_archive(routes())[1])   # 同時記在 log

    def test_trailer_count_other_than_two_is_noticed_in_log(self):
        code, out, *_ = self.run_archive(routes(x115=xlsx(base_rows(), trailer=[["只有一列註記"]])))
        self.assertEqual(code, 0)
        self.assertIn("結尾列合計 1（含文字 1 + 全空 0；先前確認為 2）", out)
        self.assertIn("提示，不是失敗", out)

    def test_multi_dates_become_arrays_and_raw_string_is_kept(self):
        rows = [data_row(code="1111", other_dates={"auto": "1150110,1150120", "stop": "1150201,1150202,1150203,1150204,1150205", "lift": "1150301"})]
        self.run_archive(routes(x115=xlsx(rows)))
        r = self.rows_by_code()["1111"]
        self.assertEqual(r["dates_iso"]["自動補正日期"], ["2026-01-10", "2026-01-20"])
        self.assertEqual(len(r["dates_iso"]["停止生效日期"]), 5)
        self.assertEqual(r["dates_iso"]["解除生效日期"], ["2026-03-01"])
        self.assertEqual(r["fields"]["自動補正日期"], "1150110,1150120")
        self.assertNotIn("生效日期", r["dates_iso"])                      # 空值不產生 ISO
        self.assertEqual(r["flags"], [])

    def test_nonexistent_and_unparseable_dates_keep_original_and_are_flagged(self):
        rows = [data_row(code="1111", other_dates={"effective": "1150231", "auto": "1150110,1151399", "revoke": "民國115年", "stop": "ABC"})]
        self.run_archive(routes(x115=xlsx(rows)))
        r = self.rows_by_code()["1111"]
        self.assertEqual(r["fields"]["生效日期"], "1150231")
        self.assertEqual(r["dates_iso"]["生效日期"], [None])
        self.assertEqual(r["dates_iso"]["自動補正日期"], ["2026-01-10", None])
        self.assertEqual(r["fields"]["廢止/撤銷日期"], "民國115年")
        self.assertIn("日期不存在:生效日期", r["flags"])
        self.assertIn("日期不存在:自動補正日期", r["flags"])
        self.assertIn("日期無法解析:廢止/撤銷日期", r["flags"])
        self.assertIn("日期無法解析:停止生效日期", r["flags"])
        self.assertEqual(self.parsed()["rows"][0]["fields"]["生效日期"], "1150231")     # 沒被補、沒被丟

    def test_comma_in_a_single_date_column_is_flagged(self):
        self.run_archive(routes(x115=xlsx([data_row(code="1111", other_dates={"effective": "1150101,1150102"})])))
        self.assertIn("單一日期欄含多值:生效日期", self.rows_by_code()["1111"]["flags"])

    def test_effective_date_after_source_update_date_is_flagged_and_listed_with_three_columns_only(self):
        rows = [data_row(code="1111", amount=100, other_dates={"effective": 1151201}), data_row(code="2222", amount=200, other_dates={"effective": 1150901})]
        code, out, *_ = self.run_archive(routes(x115=xlsx(rows)))
        self.assertIn("日期晚於來源更新日:生效日期", self.rows_by_code()["1111"]["flags"])
        self.assertNotIn("日期晚於來源更新日:生效日期", self.rows_by_code()["2222"]["flags"])
        self.assertIn("生效日期晚於來源更新日（2026-10-08）：1 列", out)
        self.assertIn("  - 1111／現金增資／1150109", out)
        self.assertNotIn("2222／現金增資", out.split("生效日期晚於來源更新日")[1].split("==")[0])
        for s in SENT:
            self.assertNotIn(s, out)

    def test_reference_date_is_the_later_of_filename_date_and_page_date(self):
        self.run_archive(routes(page=page_with("2026-10-20")))
        meta = self.load("meta/115.json")
        self.assertEqual((meta["source_date_filename"], meta["source_date_page"], meta["reference_date"]), ("2026-10-08", "2026-10-20", "2026-10-20"))

    def test_duplicate_keys_are_all_kept_with_sequence_and_never_merged(self):
        rows = [data_row(code="1111", amount=100), data_row(code="1111", amount=100), data_row(code="1111", amount=100), data_row(code="2222")]
        self.run_archive(routes(x115=xlsx(rows)))
        got = [r for r in self.parsed()["rows"] if r["fields"]["證券代號"] == "1111"]
        self.assertEqual([(r["dup_seq"], r["dup_count"]) for r in got], [(1, 3), (2, 3), (3, 3)])
        self.assertEqual(len({r["id"] for r in got}), 3)
        self.assertEqual(len(self.parsed()["rows"]), 4)
        self.assertEqual(got[0]["key"], "1111|現金增資|1150109|100")

    def test_same_code_category_receipt_with_different_amount_is_suspected_amount_change(self):
        rows = [data_row(code="1111", amount=100), data_row(code="1111", amount=150), data_row(code="2222", amount=7)]
        self.run_archive(routes(x115=xlsx(rows)))
        got = self.parsed()["rows"]
        self.assertEqual([("同日同類別多筆" in r["flags"]) for r in got], [True, True, False])
        self.assertEqual({r["fields"]["金額"] for r in got[:2]}, {"100", "150"})      # 兩筆原值都在

    def test_amount_and_price_numeric_only_when_purely_numeric(self):
        rows = [data_row(code="1111", amount="1,234,000"), data_row(code="2222", amount="5000")]
        rows[1][8] = "10.5"
        rows[0][8] = "約10元"
        self.run_archive(routes(x115=xlsx(rows)))
        a, b = self.parsed()["rows"]
        self.assertEqual(a["fields"]["金額"], "1,234,000")
        self.assertNotIn("金額", a["numbers"])
        self.assertIn("金額非純數字", a["flags"])
        self.assertIn("發行價格非純數字", a["flags"])
        self.assertEqual(b["numbers"], {"金額": 5000, "發行價格": 10.5})
        self.assertEqual(b["flags"], [])

    def test_cells_beyond_the_header_are_kept_and_flagged_not_silently_dropped(self):
        rows = [data_row(code="1111") + ["多出來的內容"]]
        self.run_archive(routes(x115=xlsx(rows)))
        r = self.parsed()["rows"][0]
        self.assertIn("表頭外有資料", r["flags"])
        self.assertEqual(r["extra_cells"], ["多出來的內容"])

    def test_blank_rows_between_data_are_counted_not_data(self):
        rows = base_rows()[:2] + [[None] * 18] + base_rows()[2:]
        self.run_archive(routes(x115=xlsx(rows)))
        self.assertEqual(self.load("meta/115.json")["row_counts"]["data_rows"], 4)

    def test_effective_date_equal_to_the_source_date_is_not_later_and_other_columns_flag_separately(self):
        rows = [data_row(code="1111", other_dates={"effective": 1151008}),                      # = 來源更新日 2026-10-08：不算晚於
                data_row(code="2222", other_dates={"reject": 1151009, "effective": 1150101})]  # 退件日期晚於，但生效日期沒有
        code, out, *_ = self.run_archive(routes(x115=xlsx(rows), x114=xlsx(rows)))
        got = self.rows_by_code()
        self.assertEqual(got["1111"]["flags"], [])
        self.assertEqual(got["2222"]["flags"], ["日期晚於來源更新日:退件日期"])
        self.assertEqual(out.count("生效日期晚於來源更新日（2026-10-08）：0 列"), 2)    # 兩個年度都是 0：摘要只計生效日期

    def test_last_rows_with_three_or_more_cells_are_data_even_without_a_code(self):
        rows = base_rows() + [["", "上市", "已生效", "機密公司甲", None, "公司債"] + [None] * 12]
        self.assertEqual(self.run_archive(routes(x115=xlsx(rows, trailer=[])))[0], 1)
        self.assertIn("證券代號空白", self.run_archive(routes(x115=xlsx(rows, trailer=[])), env={"ARCHIVE_FSC_DIR": self.tmp + "/x"})[1])

    def test_last_row_with_only_a_code_and_one_cell_is_data_not_trailer(self):
        rows = base_rows() + [["7777", None, "審查中"] + [None] * 15]
        self.run_archive(routes(x115=xlsx(rows, trailer=[])))
        self.assertEqual(self.load("meta/115.json")["row_counts"], {"sheet_rows": 8, "data_rows": 5, "trailer_rows": 0, "trailing_blank_rows": 0, "blank_rows": 0})
        self.assertIn("7777", self.rows_by_code())

    def test_trailing_blank_rows_after_the_trailer_are_not_data(self):
        self.run_archive(routes(x115=xlsx(base_rows(), trailer=TRAILER + [[None], [None]])))
        rc = self.load("meta/115.json")["row_counts"]
        self.assertEqual((rc["data_rows"], rc["trailer_rows"], rc["trailing_blank_rows"], rc["blank_rows"]), (4, 2, 2, 2))

    def test_trailing_blank_rows_are_recorded_separately_from_text_trailer_rows(self):
        # 與真實檔案同型：資料後 2 列「存在但沒有文字」（空字串／空白字元的儲存格）
        code, out, *_ = self.run_archive(routes(x115=xlsx(base_rows(), trailer=[["", " "], [" "]])))
        self.assertEqual(code, 0, out)
        meta = self.load("meta/115.json")
        self.assertEqual(meta["row_counts"], {"sheet_rows": 9, "data_rows": 4, "trailer_rows": 0, "trailing_blank_rows": 2, "blank_rows": 2})
        self.assertEqual(meta["trailer_rows"], [])
        self.assertIn("結尾列 2（含文字 0 + 全空 2）", out)
        self.assertNotIn("先前確認為 2", out)                               # 合計恰為 2：不提示

    def test_trailer_notice_uses_the_sum_and_is_never_a_failure(self):
        code, out, *_ = self.run_archive(routes(x115=xlsx(base_rows(), trailer=[["說明文字"], [None], [" "], [" "]])))
        self.assertEqual(code, 0, out)
        rc = self.load("meta/115.json")["row_counts"]
        self.assertEqual((rc["trailer_rows"], rc["trailing_blank_rows"]), (1, 3))
        self.assertIn("結尾列合計 4（含文字 1 + 全空 3；先前確認為 2）", out)
        self.assertEqual(self.run_archive(routes(x115=xlsx(base_rows(), trailer=[])), env={"ARCHIVE_FSC_DIR": self.tmp + "/n"})[0], 0)

    def test_data_row_decisions_do_not_depend_on_trailing_blank_rows(self):
        self.run_archive(routes(x115=xlsx(base_rows(), trailer=[])))
        without = self.parsed()["rows"]
        self.run_archive(routes(x115=xlsx(base_rows(), trailer=[["", " "], [" "]])), env={"ARCHIVE_FSC_DIR": self.tmp + "/w"})
        with open(os.path.join(self.tmp, "w", "parsed", "115.json"), encoding="utf-8") as f:
            with_blank = json.load(f)["rows"]
        self.assertEqual(without, with_blank)

    def test_neutral_flag_name_replaces_the_old_one_everywhere(self):
        rows = [data_row(code="1111", amount=100), data_row(code="1111", amount=150)]
        self.run_archive(routes(x115=xlsx(rows)))
        text = open(os.path.join(self.dir, "parsed", "115.json"), encoding="utf-8").read()
        self.assertIn("同日同類別多筆", text)
        self.assertNotIn("疑似金額變更", text)
        self.assertNotIn("疑似金額變更", open(self.summary, encoding="utf-8").read())
        self.assertIn("同日同類別多筆×2", open(self.summary, encoding="utf-8").read())

    def test_wrong_sheet_name_with_good_header_is_a_warning_recorded_in_meta(self):
        code, out, *_ = self.run_archive(routes(x115=xlsx(base_rows(), sheet="Sheet1")))
        self.assertEqual(code, 0)
        self.assertIn("::warning title=工作表名稱不同", out)
        self.assertEqual(self.load("meta/115.json")["sheet_name"], "Sheet1")

    def test_percent_encoded_filename_is_decoded_to_a_safe_name_and_original_kept(self):
        enc = "1151008%E7%94%B3%E5%A0%B1v3.xlsx"
        url = "https://www.fsc.gov.tw/userfiles/file/" + enc
        rows = [(lab, [(h.replace("1151008v3.xlsx", enc), t) for h, t in links]) for lab, links in GOOD_ROWS]
        r = routes(page=page_html(rows).encode("utf-8"))
        r[url] = r.pop(URL115)
        code, out, op, _ = self.run_archive(r)
        self.assertEqual(code, 0, out)
        meta = self.load("meta/115.json")
        self.assertEqual(meta["filename_original"], enc)
        self.assertEqual(meta["filename_decoded"], "1151008申報v3.xlsx")
        self.assertEqual(meta["filename_safe"], "1151008申報v3.xlsx")
        self.assertEqual(meta["file_version"], "3")
        self.assertEqual(meta["source_url"], url)
        self.assertTrue(any(n.startswith("raw/115_" + meta["sha256"][:8] + "_1151008申報v3") for n in self.tree()))

    def test_safe_filename_never_escapes_the_directory(self):
        for evil in ("../../etc/passwd.xlsx", "..%2F..%2Fx.xlsx", "a/b\\c.xlsx", ".hidden.xlsx", "x y;z$.xlsx", "noext"):
            n = af.safe_filename(__import__("urllib.parse").parse.unquote(evil))
            self.assertNotIn("/", n)
            self.assertNotIn("\\", n)
            self.assertFalse(n.startswith("."))
            self.assertTrue(n.endswith(".xlsx"))
            self.assertRegex(n, r"^[\w.\-()]+$")
        self.assertEqual(af.safe_filename("../../etc/passwd.xlsx"), "passwd.xlsx")
        self.assertEqual(af.safe_filename("x y;z$.xlsx"), "x_y_z_.xlsx")
        self.assertLessEqual(len(af.safe_filename("長" * 300 + ".xlsx")), 80)

    def test_raw_file_is_bit_identical_to_the_download(self):
        body = xlsx(base_rows())
        self.run_archive(routes(x115=body))
        raw = [n for n in self.tree() if n.startswith("raw/115_")][0]
        with open(os.path.join(self.dir, raw), "rb") as f:
            self.assertEqual(f.read(), body)
        self.assertEqual(self.load("meta/115.json")["sha256"], hashlib.sha256(body).hexdigest())


# ─────────────────────────────────────────
# 冪等與版本
# ─────────────────────────────────────────
class TestIdempotencyAndVersions(Base):
    def test_rerun_of_same_files_is_byte_identical_even_with_new_clock_and_page_text(self):
        self.run_archive(routes())
        first = self.tree()
        later = datetime(2031, 7, 4, 3, 0, tzinfo=TAIPEI)
        code, out, *_ = self.run_archive(routes(page=page_with("2030-01-01")), clock=later)
        self.assertEqual(code, 0)
        self.assertEqual(self.tree(), first)                               # git diff --staged 會是空的
        self.assertEqual(out.count("內容與既有相同，沒有檔案變動"), 2)

    def test_no_run_timestamp_in_parsed_or_raw_and_first_seen_only_in_meta(self):
        self.clock = datetime(2031, 7, 4, 3, 0, tzinfo=TAIPEI)
        self.run_archive(routes())
        for rel in self.tree():
            with open(os.path.join(self.dir, rel), "rb") as f:
                data = f.read()
            if rel.startswith("meta/"):
                self.assertIn(b"2031-07-04", data)
            elif rel.startswith("parsed/"):
                self.assertNotIn(b"2031-07-04", data)
                self.assertNotIn(b"2031-", data)           # 不用 b"2031"：sha256 十六進位字串偶爾會含這四個字元

    def test_first_seen_is_taipei_date_not_utc(self):
        self.run_archive(routes(), clock=datetime(2026, 10, 11, 17, 30, tzinfo=timezone.utc))       # UTC 17:30 = 台北隔天 01:30
        self.assertEqual(self.load("meta/115.json")["first_seen"], "2026-10-12")

    def test_new_version_writes_diff_and_preserves_old_first_seen_in_history(self):
        self.run_archive(routes())
        old_sha = self.load("meta/115.json")["sha256"]
        new_rows = base_rows()
        new_rows[0] = data_row(code="1111", category="現金增資", closing="已生效", amount=100, received=1150105, other_dates={"effective": 1150120, "lift": 1150125})   # 解除生效日期新增
        new_rows[1] = data_row(code="2222", category="公司債", closing="已生效", amount=200, received=1150106, other_dates={"effective": 1150130})                   # 結案類型 + 生效日期
        del new_rows[3]                                                                                                                                               # 4444 消失
        new_rows.append(data_row(code="5555", category="公司債", amount=500, received=1150115))                                                                       # 新增
        later = datetime(2026, 10, 20, 9, 0, tzinfo=TAIPEI)
        code, out, *_ = self.run_archive(routes(x115=xlsx(new_rows)), clock=later)
        self.assertEqual(code, 0, out)
        meta = self.load("meta/115.json")
        new_sha = meta["sha256"]
        self.assertNotEqual(old_sha, new_sha)
        self.assertEqual(meta["first_seen"], "2026-10-20")
        self.assertEqual([(h["sha8"], h["first_seen"]) for h in meta["history"]], [(old_sha[:8], "2026-10-12"), (new_sha[:8], "2026-10-20")])
        diff = self.load(f"diffs/115_{old_sha[:8]}_{new_sha[:8]}.json")
        self.assertEqual(diff["added"], ["5555|公司債|1150115|500#1"])
        self.assertEqual(diff["vanished"], ["4444|現金增資|1150108|400#1"])
        ch = {(c["id"], c["field"]): (c["old"], c["new"]) for c in diff["changed"]}
        self.assertEqual(ch[("1111|現金增資|1150105|100#1", "解除生效日期")], ("", "1150125"))
        self.assertEqual(ch[("2222|公司債|1150106|200#1", "結案類型")], ("審查中", "已生效"))
        self.assertEqual(ch[("2222|公司債|1150106|200#1", "生效日期")], ("", "1150130"))
        self.assertEqual(len(ch), 3)
        self.assertEqual(diff["counts"], {"added": 1, "vanished": 1, "changed_fields": 3, "old_rows": 4, "new_rows": 4})
        self.assertEqual(len([n for n in self.tree() if n.startswith("raw/115_")]), 2)      # 兩個版本的原檔都留
        self.assertIn("新增 1、消失 1、同鍵欄位變更 3", out)

    def test_diff_covers_every_date_column_and_closing_type(self):
        self.run_archive(routes())
        rows = base_rows()
        rows[0] = data_row(code="1111", category="現金增資", closing="已生效", amount=100, received=1150105,
                           other_dates={"auto": "1150201", "stop": "1150202", "lift": "1150203", "effective": 1150204, "revoke": "1150205", "withdraw": "1150206", "reject": "1150207"})
        self.run_archive(routes(x115=xlsx(rows)))
        d = [n for n in self.tree() if n.startswith("diffs/115_")][0]
        fields = {c["field"] for c in self.load(d)["changed"]}
        self.assertEqual(fields, {"自動補正日期", "停止生效日期", "解除生效日期", "生效日期", "廢止/撤銷日期", "自行撤回日期", "退件日期"})

    def test_flag_rename_does_not_touch_diff_pairing_and_flags_never_appear_in_diffs(self):
        rows = [data_row(code="1111", amount=100), data_row(code="1111", amount=150), data_row(code="2222", amount=7)]
        self.run_archive(routes(x115=xlsx(rows)))
        rows2 = [data_row(code="1111", amount=100), data_row(code="1111", amount=175), data_row(code="2222", amount=7)]
        self.run_archive(routes(x115=xlsx(rows2)))
        d = [n for n in self.tree() if n.startswith("diffs/115_")][0]
        text = open(os.path.join(self.dir, d), encoding="utf-8").read()
        diff = json.loads(text)
        self.assertEqual((diff["vanished"], diff["added"], diff["changed"]), (["1111|現金增資|1150109|150#1"], ["1111|現金增資|1150109|175#1"], []))
        self.assertNotIn("同日同類別多筆", text)
        self.assertNotIn("flags", text)
        # 旗標只是 parsed 的附註：兩個版本都有旗標，但 diff 的配對只看案件鍵與欄位值
        flagged = [r for r in self.parsed()["rows"] if "同日同類別多筆" in r["flags"]]
        self.assertEqual(len(flagged), 2)

    def test_only_changed_year_gets_a_diff_and_the_other_year_stays_identical(self):
        self.run_archive(routes())
        before = self.tree()
        changed = base_rows()
        changed[1][2] = "已生效"
        self.run_archive(routes(x115=xlsx(changed)))
        after = self.tree()
        self.assertTrue(all(after[k] == v for k, v in before.items() if "114" in k))
        self.assertEqual(len([n for n in after if n.startswith("diffs/")]), 1)
        self.assertTrue([n for n in after if n.startswith("diffs/115_")])

    def test_amount_change_between_versions_shows_as_vanished_plus_added(self):
        self.run_archive(routes())
        rows = base_rows()
        rows[1] = data_row(code="2222", category="公司債", closing="審查中", amount=999, received=1150106)
        self.run_archive(routes(x115=xlsx(rows)))
        d = [n for n in self.tree() if n.startswith("diffs/")][0]
        diff = self.load(d)
        self.assertEqual((diff["vanished"], diff["added"]), (["2222|公司債|1150106|200#1"], ["2222|公司債|1150106|999#1"]))
        self.assertEqual([r["flags"] for r in self.parsed()["rows"] if r["fields"]["證券代號"] == "2222"], [[]])   # 版本內只剩一種金額 → 不再標記

    def test_rolling_back_to_an_earlier_version_keeps_its_original_first_seen(self):
        changed = base_rows()
        changed[1][2] = "已生效"
        a, b = xlsx(base_rows()), xlsx(changed)
        self.run_archive(routes(x115=a))
        self.run_archive(routes(x115=b), clock=self.clock + timedelta(days=3))
        self.run_archive(routes(x115=a), clock=self.clock + timedelta(days=9))
        meta = self.load("meta/115.json")
        self.assertEqual(meta["first_seen"], "2026-10-12")
        self.assertEqual(len(meta["history"]), 2)

    def test_partial_previous_write_is_redone_next_time(self):
        self.run_archive(routes())
        os.remove(os.path.join(self.dir, "meta", "115.json"))               # 模擬上次在最後一步前中斷
        os.remove(os.path.join(self.dir, "parsed", "115.json"))
        code, out, *_ = self.run_archive(routes())
        self.assertEqual(code, 0, out)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "meta", "115.json")))

    def test_existing_raw_with_same_name_but_different_bytes_is_never_overwritten(self):
        self.run_archive(routes())
        raw = [n for n in self.tree() if n.startswith("raw/115_")][0]
        with open(os.path.join(self.dir, raw), "wb") as f:
            f.write(b"tampered")
        os.remove(os.path.join(self.dir, "meta", "115.json"))
        code, out, *_ = self.run_archive(routes())
        self.assertEqual(code, 1)
        self.assertIn("不覆蓋", out)
        with open(os.path.join(self.dir, raw), "rb") as f:
            self.assertEqual(f.read(), b"tampered")


# ─────────────────────────────────────────
# fail-closed
# ─────────────────────────────────────────
class TestFailClosed(Base):
    def assert_year_not_written(self, year="115"):
        for sub in ("meta", "parsed"):
            self.assertFalse(os.path.exists(os.path.join(self.dir, sub, f"{year}.json")), f"{sub}/{year}")
        raw = os.path.join(self.dir, "raw")
        self.assertFalse(os.path.isdir(raw) and any(n.startswith(year + "_") for n in os.listdir(raw)))

    def fails(self, x115, needle):
        code, out, op, _ = self.run_archive(routes(x115=x115))
        self.assertEqual(code, 1, out)
        self.assertIn("::error title=留存失敗 115年度申報案件", out)
        self.assertIn(needle, out)
        self.assert_year_not_written("115")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "meta", "114.json")))   # 另一年度不受影響
        return out

    def test_sheet_count_not_one(self):
        self.fails(xlsx(base_rows(), extra_sheets=[("Sheet2", [["x"]])]), "工作表數量為 2")

    def test_header_missing_column_extra_column_and_wrong_order(self):
        self.fails(xlsx(base_rows(), header=HEADER[:-1]), "名稱或欄數不同")
        self.fails(xlsx(base_rows(), header=HEADER + ["新欄位"]), "多出 ['新欄位']")
        swapped = list(HEADER)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        self.fails(xlsx(base_rows(), header=swapped), "順序不同")
        renamed = list(HEADER)
        renamed[6] = "申請金額"
        self.fails(xlsx(base_rows(), header=renamed), "缺少 ['金額']")

    def test_header_not_found(self):
        self.fails(xlsx(base_rows(), header=["foo"] * 18), "找不到含「證券代號」的表頭列")

    def test_blank_security_code_in_a_data_row(self):
        rows = base_rows()
        rows[2][0] = None
        out = self.fails(xlsx(rows), "證券代號空白")
        self.assertIn("第 6 列", out)

    def test_no_data_rows(self):
        self.fails(xlsx([]), "沒有任何資料列")

    def test_corrupt_non_xlsx_and_html_response(self):
        self.fails(b"not a zip at all", "xlsx 無法解析")
        self.fails(b"<html>blocked</html>", "xlsx 無法解析")

    def test_row_drop_of_20_percent_or_more_is_refused_but_19_percent_is_accepted(self):
        ten = [data_row(code=str(1000 + i), amount=i) for i in range(10)]
        self.run_archive(routes(x115=xlsx(ten)))
        good = self.tree()
        code, out, *_ = self.run_archive(routes(x115=xlsx(ten[:8])))              # 少 20%
        self.assertEqual(code, 1)
        self.assertIn("比前一版 10 少 2 列", out)
        self.assertEqual(self.tree(), good)                                         # 一個位元組都沒動
        hundred = [data_row(code=str(1000 + i), amount=i) for i in range(100)]
        self.run_archive(routes(x115=xlsx(hundred)))
        code, out, *_ = self.run_archive(routes(x115=xlsx(hundred[:81])))        # 少 19%
        self.assertEqual(code, 0, out)
        self.assertEqual(self.load("meta/115.json")["row_counts"]["data_rows"], 81)
        code, out, *_ = self.run_archive(routes(x115=xlsx(hundred[:64])))        # 81 → 64：少 21%
        self.assertEqual(code, 1)

    def test_row_drop_check_does_not_apply_to_a_rerun_of_the_same_version(self):
        self.run_archive(routes())
        self.assertEqual(self.run_archive(routes())[0], 0)

    def test_page_row_missing_or_not_unique(self):
        rows = [r for r in GOOD_ROWS if r[0] != "115年度申報案件"]
        code, out, op, _ = self.run_archive(routes(page=page_html(rows).encode("utf-8")))
        self.assertEqual(code, 1)
        self.assertIn("找不到儲存格文字完全等於「115年度申報案件」", out)
        self.assert_year_not_written("115")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "meta", "114.json")))
        self.assertEqual([r[0] for r in op.requests], [PAGE_URL, URL114])
        dup = GOOD_ROWS + [("115年度申報案件", [("https://www.fsc.gov.tw/userfiles/file/other.xlsx", "x")])]
        code, out, op, _ = self.run_archive(routes(page=page_html(dup).encode("utf-8")), env={"ARCHIVE_FSC_DIR": os.path.join(self.tmp, "d2")})
        self.assertEqual(code, 1)
        self.assertIn("有 2 列的儲存格文字完全等於", out)

    def test_http_errors_truncation_and_redirect_are_explicit_and_not_followed_or_retried(self):
        for outcome, needle in ((http_error(URL115, 404), "HTTP 404"), (http_error(URL115, 302, "https://evil.example.com/x"), "被轉址"),
                                (urllib.error.URLError("dns"), "連線失敗")):
            r = routes()
            r[URL115] = outcome
            code, out, op, _ = self.run_archive(r, env={"ARCHIVE_FSC_DIR": os.path.join(self.tmp, "d" + needle)})
            self.assertEqual(code, 1)
            self.assertIn(needle, out)
            self.assertEqual(len(op.requests), 3)                                   # 沒有重試、沒有跟隨
            self.assertNotIn("evil.example.com", [u for u, *_ in op.requests])

    def test_oversized_response_is_a_truncation_error(self):
        old = ps.MAX_BYTES
        ps.MAX_BYTES = 100
        try:
            code, out, *_ = self.run_archive(routes())
        finally:
            ps.MAX_BYTES = old
        self.assertEqual(code, 1)
        self.assertIn("超過大小上限", out)

    def test_corrupt_existing_meta_or_parsed_is_not_overwritten(self):
        self.run_archive(routes())
        meta_p = os.path.join(self.dir, "meta", "115.json")
        with open(meta_p, "w") as f:
            f.write("{broken")
        code, out, *_ = self.run_archive(routes())
        self.assertEqual(code, 1)
        self.assertIn("為避免覆蓋已留存的資料", out)
        with open(meta_p) as f:
            self.assertEqual(f.read(), "{broken")
        # 新版本出現、但既有 parsed 不見：不能比對就不寫
        shutil.rmtree(self.tmp + "/fsc")
        self.run_archive(routes())
        os.remove(os.path.join(self.dir, "parsed", "115.json"))
        before = self.tree()
        code, out, *_ = self.run_archive(routes(x115=xlsx(base_rows()[:3] + [data_row(code="9999")])))
        self.assertEqual(code, 1)
        self.assertIn("無法與新版本比對", out)
        self.assertEqual(self.tree(), before)

    def test_existing_meta_with_valid_json_but_wrong_structure_is_not_overwritten(self):
        self.run_archive(routes())
        meta_p = os.path.join(self.dir, "meta", "115.json")
        for bad in ("{}", "[]", '{"sha256": "x"}', '{"sha256": 1, "history": [], "row_counts": {"data_rows": 4}}'):
            with open(meta_p, "w") as f:
                f.write(bad)
            code, out, *_ = self.run_archive(routes())
            self.assertEqual(code, 1, bad)
            self.assertIn("結構不符，為避免覆蓋", out)
            with open(meta_p) as f:
                self.assertEqual(f.read(), bad)

    def test_truncated_page_is_a_failure_and_no_xlsx_is_requested(self):
        old = ps.MAX_BYTES
        ps.MAX_BYTES = 200                       # 頁面 629 bytes → 被截斷，但截斷後的內容仍可能含完整的年度列
        try:
            code, out, op, _ = self.run_archive(routes())
        finally:
            ps.MAX_BYTES = old
        self.assertEqual(code, 1)
        self.assertEqual(len(op.requests), 1)
        self.assertIn("頁面取得失敗，兩個年度都無法解析連結", out)
        self.assertFalse(os.path.exists(self.dir))

    def test_unexpected_exception_in_one_year_is_an_error_not_a_crash(self):
        orig = af.parse_sheet

        def boom(data):
            if boom.n == 0:
                boom.n += 1
                raise RuntimeError("boom")
            return orig(data)
        boom.n = 0
        af.parse_sheet = boom
        try:
            code, out, *_ = self.run_archive(routes())
        finally:
            af.parse_sheet = orig
        self.assertEqual(code, 1)
        self.assertIn("未預期的錯誤 RuntimeError", out)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "meta", "114.json")))

    def test_either_year_failing_leaves_the_other_written_and_exit_code_red(self):
        for bad in (URL115, URL114):
            d = os.path.join(self.tmp, "iso" + bad[-9:])
            r = routes()
            r[bad] = FakeResp(b"garbage", ctype="application/octet-stream")
            code, out, *_ = self.run_archive(r, env={"ARCHIVE_FSC_DIR": d})
            good = "114" if bad == URL115 else "115"
            self.assertEqual(code, 1)
            self.assertTrue(os.path.exists(os.path.join(d, "meta", f"{good}.json")))
            self.assertTrue(os.path.exists(os.path.join(d, "parsed", f"{good}.json")))
            self.assertFalse(os.path.exists(os.path.join(d, "meta", f"{'115' if good == '114' else '114'}.json")))

    def test_page_failure_requests_no_xlsx_and_writes_nothing(self):
        for outcome in (http_error(PAGE_URL, 403), urllib.error.URLError("dns"), http_error(PAGE_URL, 301, "https://www.sfb.gov.tw/x")):
            code, out, op, _ = self.run_archive({PAGE_URL: outcome})
            self.assertEqual(code, 1)
            self.assertEqual(len(op.requests), 1)
            self.assertIn("::error title=留存失敗 頁面", out)
            self.assertFalse(os.path.exists(self.dir))

    def test_hostile_xlsx_link_host_is_rejected_before_download(self):
        evil = [("115年度申報案件", [("https://evil.example.com/1151008v3.xlsx", "x")]), GOOD_ROWS[0]]
        code, out, op, _ = self.run_archive(routes(page=page_html(evil).encode("utf-8")))
        self.assertEqual(code, 1)
        self.assertIn("不在白名單", out)
        self.assertNotIn("evil.example.com", [u.split("/")[2] for u, *_ in op.requests])

    def test_hostile_or_insecure_page_url_is_rejected_before_any_request(self):
        for url in ("https://evil.example.com/x", "http://www.sfb.gov.tw/x", "https://www.sfb.gov.tw.evil.com/x"):
            code, out, op, _ = self.run_archive(routes(), env={"ARCHIVE_FSC_PAGE_URL": url})
            self.assertEqual(code, 2, url)
            self.assertEqual(op.requests, [])
        self.assertFalse(os.path.exists(self.dir))

    def test_new_year_label_on_the_page_is_only_a_warning(self):
        rows = GOOD_ROWS + [("116年度申報案件", [("https://www.fsc.gov.tw/userfiles/file/1160101v1.xlsx", "x")])]
        code, out, op, _ = self.run_archive(routes(page=page_html(rows).encode("utf-8")))
        self.assertEqual(code, 0)
        self.assertIn("::warning title=頁面出現未納入留存的年度::116年度申報案件", out)
        self.assertEqual(len(op.requests), 3)


# ─────────────────────────────────────────
# 請求規則、輸出內容
# ─────────────────────────────────────────
class TestRequestsAndOutput(Base):
    def test_exactly_three_gets_spaced_at_least_6s_with_project_user_agent_page_first(self):
        code, out, op, sleeps = self.run_archive(routes())
        self.assertEqual(code, 0, out)
        self.assertEqual([u for u, *_ in op.requests], [PAGE_URL, URL115, URL114])
        self.assertTrue(all(m == "GET" for _, m, _ in op.requests))
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all(s >= ps.MIN_INTERVAL_SEC for s in sleeps))
        for _, _, h in op.requests:
            self.assertIn("Corporate-Bond-Bot-archive-fsc", h["User-agent"])
            self.assertIn("o/r", h["User-agent"])

    def test_the_same_three_requests_are_made_even_when_nothing_changed(self):
        self.run_archive(routes())
        code, out, op, sleeps = self.run_archive(routes())
        self.assertEqual(len(op.requests), 3)
        self.assertEqual(len(sleeps), 2)

    def test_dry_run_connects_to_nothing_and_writes_nothing(self):
        code, out, op, sleeps = self.run_archive(routes(), env={"ARCHIVE_DRY_RUN": "1"})
        self.assertEqual(code, 0)
        self.assertEqual(op.requests, [])
        self.assertEqual(sleeps, [])
        self.assertFalse(os.path.exists(self.dir))
        self.assertFalse(os.path.exists(self.summary))
        self.assertIn("DRY-RUN", out)

    def test_log_and_summary_contain_numbers_not_companies_amounts_or_underwriters(self):
        code, out, *_ = self.run_archive(routes())
        with open(self.summary, encoding="utf-8") as f:
            summary = f.read()
        for s in SENT:
            self.assertNotIn(s, out)
            self.assertNotIn(s, summary)
        for needle in ("案件類別（3 種）", "結案類型（4 種）", "日期欄樣態", "品質旗標", "結案類型 × 生效日期有無", "已生效：有1／無0", "審查中：有0／無1"):
            self.assertIn(needle, summary)
        self.assertIn("收文日期：7碼×4", summary)

    def test_date_shape_counts_cover_multi_nonexistent_and_other(self):
        rows = [data_row(code="1111", other_dates={"auto": "1150110,1150120"}), data_row(code="2222", other_dates={"auto": "1150231"}),
                data_row(code="3333", other_dates={"auto": "abc"}), data_row(code="4444", other_dates={"auto": "1150110,1150231"})]
        self.run_archive(routes(x115=xlsx(rows)))
        s = open(self.summary, encoding="utf-8").read()
        self.assertIn("逗號多值2個×1", s)
        self.assertIn("7碼但日期不存在×1", s)
        self.assertIn("逗號多值2個含不存在日期×1", s)
        self.assertIn("其他:a{3}×1", s)

    def test_files_are_written_under_data_fsc_by_default_and_nothing_elsewhere(self):
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            op = RoutedOpener(routes())
            with contextlib.redirect_stdout(io.StringIO()):
                af.run({"PROBE_REPO": "o/r"}, opener=op, sleep=lambda s: None, now=lambda: self.clock)
        finally:
            os.chdir(cwd)
        top = set(os.listdir(self.tmp)) - {"summary.md"}
        self.assertEqual(top, {"data"})
        self.assertEqual(os.listdir(os.path.join(self.tmp, "data")), ["fsc"])

    def test_no_tmp_files_left_behind(self):
        self.run_archive(routes())
        self.assertFalse([n for n in self.tree() if n.endswith(".tmp")])


# ─────────────────────────────────────────
# workflow
# ─────────────────────────────────────────
@unittest.skipIf(yaml is None, "需要 PyYAML")
class TestWorkflowContract(unittest.TestCase):
    def setUp(self):
        self.text = open(os.path.join(ROOT, ".github", "workflows", "archive-fsc.yml"), encoding="utf-8").read()
        self.wf = yaml.safe_load(self.text)
        self.code = re.sub(r"(?m)^\s*#.*$", "", self.text)
        self.steps = self.wf["jobs"]["archive"]["steps"]

    def test_triggers_dispatch_with_dry_run_and_two_daily_crons_on_unused_minutes(self):
        on = self.wf[True]
        self.assertEqual(set(on), {"schedule", "workflow_dispatch"})
        self.assertEqual(set(on["workflow_dispatch"]["inputs"]), {"dry_run"})
        self.assertEqual(on["workflow_dispatch"]["inputs"]["dry_run"]["default"], "false")
        crons = [c["cron"] for c in on["schedule"]]
        self.assertEqual(len(crons), 2)
        used = set()
        for name in os.listdir(os.path.join(ROOT, ".github", "workflows")):
            if name == "archive-fsc.yml":
                continue
            t = open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8").read()
            used |= {m.split()[0] for m in re.findall(r'cron:\s*"([^"]+)"', t)}
        for c in crons:
            f = c.split()
            self.assertEqual(f[2:], ["*", "*", "*"], c)                              # 每天
            self.assertNotIn(f[0], ("0", "30"), c)
            self.assertNotIn(f[0], used, c)
        self.assertEqual(len({c.split()[0] for c in crons}), 2)

    def test_permissions_contents_write_only_no_secrets_no_telegram_no_dispatch(self):
        self.assertEqual(self.wf["permissions"], {"contents": "write"})
        self.assertNotIn("permissions", self.wf["jobs"]["archive"])
        for forbidden in ("secrets.", "TELEGRAM", "api.telegram.org", "/dispatches", "gh api", "actions: write", "workflow_run", "bot_relay", "GITHUB_TOKEN"):
            self.assertNotIn(forbidden, self.code, forbidden)

    def test_independent_concurrency_never_cancels(self):
        c = self.wf["concurrency"]
        self.assertEqual(c["group"], "archive-fsc")
        self.assertIs(c["cancel-in-progress"], False)

    def test_commit_step_contract(self):
        commit = [s for s in self.steps if "push_with_retry.sh" in s.get("run", "")][0]
        self.assertEqual(commit["env"]["PUSH_ALERT_CMD"], "echo")
        self.assertIn("data/fsc 推送失敗", commit["env"]["PUSH_FAIL_MESSAGE"])
        self.assertNotIn("每日報告", commit["env"]["PUSH_FAIL_MESSAGE"])
        run = commit["run"]
        self.assertLess(run.index("mkdir -p data/fsc"), run.index("git add data/fsc"))
        self.assertIn("無變更，略過 commit", run)
        self.assertNotIn("git add .", run)
        self.assertNotRegex(run, r"(?m)^\s*git push\b")
        self.assertNotIn("reports", run)
        # 原始檔入庫防呆：在 git add 之後、判斷「無變更」之前
        self.assertIn("git ls-files --error-unmatch", run)
        self.assertIn("::error title=原始檔未被 git 追蹤", run)
        self.assertLess(run.index("git add data/fsc"), run.index("git ls-files --error-unmatch"))
        self.assertLess(run.index("git ls-files --error-unmatch"), run.index("git diff --staged --quiet"))

    def test_failure_gate_and_continue_on_error(self):
        arch = [s for s in self.steps if s.get("id") == "archive"][0]
        self.assertIs(arch["continue-on-error"], True)
        self.assertNotIn("${{", arch["run"])
        self.assertEqual(set(arch["env"]), {"ARCHIVE_DRY_RUN", "PROBE_REPO"})
        commit = [s for s in self.steps if "push_with_retry.sh" in s.get("run", "")][0]
        gate = self.steps[-1]
        self.assertLess(self.steps.index(arch), self.steps.index(commit))
        self.assertLess(self.steps.index(commit), self.steps.index(gate))
        self.assertIn("steps.archive.outcome == 'failure'", gate["if"])
        self.assertIn("always()", gate["if"])
        self.assertIn("exit 1", gate["run"])
        self.assertEqual(arch["run"], "python3 scripts/archive_fsc.py")


@unittest.skipIf(yaml is None, "需要 PyYAML")
class TestCommitStepActuallyRuns(unittest.TestCase):
    """把 workflow 裡「提交存檔」那段 shell 原封不動拿出來，在真的 git 倉庫裡跑（含 data/fsc 不存在的情況）。"""

    def setUp(self):
        import subprocess
        self.subprocess = subprocess
        self.tmp = tempfile.mkdtemp()
        wf = yaml.safe_load(open(os.path.join(ROOT, ".github", "workflows", "archive-fsc.yml"), encoding="utf-8"))
        step = [s for s in wf["jobs"]["archive"]["steps"] if "push_with_retry.sh" in s.get("run", "")][0]
        self.script = step["run"]
        self.env_extra = {k: re.sub(r"\$\{\{[^}]*\}\}", "X", str(v)) for k, v in step["env"].items()}
        self.remote = os.path.join(self.tmp, "remote.git")
        self.work = os.path.join(self.tmp, "work")
        self.git(self.tmp, "init", "-q", "--bare", "-b", "master", self.remote)
        self.git(self.tmp, "clone", "-q", self.remote, self.work)
        self.git(self.work, "checkout", "-q", "-b", "master")
        os.makedirs(os.path.join(self.work, "scripts"))
        shutil.copy(os.path.join(ROOT, "scripts", "push_with_retry.sh"), os.path.join(self.work, "scripts"))
        # 真實的 .gitignore（含 *.xlsx 與 data/fsc/raw 的例外）：首次部署就是死在這裡——暫存倉庫沒有它，測試看不出 raw 被忽略
        shutil.copy(os.path.join(ROOT, ".gitignore"), os.path.join(self.work, ".gitignore"))
        with open(os.path.join(self.work, "README"), "w") as f:
            f.write("x")
        self.git(self.work, "add", "-A")
        self.git(self.work, "commit", "-q", "-m", "init")
        self.git(self.work, "push", "-q", "origin", "master")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def git(self, cwd, *args):
        e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        return self.subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=e)

    def run_step(self):
        e = {**os.environ, "GITHUB_REF_NAME": "master", "PUSH_RETRY_SLEEP": "0", "PUSH_ATTEMPTS": "2", **self.env_extra}
        return self.subprocess.run(["bash", "-e", "-c", self.script], cwd=self.work, capture_output=True, text=True, env=e)

    def remote_tree(self):
        return self.git(self.tmp, "--git-dir", self.remote, "ls-tree", "-r", "--name-only", "master").stdout.split()

    def archive_into_work(self, rts):
        op = RoutedOpener(rts)
        with contextlib.redirect_stdout(io.StringIO()):
            return af.run({"PROBE_REPO": "o/r", "ARCHIVE_FSC_DIR": os.path.join(self.work, "data", "fsc")}, opener=op, sleep=lambda s: None,
                          now=lambda: datetime(2026, 10, 12, 9, 0, tzinfo=TAIPEI))

    def test_no_data_directory_at_all_is_a_clean_no_change(self):
        self.assertFalse(os.path.exists(os.path.join(self.work, "data")))
        r = self.run_step()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("無變更", r.stdout)

    def test_real_archive_output_is_committed_pushed_and_a_same_file_rerun_is_a_no_change(self):
        self.assertEqual(self.archive_into_work(routes()), 0)
        r = self.run_step()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("push 成功", r.stdout)
        tree = self.git(self.tmp, "--git-dir", self.remote, "ls-tree", "-r", "--name-only", "master").stdout.split()
        self.assertTrue(any(n.startswith("data/fsc/parsed/115") for n in tree), tree)
        self.assertTrue(any(n.startswith("data/fsc/raw/114_") for n in tree), tree)
        self.assertRegex(self.git(self.work, "log", "-1", "--format=%s").stdout, r"^archive: 證期局申報彙總表 \d{4}-\d{2}-\d{2}")
        # 第二次：隔天、頁面文字不同、同一個檔 → git diff --staged 必須是空的
        op = RoutedOpener(routes(page=page_with("2030-01-01")))
        with contextlib.redirect_stdout(io.StringIO()):
            af.run({"PROBE_REPO": "o/r", "ARCHIVE_FSC_DIR": os.path.join(self.work, "data", "fsc")}, opener=op, sleep=lambda s: None,
                   now=lambda: datetime(2031, 1, 1, tzinfo=TAIPEI))
        self.git(self.work, "add", "data/fsc")
        self.assertEqual(self.git(self.work, "diff", "--staged", "--stat").stdout.strip(), "")
        r2 = self.run_step()
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("無變更", r2.stdout)

    def test_every_path_the_archive_writes_is_not_ignored_by_the_real_gitignore(self):
        self.assertEqual(self.archive_into_work(routes()), 0)
        # 讓 diffs/ 也出現：第二個版本
        changed = base_rows()
        changed[1][2] = "已生效"
        self.assertEqual(self.archive_into_work(routes(x115=xlsx(changed))), 0)
        os.makedirs(os.path.join(self.work, "data", "fsc"), exist_ok=True)
        shutil.copy(os.path.join(ROOT, "data", "fsc", "SOURCE.md"), os.path.join(self.work, "data", "fsc", "SOURCE.md"))
        paths = []
        for r, _, fl in os.walk(os.path.join(self.work, "data", "fsc")):
            paths += [os.path.relpath(os.path.join(r, f), self.work) for f in fl]
        kinds = {p.split("/")[2] for p in paths}
        self.assertEqual(kinds, {"raw", "parsed", "meta", "diffs", "SOURCE.md"})
        for p in sorted(paths):
            r = self.git(self.work, "check-ignore", "-v", p)
            # 回傳 1 = 沒有任何規則命中；回傳 0 時 -v 會顯示命中的規則，開頭是 ! 的是「例外」規則（= 不被忽略）
            if r.returncode == 0:
                pattern = r.stdout.split("\t")[0].split(":", 2)[2]
                self.assertTrue(pattern.startswith("!"), f"{p} 被忽略：{r.stdout}")
            else:
                self.assertEqual(r.returncode, 1, r.stderr)

    def test_the_test_is_meaningful_without_the_exception_raw_would_be_ignored(self):
        with open(os.path.join(self.work, ".gitignore"), "w") as f:
            f.write("*.xlsx\n")                                              # 修正前的規則
        self.assertEqual(self.archive_into_work(routes()), 0)
        raw = [os.path.relpath(os.path.join(r, f), self.work) for r, _, fl in os.walk(os.path.join(self.work, "data", "fsc", "raw")) for f in fl]
        self.assertEqual(len(raw), 2)
        for p in raw:
            self.assertEqual(self.git(self.work, "check-ignore", "-q", p).returncode, 0, p)

    def test_raw_files_are_committed_and_pushed_with_the_real_gitignore(self):
        self.assertEqual(self.archive_into_work(routes()), 0)
        r = self.run_step()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        tree = self.remote_tree()
        self.assertEqual(len([n for n in tree if n.startswith("data/fsc/raw/") and n.endswith(".xlsx")]), 2, tree)

    def test_raw_present_but_ignored_turns_the_run_red_and_pushes_nothing(self):
        with open(os.path.join(self.work, ".gitignore"), "w") as f:
            f.write("*.xlsx\n")                                              # 修正前的 .gitignore
        self.git(self.work, "add", ".gitignore")
        self.git(self.work, "commit", "-q", "-m", "old ignore")
        self.git(self.work, "push", "-q", "origin", "master")
        self.assertEqual(self.archive_into_work(routes()), 0)
        r = self.run_step()
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("::error title=原始檔未被 git 追蹤", r.stdout)
        self.assertEqual(r.stdout.count("::error title=原始檔未被 git 追蹤"), 2)    # 兩個年度的原檔各一次
        self.assertEqual([n for n in self.remote_tree() if n.startswith("data/")], [])          # 半套資料（只有 meta／parsed）也不提交
        self.assertNotIn("push 成功", r.stdout)

    def test_a_single_ignored_raw_file_is_enough_to_fail(self):
        self.assertEqual(self.archive_into_work(routes()), 0)
        with open(os.path.join(self.work, "data", "fsc", "raw", "evil.csv"), "w") as f:
            f.write("x")                                                    # *.csv 同樣被 .gitignore 忽略
        r = self.run_step()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("raw/evil.csv", r.stdout)
        self.assertEqual([n for n in self.remote_tree() if n.startswith("data/")], [])

    def test_push_failure_turns_the_step_red_with_the_fsc_specific_message(self):
        self.assertEqual(self.archive_into_work(routes()), 0)
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        os.chmod(hook, 0o755)
        r = self.run_step()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("::error title=git push 最終失敗", r.stdout)
        self.assertIn("data/fsc 推送失敗", r.stdout)
        self.assertNotIn("下一班可能重發", r.stdout)


if __name__ == "__main__":
    unittest.main()
