"""「金管會申報進度」變動區塊的金額顯示：同日同類別多筆不同金額的案件必須各自對應到正確的列，每一行都帶金額。
全部離線、合成資料（暫存目錄）。"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fsc_progress as fp  # noqa: E402
from test_fsc_progress import NOW, Base, days_ago, prow  # noqa: E402

CAT = "轉換公司債(無擔保)"
HEADER = "🔄 較上次資料版本的變動（115年檔"


def rid(code, amount, receipt="1151109", cat=CAT, seq=1):
    return f"{code}|{cat}|{receipt}|{amount}#{seq}"


class DiffBase(Base):
    def make(self, rows, added=(), vanished=(), changed=(), first_seen=None):
        first_seen = first_seen or days_ago(1)
        sha = self.d.year("115", list(rows), first_seen=first_seen,
                          history=[{"sha8": "oldoldol", "first_seen": "2026-09-01"}, {"sha8": ("115" * 22)[:8], "first_seen": first_seen}])
        self.d.year("114", [])
        self.d.check({"115": "2026-10-12", "114": "2026-10-12"})
        self.d.diff("115", "oldoldol", sha, added=added, vanished=vanished, changed=changed)
        return sha

    def section(self):
        t = self.text()
        self.assertIn(HEADER, t)
        return t.split(HEADER)[1].split("（以上為申報進度")[0]

    def lines(self, kind):
        return [ln for ln in self.section().splitlines() if ln.startswith(f"  • {kind}：")]

    def amounts(self, lines):
        return [re.search(r"｜(\d+\.\d\d億[^｜]*|金額 [^｜]*)｜", ln).group(1) for ln in lines]


class TestAddedSameDaySameCategory(DiffBase):
    def test_two_different_amounts_are_two_distinct_correct_lines(self):
        # 2059 實例：同一天、同類別，審理中是 90 億與 10 億；舊版 renderer 兩行都顯示 10 億
        rows = [prow("2059", name="甲公司", receipt="2026-11-09", amount="9000000000", rownum=714),
                prow("2059", name="甲公司", receipt="2026-11-09", amount="1000000000", rownum=715)]
        self.make(rows, added=[rid("2059", "1000000000"), rid("2059", "9000000000")])
        got = self.lines("新增")
        self.assertEqual(len(got), 2)
        self.assertEqual(sorted(self.amounts(got)), ["10.00億 台幣", "90.00億 台幣"])
        self.assertEqual(len(set(self.amounts(got))), 2)                                     # 同組內可由金額區分
        self.assertIn("新增 2、消失 0、欄位變動 0", self.section())

    def test_three_rows_same_day_same_category(self):
        rows = [prow("7728", receipt="2026-10-29", amount=a, rownum=n) for n, a in ((1, "100000000"), (2, "200000000"), (3, "300000000"))]
        self.make(rows, added=[rid("7728", a, "1151029") for a in ("100000000", "200000000", "300000000")])
        got = self.lines("新增")
        self.assertEqual(sorted(self.amounts(got)), ["1.00億 台幣", "2.00億 台幣", "3.00億 台幣"])

    def test_amount_shown_matches_the_actual_row_even_if_row_order_is_reversed(self):
        rows = [prow("1001", name="名乙", receipt="2026-11-09", amount="600000000", rownum=2),
                prow("1001", name="名乙", receipt="2026-11-09", amount="500000000", rownum=1)]
        self.make(rows, added=[rid("1001", "500000000"), rid("1001", "600000000")])
        got = self.lines("新增")
        self.assertEqual(self.amounts(got), ["5.00億 台幣", "6.00億 台幣"])                   # 依 diff 的順序，各自金額正確


class TestChangedLines(DiffBase):
    def test_changed_lines_carry_the_amount_and_same_group_lines_are_distinguishable(self):
        # 2344 實例：同日同類別兩筆（140 億與 40 億）都從審理中變成生效；舊版兩行文字完全相同
        rows = [prow("2344", name="乙公司", closing="生效", receipt="2026-10-01", amount="14000000000", eff="2026-10-10", rownum=659),
                prow("2344", name="乙公司", closing="生效", receipt="2026-10-01", amount="4000000000", eff="2026-10-10", rownum=660)]
        ch = []
        for a in ("14000000000", "4000000000"):
            ch.append({"id": rid("2344", a, "1151001"), "field": "結案類型", "old": "", "new": "生效"})
            ch.append({"id": rid("2344", a, "1151001"), "field": "生效日期", "old": "1151009", "new": "1151010"})
        self.make(rows, changed=ch)
        got = self.lines("變動")
        self.assertEqual(len(got), 4)
        self.assertEqual(len(set(got)), 4)                                                   # 沒有任何兩行文字完全相同
        by_amount = {}
        for ln in got:
            by_amount.setdefault(self.amounts([ln])[0], []).append(ln)
        self.assertEqual(sorted(by_amount), ["140.00億 台幣", "40.00億 台幣"])
        for v in by_amount.values():
            self.assertEqual(len(v), 2)
            self.assertTrue(any("結案類型：— → 生效" in x for x in v) and any("生效日期：2026-10-09 → 2026-10-10" in x for x in v))

    def test_only_the_changed_row_is_listed_and_with_its_own_amount(self):
        rows = [prow("1001", name="名甲", closing="生效", receipt="2026-10-01", amount="900000000", eff="2026-10-10"),
                prow("1001", name="名甲", closing="", receipt="2026-10-01", amount="100000000", eff="2026-10-25")]
        self.make(rows, changed=[{"id": rid("1001", "900000000", "1151001"), "field": "結案類型", "old": "", "new": "生效"}])
        got = self.lines("變動")
        self.assertEqual(len(got), 1)
        self.assertIn("9.00億 台幣", got[0])
        self.assertNotIn("1.00億", got[0])


class TestVanished(DiffBase):
    def test_vanished_shows_its_own_amount_not_the_surviving_rows(self):
        rows = [prow("3003", name="名丙", receipt="2026-09-01", amount="100000000", cat="轉換公司債(有擔保)")]      # 保留的一筆
        self.make(rows, vanished=[rid("3003", "400000000", "1150901", "轉換公司債(有擔保)")])
        got = self.lines("消失")
        self.assertEqual(len(got), 1)
        self.assertIn("4.00億", got[0])
        self.assertNotIn("1.00億", got[0])
        self.assertIn("消失 1", self.section())

    def test_vanished_and_kept_in_the_same_group(self):
        rows = [prow("3003", receipt="2026-09-01", amount="100000000"), prow("3003", receipt="2026-09-01", amount="200000000")]
        self.make(rows, vanished=[rid("3003", "300000000", "1150901")], added=[rid("3003", "200000000", "1150901")])
        self.assertIn("3.00億", self.lines("消失")[0])
        self.assertIn("2.00億 台幣", self.lines("新增")[0])


class TestIdenticalRowsAndOddAmounts(DiffBase):
    def test_exactly_identical_rows_keep_the_x2_note_and_each_shows_the_amount(self):
        rows = [prow("7751", name="名丁", receipt="2026-11-05", amount="1000000000", seq=1, rownum=1),
                prow("7751", name="名丁", receipt="2026-11-05", amount="1000000000", seq=2, rownum=2)]
        self.make(rows, added=[rid("7751", "1000000000", "1151105", seq=1), rid("7751", "1000000000", "1151105", seq=2)])
        got = self.lines("新增")
        self.assertEqual(len(got), 2)
        for ln in got:
            self.assertIn("10.00億 台幣", ln)
            self.assertIn("（來源表中完全相同的列 ×2）", ln)
        self.assertIn("其中 2 筆屬於來源表中完全相同的重複列", self.section())

    def test_different_amounts_are_not_marked_identical(self):
        rows = [prow("2059", receipt="2026-11-09", amount="9000000000"), prow("2059", receipt="2026-11-09", amount="1000000000")]
        self.make(rows, added=[rid("2059", "9000000000"), rid("2059", "1000000000")])
        for ln in self.lines("新增"):
            self.assertNotIn("×", ln)

    def test_non_numeric_amount_is_shown_as_is_and_distinguishable(self):
        rows = [prow("1001", name="名戊", receipt="2026-11-09", amount="待定", cur=""), prow("1001", name="名戊", receipt="2026-11-09", amount="300000000")]
        self.make(rows, added=[rid("1001", "待定"), rid("1001", "300000000")],
                  changed=[{"id": rid("1001", "待定"), "field": "結案類型", "old": "", "new": "生效"}])
        add = self.lines("新增")
        self.assertEqual(len(add), 2)
        self.assertTrue(any("金額 待定（非純數字，原樣顯示；幣別未載明）" in ln for ln in add))
        self.assertTrue(any("3.00億 台幣" in ln for ln in add))
        self.assertIn("金額 待定（非純數字", self.lines("變動")[0])

    def test_non_numeric_vanished_amount_from_id(self):
        self.make([], vanished=[rid("1001", "待定")])
        self.assertIn("金額 待定（非純數字，原樣顯示）", self.lines("消失")[0])


class TestNoPartialKeyMatching(DiffBase):
    def test_diff_id_whose_amount_matches_no_row_is_flagged_not_resolved_to_a_sibling(self):
        # 舊版 bug：只用 代號＋類別＋收文日期＋序號 找列，會把任何同日同類別的列當成它
        rows = [prow("2059", name="名己", receipt="2026-11-09", amount="9000000000")]
        self.make(rows, added=[rid("2059", "7770000000")])
        got = self.lines("新增")
        self.assertEqual(len(got), 1)
        self.assertIn("diff 記錄異常：目前版本找不到對應列", got[0])
        self.assertIn("77.70億", got[0])                                                      # 金額取自 diff 的 id
        self.assertNotIn("90.00億", got[0])                                                   # 不借用同日同類別的另一列
        self.assertNotIn("名己", got[0])                                                      # 也不猜公司名稱
        self.assertIn("另有 1 筆資料異常", self.section())
        self.assertEqual(self.report().counts["diff"]["115"]["bad"], 1)

    def test_changed_id_with_wrong_amount_is_flagged(self):
        rows = [prow("2344", name="名庚", closing="生效", receipt="2026-10-01", amount="4000000000", eff="2026-10-10")]
        self.make(rows, changed=[{"id": rid("2344", "5000000000", "1151001"), "field": "結案類型", "old": "", "new": "生效"}])
        got = self.lines("變動")
        self.assertIn("diff 記錄異常", got[0])
        self.assertIn("50.00億", got[0])
        self.assertNotIn("40.00億", got[0])

    def test_wrong_sequence_number_is_flagged(self):
        rows = [prow("1001", receipt="2026-11-09", amount="100000000", seq=1)]
        self.make(rows, added=[rid("1001", "100000000", seq=2)])
        self.assertIn("diff 記錄異常", self.lines("新增")[0])

    def test_parsed_rows_sharing_one_id_make_the_diff_item_ambiguous(self):
        a = prow("1001", name="名辛", receipt="2026-11-09", amount="100000000", rownum=1)
        b = prow("1001", name="名壬", receipt="2026-11-09", amount="100000000", rownum=2)          # 同一個 id（parsed 檔異常）
        self.make([a, b], added=[rid("1001", "100000000")])
        got = self.lines("新增")
        self.assertEqual(len(got), 1)
        self.assertIn("目前版本有多列使用同一個 id", got[0])
        self.assertNotIn("名辛", got[0])
        self.assertNotIn("名壬", got[0])

    def test_diff_file_listing_the_same_item_twice_is_flagged_and_not_double_counted(self):
        rows = [prow("1001", receipt="2026-11-09", amount="100000000")]
        self.make(rows, added=[rid("1001", "100000000"), rid("1001", "100000000")],
                  changed=[{"id": rid("1001", "100000000"), "field": "結案類型", "old": "", "new": "生效"}] * 2,
                  vanished=[rid("3003", "400000000", "1150901")] * 2)
        sec = self.section()
        self.assertIn("新增 1、消失 1、欄位變動 1", sec)
        self.assertEqual(sec.count("diff 記錄異常：diff 檔重複記錄同一項目"), 3)
        self.assertIn("另有 3 筆資料異常", sec)

    def test_clean_diff_has_no_anomaly_marks(self):
        rows = [prow("2059", receipt="2026-11-09", amount="9000000000"), prow("2059", receipt="2026-11-09", amount="1000000000")]
        self.make(rows, added=[rid("2059", "9000000000"), rid("2059", "1000000000")])
        self.assertNotIn("diff 記錄異常", self.section())
        self.assertNotIn("資料異常", self.section())


class TestCaseKey(unittest.TestCase):
    def test_key_contains_year_code_category_receipt_amount_and_sequence(self):
        c = fp.classify_row("115", prow("2059", receipt="2026-11-09", amount="9000000000", seq=3), fp.taipei_today(NOW))
        self.assertEqual(c.key, f"115|2059|{CAT}|1151109|9000000000#3")

    def test_two_rows_differing_only_in_amount_get_different_keys(self):
        a = fp.classify_row("115", prow("2059", amount="9000000000"), fp.taipei_today(NOW))
        b = fp.classify_row("115", prow("2059", amount="1000000000"), fp.taipei_today(NOW))
        self.assertNotEqual(a.key, b.key)

    def test_key_equals_the_archive_id_format(self):
        row = prow("2059", receipt="2026-11-09", amount="9000000000", seq=2)
        c = fp.classify_row("115", row, fp.taipei_today(NOW))
        self.assertEqual(c.key, "115|" + row["id"])


if __name__ == "__main__":
    unittest.main()
