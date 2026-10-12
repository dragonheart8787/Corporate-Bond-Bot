"""每則轉換公司債公告底下的「📑 申報表：」查詢行（fsc_progress.AnnouncementLookup 與 complete_formatter 的接線）的測試。
全部離線：資料是測試裡合成的 parsed／meta／diffs／_last_check；socket 在本模組內被封鎖。"""

import json
import os
import shutil
import socket
import sys
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import bot_listener as bl  # noqa: E402
import complete_formatter as cf  # noqa: E402
import fsc_progress as fp  # noqa: E402
import main as main_mod  # noqa: E402
import message_split as ms  # noqa: E402
from test_fsc_progress import NOW, DataDir, days_ago, prow, roc  # noqa: E402

_real_socket = socket.socket
HEAD = fp.LOOKUP_HEAD
NOTE = fp.AMBIGUOUS_NOTE


def setUpModule():
    def blocked(*a, **k):
        raise AssertionError("測試不得建立真的網路連線")
    socket.socket = blocked  # type: ignore[assignment]


def tearDownModule():
    socket.socket = _real_socket  # type: ignore[assignment]


def ann(code, name, body="公告董事會決議發行本公司國內第二次無擔保可轉換公司債 #可轉債", h="17:12:36"):
    return {"text": f"#{code} #{name} 發佈時間：20261012 {h} {body}", "date": "2026-10-12 17:12:36", "from": "x"}


class Base(unittest.TestCase):
    def setUp(self):
        self.d = DataDir()
        self.addCleanup(self.d.cleanup)

    def good(self, rows115, rows114=(), check="today"):
        self.d.year("115", list(rows115))
        self.d.year("114", list(rows114))
        if check == "today":
            self.d.check({"115": "2026-10-12", "114": "2026-10-12"})
        elif check:
            self.d.check(check)

    def lines(self, code, now=NOW):
        return fp.AnnouncementLookup(now, self.d.dir).lines_for(code)


class TestMatching(Base):
    def test_all_cb_rows_of_the_code_in_both_years_are_listed_newest_first(self):
        self.good([prow("7610", receipt="2026-04-14", closing="生效", eff="2026-04-30", amount="800000000"),
                   prow("7610", receipt="2026-10-05", amount="1200000000", eff="2026-10-22")],
                  [prow("7610", receipt="2025-03-01", closing="生效", eff="2025-03-20", amount="300000000")])
        out = self.lines("7610")
        self.assertTrue(out[0].startswith(f"{HEAD}{NOTE}（共 3 筆；資料版本：115年檔 v2 最近成功檢查 2026-10-12、114年檔 v2 最近成功檢查 2026-10-12"))
        self.assertEqual(out[1:], [
            "  • 無擔保｜12.00億 台幣｜收文日期（送件）2026-10-05｜審理中｜生效日期（預定）2026-10-22",
            "  • 無擔保｜8.00億 台幣｜收文日期（送件）2026-04-14｜已生效｜生效日期 2026-04-30",
            "  • 無擔保｜3.00億 台幣｜收文日期（送件）2025-03-01｜已生效｜生效日期 2025-03-20"])

    def test_every_state_uses_the_same_rules_as_the_progress_block(self):
        self.good([prow("1001", cat="轉換公司債(有擔保)", receipt="2026-10-06", eff="2026-10-23"),                       # 審理中 + 預定
                   prow("1001", receipt="2026-10-05", stop=roc("2026-10-02")),                                              # 停止生效
                   prow("1001", receipt="2026-10-04", stop=roc("2026-10-02"), eff="2026-10-20"),                            # 有生效日期 → 仍是審理中
                   prow("1001", receipt="2026-10-03"),                                                                 # 審理中、無預定日
                   prow("1001", receipt="2026-09-01", closing="生效", eff="2026-09-20"),
                   prow("1001", receipt="2026-08-01", closing="自行撤回", withdraw="2026-08-10"),
                   prow("1001", receipt="2026-07-01", closing="退件", reject="2026-07-05"),
                   prow("1001", cat="轉換公司債(海外無擔保)", receipt="2026-06-01", closing="廢止/撤銷", revoke="2026-06-09", eff="2026-06-02")])
        out = self.lines("1001")
        self.assertEqual(out[1:], [
            "  • 有擔保｜5.00億 台幣｜收文日期（送件）2026-10-06｜審理中｜生效日期（預定）2026-10-23",
            "  • 無擔保｜5.00億 台幣｜收文日期（送件）2026-10-05｜停止生效｜停止生效日期 2026-10-02｜生效日期（預定）—",
            "  • 無擔保｜5.00億 台幣｜收文日期（送件）2026-10-04｜審理中｜生效日期（預定）2026-10-20",
            "  • 無擔保｜5.00億 台幣｜收文日期（送件）2026-10-03｜審理中｜生效日期（預定）—",
            "  • 無擔保｜5.00億 台幣｜收文日期（送件）2026-09-01｜已生效｜生效日期 2026-09-20",
            "  …另有 3 筆未列出"])
        with mock.patch.object(fp, "LOOKUP_MAX_ROWS", 99):                       # 取消上限，檢查其餘三種狀態的格式
            allrows = self.lines("1001")[1:]
        self.assertEqual(allrows[5:], [
            "  • 無擔保｜5.00億 台幣｜收文日期（送件）2026-08-01｜已終止（自行撤回）｜自行撤回日期 2026-08-10",
            "  • 無擔保｜5.00億 台幣｜收文日期（送件）2026-07-01｜已終止（退件）｜退件日期 2026-07-05",
            "  • 海外無擔保｜5.00億 台幣｜收文日期（送件）2026-06-01｜已終止（廢止/撤銷）｜廢止/撤銷日期 2026-06-09"])

    def test_status_comes_only_from_closing_type_not_from_dates(self):
        self.good([prow("1001", receipt="2026-10-05", eff="2026-09-01")])             # 生效日期在過去但結案類型空值 → 仍是審理中
        self.assertIn("審理中｜生效日期（預定）2026-09-01", self.lines("1001")[1])

    def test_only_cb_categories_and_exact_code_match(self):
        self.good([prow("1001", cat="交換公司債(海外無擔保)"), prow("1001", cat="現金增資"), prow("10011"), prow("2001", name="1001"),
                   prow("1001", cat="轉換公司債（有擔保）")])
        out = self.lines("1001")
        self.assertIn("（共 1 筆；", out[0])
        self.assertEqual(len(out), 2)
        self.assertIn("有擔保", out[1])

    def test_never_matches_by_company_name(self):
        self.good([prow("2001", name="映興"), prow("2002", name="聯友金屬-創")])
        self.assertTrue(self.lines("9999")[0].startswith(f"{HEAD}查無此證券代號"))

    def test_more_than_5_rows_are_capped_with_a_count(self):
        self.good([prow("1001", receipt=days_ago(i + 1)) for i in range(5)])
        self.assertEqual(len(self.lines("1001")), 6)
        self.assertNotIn("另有", "\n".join(self.lines("1001")))
        self.good([prow("1001", receipt=days_ago(i + 1)) for i in range(12)])
        out = self.lines("1001")
        self.assertEqual(len(out), 1 + 5 + 1)
        self.assertEqual(out[-1], "  …另有 7 筆未列出")
        self.assertIn("共 12 筆", out[0])
        self.assertEqual(out[1].split("收文日期（送件）")[1][:10], days_ago(1))      # 新到舊

    def test_identical_rows_are_all_listed_with_the_existing_note(self):
        self.good([prow("1001", seq=1), prow("1001", seq=2, rownum=11), prow("1001", amount="900000000", seq=3, rownum=12)])
        out = self.lines("1001")
        self.assertEqual(len(out), 4)
        self.assertEqual(sum("（來源表中完全相同的列 ×2）" in ln for ln in out), 2)
        self.assertEqual(sum(ln.startswith("  • ") for ln in out), 3)                 # 不合併

    def test_no_match_line_text_and_versions(self):
        self.good([prow("2001")])
        self.assertEqual(self.lines("1001"), [
            f"{HEAD}查無此證券代號的轉換公司債申報案件（資料版本：115年檔 v2 最近成功檢查 2026-10-12、114年檔 v2 最近成功檢查 2026-10-12；表為定期更新，查無不代表尚未送件）"])

    def test_without_a_check_record_the_lookup_still_runs_and_says_so(self):
        self.good([prow("2001")], check=None)
        out = self.lines("1001")
        self.assertIn("115年檔 v2 最近成功檢查 —", out[0])

    def test_unreadable_rows_are_disclosed_on_a_no_match(self):
        self.good([prow("2001"), "garbage", {"fields": {"證券代號": "9"}}, prow("", cat="轉換公司債(無擔保)")])
        out = self.lines("1001")
        self.assertTrue(out[0].endswith("；另有 3 列無法解析的資料，結果可能不完整"))


class TestUnrecognizedCode(Base):
    EXPECT = [f"{HEAD}無法辨識證券代號，未查詢"]

    def test_none_blank_non_numeric_and_wrong_length(self):
        self.good([prow("1001")])
        for code in (None, "", "   ", "abc", "12a4", "00679B", "12", "123", "1234567", "１２３４a", "1001 1002"):
            self.assertEqual(self.lines(code), self.EXPECT, repr(code))

    def test_valid_codes_are_4_to_6_digits(self):
        self.good([prow("1001"), prow("12345"), prow("123456")])
        for code in ("1001", "12345", "123456"):
            self.assertIn("共 1 筆", self.lines(code)[0], code)

    def test_unrecognized_code_is_reported_even_when_the_data_is_unusable(self):
        shutil.rmtree(self.d.dir)
        self.assertEqual(self.lines(None), self.EXPECT)


class TestDataHealth(Base):
    def assert_refused(self, out, needle):
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith(f"{HEAD}申報表資料缺失/過舊，未查詢（原因："), out[0])
        self.assertIn(needle, out[0])
        self.assertNotIn("•", out[0])

    def test_missing_directory_and_files(self):
        self.good([prow("1001")])
        os.remove(os.path.join(self.d.dir, "parsed", "115.json"))
        self.assert_refused(self.lines("1001"), "parsed/115.json 檔案不存在")
        shutil.rmtree(self.d.dir)
        self.assert_refused(self.lines("1001"), "meta/115.json 檔案不存在")

    def test_corrupt_files_and_version_mismatch(self):
        self.good([prow("1001")])
        self.d.write("parsed/115.json", None, raw="{broken")
        self.assert_refused(self.lines("1001"), "parsed/115.json 無法讀取")
        self.d.year("115", [prow("1001")], parsed_sha="b" * 64)
        self.assert_refused(self.lines("1001"), "版本（sha256）不一致")

    def test_stale_by_last_check_date_refuses_even_though_rows_exist(self):
        self.good([prow("1001")], check={"115": days_ago(4), "114": "2026-10-12"})
        self.assert_refused(self.lines("1001"), "申報進度資料過舊(最後成功檢查 2026-10-08)")
        self.good([prow("1001")], check={"115": days_ago(3), "114": "2026-10-12"})
        self.assertIn("共 1 筆", self.lines("1001")[0])

    def test_one_year_stale_refuses_the_whole_lookup(self):
        self.good([prow("1001")], check={"115": "2026-10-12", "114": days_ago(9)})
        self.assert_refused(self.lines("1001"), "114年檔：申報進度資料過舊")

    def test_old_rule_when_no_record_and_future_first_seen(self):
        self.d.year("115", [prow("1001")], first_seen=days_ago(8))
        self.d.year("114", [])
        self.assert_refused(self.lines("1001"), "尚無檢查紀錄")
        self.d.year("115", [prow("1001")], first_seen=days_ago(-2))
        self.assert_refused(self.lines("1001"), "晚於今日")

    def test_the_gate_is_the_same_as_the_progress_block(self):
        for check in ({"115": "2026-10-12", "114": "2026-10-12"}, {"115": days_ago(4), "114": "2026-10-12"}, None):
            self.good([prow("1001")], check=check)
            now = NOW
            asm_state = fp.build_report(now, self.d.dir).state
            refused = self.lines("1001")[0].startswith(f"{HEAD}申報表資料缺失/過舊")
            self.assertEqual(refused, asm_state != "正常", check)


class TestRowAnomalies(Base):
    def test_bad_rows_for_the_code_are_marked_counted_and_do_not_break_the_rest(self):
        self.good([prow("1001", closing="生效", eff=""), prow("1001", closing="駁回"), prow("1001", closing="生效", eff=days_ago(-1)),
                   prow("1001", receipt="2026-10-05")])
        out = self.lines("1001")
        self.assertIn("共 4 筆", out[0])
        self.assertIn("其中資料異常 3 列", out[0])
        self.assertEqual(sum("資料異常：" in ln for ln in out), 3)
        self.assertIn("審理中", out[1])                                                # 正常列在前，異常列在後
        self.assertIn("生效日期欄位無值", "\n".join(out))
        self.assertIn("未知的結案類型", "\n".join(out))
        self.assertIn("生效日期晚於今日", "\n".join(out))

    def test_unexpected_exception_on_one_row_is_marked_not_raised(self):
        self.good([prow("1001"), prow("1001", receipt="2026-10-04")])
        real = fp.classify_row
        calls = {"n": 0}

        def flaky(year, row, today):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("boom")
            return real(year, row, today)
        with mock.patch.object(fp, "classify_row", flaky):
            out = self.lines("1001")
        self.assertIn("解析例外 ValueError", "\n".join(out))
        self.assertEqual(sum("•" in ln for ln in out), 2)

    def test_a_failure_while_loading_is_reported_not_raised(self):
        self.good([prow("1001")])
        with mock.patch.object(fp, "assess_sources", side_effect=RuntimeError("boom")):
            lk = fp.AnnouncementLookup(NOW, self.d.dir)
            out = lk.lines_for("1001")
        self.assertEqual(out, [f"{HEAD}申報表資料缺失/過舊，未查詢（原因：讀取失敗（RuntimeError））"])

    def test_lines_for_never_raises(self):
        self.good([prow("1001")])
        lk = fp.AnnouncementLookup(NOW, self.d.dir)
        with mock.patch.object(lk, "_lines_for", side_effect=RuntimeError("x")):
            out = lk.lines_for("1001")
        self.assertIn("申報表資料缺失/過舊，未查詢", out[0])


class TestTimezone(Base):
    def test_taipei_midnight_decides_future_effective_dates_and_the_stale_gap(self):
        self.good([prow("1001", closing="生效", eff="2026-10-13")], check={"115": "2026-10-09", "114": "2026-10-09"})
        before = datetime(2026, 10, 12, 15, 59, tzinfo=timezone.utc)      # 台北 10-12 23:59：生效日期 10-13 是未來 → 異常；檢查日差 3 天 → 可用
        after = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)        # 台北 10-13 00:00：檢查日差 4 天 → 過舊
        b = self.lines("1001", before)
        self.assertIn("資料異常", "\n".join(b))
        self.assertTrue(self.lines("1001", after)[0].startswith(f"{HEAD}申報表資料缺失/過舊"))
        self.good([prow("1001", closing="生效", eff="2026-10-13")], check={"115": "2026-10-13", "114": "2026-10-13"})
        ok = self.lines("1001", after)
        self.assertNotIn("資料異常", "\n".join(ok))
        self.assertIn("已生效｜生效日期 2026-10-13", ok[1])


MARKERS = ("轉換公司債相關公告", "【轉換公司債", "無轉換公司債", "📢 澄清媒體報導", "💰 財務資訊", "👥 人事異動",
           "⚠️ 注意交易", "📋 重大訊息", "📄 其他", "── 其他公告", "📋 其他公告")


class TestFormatterIntegration(Base):
    def render(self, rows, patch_lookup=None):
        with mock.patch.dict(os.environ, {"FSC_DATA_DIR": self.d.dir}):
            f = cf.CompleteFormatter()
            return f.format_with_convertible_bond_priority(rows)

    def test_line_sits_after_the_analysis_and_leaves_it_untouched(self):
        self.good([prow("7610", receipt="2026-04-14", closing="生效", eff="2026-04-30", amount="800000000")])
        t = self.render([ann("7610", "聯友金屬-創")])
        i = t.index("🔎 公司情況研判：")
        j = t.index(HEAD)
        k = t.index("=" * 80, j)
        self.assertLess(i, j)
        self.assertLess(j, k)
        self.assertIn("（共 1 筆；", t[j:k])                                        # 用的是公告的證券代號 7610，不是公司名稱
        self.assertIn("已生效｜生效日期 2026-04-30", t[j:k])
        self.assertEqual(t[i:j].rstrip("\n"), "🔎 公司情況研判：\n  ℹ 中性：可能規劃/進行可轉債")
        with mock.patch.object(cf.AnnouncementLookup, "lines_for", return_value=[]):
            base = self.render([ann("7610", "聯友金屬-創")])
        strip = lambda s: [ln for ln in s.splitlines() if ln.strip() and "生成時間" not in ln]
        added = [ln for ln in strip(t) if ln not in strip(base)]
        self.assertTrue(added and all(ln.startswith(HEAD) or ln.startswith("  • ") for ln in added))

    def test_line_is_added_even_without_an_analysis_block(self):
        self.good([prow("7610")])
        row = ann("7610", "聯友金屬-創", body="公告轉債相關事宜", h="09:00:00")
        t = self.render([row])
        seg = t.split("【轉換公司債 - 第 1 則】")[1].split(fp.TITLE)[0]
        self.assertNotIn("公司情況研判", seg)                                      # 這則公告沒有研判區塊
        self.assertIn(HEAD, seg)
        self.assertLess(seg.index("公告轉債相關事宜"), seg.index(HEAD))

    def test_non_cb_announcements_get_no_lookup_line(self):
        self.good([prow("2801")])
        other = {"text": "#2801 #彰銀 發佈時間：20261011 16:17:31 公告授信資產轉讓", "date": "2026-10-11 17:00:00", "from": "x"}
        t = self.render([other])
        self.assertEqual(t.count(HEAD), 0)

    def test_announcement_without_company_info_is_unrecognized_and_name_is_never_used(self):
        self.good([prow("7610", name="聯友金屬-創")])
        row = {"text": "聯友金屬-創 發佈時間：20261012 17:12:36 公告發行可轉換公司債 #可轉債", "date": "x", "from": "x"}
        t = self.render([row])
        self.assertIn(f"{HEAD}無法辨識證券代號，未查詢", t)
        self.assertNotIn("無擔保｜", t.split(HEAD)[1].split("=" * 80)[0])

    def test_two_announcements_share_one_data_read_and_a_new_report_rereads(self):
        self.good([prow("7610"), prow("3597")])
        calls = []
        real = fp.assess_sources
        with mock.patch.dict(os.environ, {"FSC_DATA_DIR": self.d.dir}), mock.patch.object(fp, "assess_sources", lambda t, d: calls.append(1) or real(t, d)):
            f = cf.CompleteFormatter()
            f.format_with_convertible_bond_priority([ann("7610", "A"), ann("3597", "B"), ann("7610", "A")])
            n1 = len(calls)
            f.format_with_convertible_bond_priority([ann("7610", "A")])
        self.assertEqual(n1, 2)                       # 區塊一次 + 查詢一次（不是每則公告各讀一次）
        self.assertEqual(len(calls), 4)               # 下一份報告重新讀

    def test_markers_and_announcement_count_are_not_disturbed_in_every_data_state(self):
        states = {"ok": lambda: self.good([prow("7610"), prow("7610", receipt="2026-10-01"), prow("3597", cat="轉換公司債(有擔保)")]),
                  "nomatch": lambda: self.good([prow("1111")]),
                  "bad": lambda: self.good([prow("7610", closing="駁回")]),
                  "stale": lambda: self.good([prow("7610")], check={"115": days_ago(9), "114": days_ago(9)}),
                  "missing": lambda: shutil.rmtree(self.d.dir)}
        for name, setup in states.items():
            setup()
            t = self.render([ann("7610", "A"), ann("3597", "B")])
            for blk in t.split(HEAD)[1:]:
                seg = blk.split("=" * 80)[0]
                for m in MARKERS:
                    self.assertNotIn(m, seg, f"{name}: {m}")
            for flt in (main_mod.filter_cb_only, bl.filter_cb_only):
                out = flt(t)
                self.assertIn("共 2 則", out, name)
                self.assertEqual(out.count(HEAD), 2, name)
                self.assertFalse(main_mod._is_empty_result(out), name)
            self.d = DataDir()
            self.addCleanup(self.d.cleanup)

    def test_no_cb_day_has_no_lookup_lines_and_the_empty_detection_is_unchanged(self):
        self.good([prow("7610")])
        t = self.render([{"text": "#2801 #彰銀 發佈時間：20261011 16:17:31 公告授信資產轉讓", "date": "x", "from": "x"}])
        for flt in (main_mod.filter_cb_only, bl.filter_cb_only):
            out = flt(t)
            self.assertIn("今日無轉換公司債相關公告。", out)
            self.assertNotIn(HEAD, out)
            self.assertTrue(main_mod._is_empty_result(out))

    def test_progress_block_content_is_identical_with_or_without_the_lookup_lines(self):
        self.good([prow("7610", receipt="2026-10-05")])
        with mock.patch.dict(os.environ, {"FSC_DATA_DIR": self.d.dir}):
            with_l = cf.CompleteFormatter().format_with_convertible_bond_priority([ann("7610", "A")])
            with mock.patch.object(cf.AnnouncementLookup, "lines_for", return_value=[]):
                without = cf.CompleteFormatter().format_with_convertible_bond_priority([ann("7610", "A")])
        self.assertEqual(fp.extract_progress_block(with_l), fp.extract_progress_block(without))
        self.assertTrue(fp.extract_progress_block(with_l))


class TestSplittingWithLookupLines(Base):
    def test_long_report_with_many_announcements_splits_on_newlines_and_keeps_case_lines_whole(self):
        self.good([prow(str(1000 + i), receipt=days_ago(j + 1)) for i in range(40) for j in range(6)])
        rows = [ann(str(1000 + i), f"公司{i}", h=f"{i % 24:02d}:00:00") for i in range(40)]
        t = self.render_with(rows)
        self.assertGreater(len(t), 12000)
        chunks = ms.split_message(t)
        orig = set(t.split("\n"))
        for c in chunks:
            self.assertLessEqual(ms.ulen(c), ms.LIMIT)
            for ln in c.split("\n"):
                self.assertIn(ln, orig)                                   # 沒有任何一行被切開
        self.assertEqual([ln for ln in "\n".join(chunks).split("\n") if ln.strip()], [ln for ln in t.split("\n") if ln.strip()])
        self.assertGreater(len(chunks), 3)

    def render_with(self, rows):
        with mock.patch.dict(os.environ, {"FSC_DATA_DIR": self.d.dir}):
            return cf.CompleteFormatter().format_with_convertible_bond_priority(rows)

    def test_lookup_rows_are_single_lines_so_no_case_is_ever_split_across_messages(self):
        self.good([prow("7610", receipt=days_ago(j + 1)) for j in range(8)])
        out = fp.AnnouncementLookup(NOW, self.d.dir).lines_for("7610")
        self.assertTrue(all("\n" not in ln for ln in out))
        self.assertEqual(sum(ln.startswith("  • ") for ln in out), 5)


class TestOffline(unittest.TestCase):
    def test_lookup_code_has_no_network_or_process_calls(self):
        src = open(os.path.join(ROOT, "fsc_progress.py"), encoding="utf-8").read()
        for bad in ("import requests", "urllib", "http.client", "socket", "subprocess", "telethon"):
            self.assertNotIn(bad, src, bad)


if __name__ == "__main__":
    unittest.main()
