"""「金管會申報進度」報告區塊（fsc_progress.py 與其接進報告／日報／/cb／/all／/status 的部分）的測試。
全部離線：資料是測試裡合成的 parsed／meta／diffs；socket 在本模組內被封鎖，真的連線會直接失敗。"""

import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import archive_fsc as af  # noqa: E402
import bot_listener as bl  # noqa: E402
import complete_formatter as cf  # noqa: E402
import fsc_progress as fp  # noqa: E402
import fsc_sources as fs  # noqa: E402
import main as main_mod  # noqa: E402

TW = fp.TW
_real_socket = socket.socket
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=TW)        # 今日（台北）= 2026-10-12


def setUpModule():
    def blocked(*a, **k):
        raise AssertionError("測試不得建立真的網路連線")
    socket.socket = blocked  # type: ignore[assignment]


def tearDownModule():
    socket.socket = _real_socket  # type: ignore[assignment]


def roc(iso):
    """'2026-10-12' → '1151012'"""
    y, m, d = iso.split("-")
    return f"{int(y) - 1911:03d}{m}{d}"


def days_ago(n, base="2026-10-12"):
    d = datetime.strptime(base, "%Y-%m-%d").date() - timedelta(days=n)
    return d.isoformat()


def prow(code="1111", name="測試公司", cat="轉換公司債(無擔保)", closing="", receipt="2026-10-05", amount="500000000", cur="台幣",
         nature="十二日制", fix="", stop="", lift="", eff="", revoke="", withdraw="", reject="", flags=(), seq=1, rownum=10):
    fields = {
        "證券代號": code, "公司型態": "上市", "結案類型": closing, "公司名稱": name, "承銷商": "某券商", "案件類別": cat,
        "金額": amount, "幣別": cur, "發行價格": "", "收文日期": roc(receipt) if receipt and "-" in receipt else receipt,
        "自動補正日期": fix, "停止生效日期": stop, "解除生效日期": lift, "生效日期": roc(eff) if eff and "-" in eff else eff,
        "廢止/撤銷日期": roc(revoke) if revoke and "-" in revoke else revoke,
        "自行撤回日期": roc(withdraw) if withdraw and "-" in withdraw else withdraw,
        "退件日期": roc(reject) if reject and "-" in reject else reject, "案件性質": nature,
    }
    return {"id": f"{code}|{cat}|{fields['收文日期']}|{amount}#{seq}", "row": rownum, "key": "k", "key_parts": [], "dup_seq": seq,
            "dup_count": 1, "fields": fields, "dates_iso": {}, "numbers": {}, "flags": list(flags)}


def fix_dates(*isos):
    return ",".join(roc(i) for i in isos)


class DataDir:
    """在暫存目錄寫一份合成的 data/fsc。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp()

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, rel, obj, raw=None):
        p = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(raw if raw is not None else json.dumps(obj, ensure_ascii=False))

    def year(self, year, rows, *, first_seen="2026-10-12", sha=None, version="2", history=None, parsed_sha=None, **meta_extra):
        sha = sha or (year * 22)[:64].ljust(64, "a")
        meta = {"sha256": sha, "first_seen": first_seen, "file_version": version, "page_update_text": "2026-10-12",
                "filename_decoded": f"{year}file.xlsx", "history": history or [{"sha8": sha[:8], "first_seen": first_seen}]}
        meta.update(meta_extra)
        self.write(f"meta/{year}.json", meta)
        self.write(f"parsed/{year}.json", {"sha256": parsed_sha or sha, "rows": rows})
        return sha

    def check(self, years, raw=None):
        self.write("_last_check.json", {"version": 1, "years": years}, raw=raw)

    def diff(self, year, old8, new_sha, added=(), vanished=(), changed=(), name=None):
        self.write(name or f"diffs/{year}_{old8}_{new_sha[:8]}.json",
                   {"year": year, "old_sha256": old8 * 8, "new_sha256": new_sha, "added": list(added), "vanished": list(vanished), "changed": list(changed)})


class Base(unittest.TestCase):
    def setUp(self):
        self.d = DataDir()
        self.addCleanup(self.d.cleanup)

    def report(self, now=NOW):
        return fp.build_report(now, self.d.dir)

    def text(self, now=NOW):
        return self.report(now).text

    def good(self, rows115, rows114=(), check="today", **kw):
        self.d.year("115", list(rows115), **kw)
        self.d.year("114", list(rows114), **kw)
        if check == "today":                      # 預設：兩個年度今天都檢查成功
            self.d.check({"115": "2026-10-12", "114": "2026-10-12"})
        elif check:
            self.d.check(check)


# ─────────────────────────────────────────
# 狀態判斷
# ─────────────────────────────────────────
class TestStatusRules(Base):
    def test_four_states_land_in_their_sections(self):
        rows = [
            prow("1001", closing="", receipt="2026-10-05"),
            prow("1002", closing="生效", receipt="2026-09-20", eff="2026-10-08"),
            prow("1003", closing="自行撤回", withdraw="2026-10-09"),
            prow("1004", closing="退件", reject="2026-10-10"),
            prow("1005", closing="廢止/撤銷", revoke="2026-10-11", eff="2026-09-01"),
        ]
        self.good(rows)
        rep = self.report()
        t = rep.text
        self.assertEqual((rep.counts["reviewing"], rep.counts["effective_total"], rep.counts["terminated_total"], rep.counts["anomalies"]), (1, 1, 3, 0))
        rev = t.split("⏳ 審理中")[1].split("✅ 近期已生效")[0]
        eff = t.split("✅ 近期已生效")[1].split("🛑 近期終止")[0]
        term = t.split("🛑 近期終止")[1]
        self.assertIn("1001", rev)
        self.assertNotIn("1002", rev)
        self.assertIn("1002", eff)
        self.assertIn("生效日期 2026-10-08", eff)
        for code in ("1003", "1004", "1005"):
            self.assertIn(code, term)
            self.assertNotIn(code, rev)
        self.assertIn("自行撤回日期 2026-10-09", term)
        self.assertIn("退件日期 2026-10-10", term)
        self.assertIn("廢止/撤銷日期 2026-10-11", term)

    def test_status_comes_only_from_closing_type_never_from_dates(self):
        # 結案類型空值但生效日期是過去 → 仍是審理中（不因日期過了就推成已生效）
        self.good([prow("1001", closing="", eff="2026-09-01")])
        rep = self.report()
        self.assertEqual((rep.counts["reviewing"], rep.counts["effective_total"]), (1, 0))
        self.assertIn("生效日期（預定）2026-09-01", rep.text)

    def test_reviewing_effective_date_is_only_ever_labelled_scheduled(self):
        self.good([prow("1001", closing="", eff="2026-10-20"), prow("1002", closing="", eff="")])
        t = self.text()
        rev = t.split("⏳ 審理中")[1].split("✅ 近期已生效")[0]
        self.assertIn("生效日期（預定）2026-10-20", rev)
        self.assertIn(f"生效日期（預定）{fp.DASH}", rev)
        self.assertNotIn("實際", rev)

    def test_stop_effect_tag_requires_stop_date_and_no_effective_date(self):
        self.good([prow("1001", stop="2026-10-02"), prow("1002", stop="2026-10-02", eff="2026-10-20"), prow("1003")])
        rev = self.text().split("⏳ 審理中")[1].split("✅ 近期已生效")[0]
        by = {ln.split()[1]: ln for ln in rev.splitlines() if ln.startswith("  • ")}
        self.assertIn("停止生效", by["1001"])
        self.assertNotIn("停止生效", by["1002"])
        self.assertNotIn("停止生效", by["1003"])
        self.assertIn("停止生效日期 2026-10-02", rev)

    def test_original_column_names_are_used_and_not_reworded(self):
        self.good([prow("1001", fix=fix_dates("2026-10-01", "2026-10-03"), stop="2026-10-04", lift="2026-10-06", eff="2026-10-20"),
                   prow("1002", closing="廢止/撤銷", revoke="2026-10-10")])
        t = self.text()
        for name in ("收文日期（送件）", "自動補正日期", "停止生效日期", "解除生效日期", "生效日期（預定）", "廢止/撤銷日期"):
            self.assertIn(name, t)
        self.assertIn("解除生效日期 2026-10-06", t)
        for bad in ("解除停止", "解除限制", "恢復", "送件日", "確定送件"):
            self.assertNotIn(bad, t)
        self.assertIn("自動補正日期 2 次", t)

    def test_missing_values_show_a_dash_and_nothing_is_inferred(self):
        self.good([prow("1001", closing="", receipt="2026-10-05", nature="", eff="", cur="", name="")])
        t = self.text()
        self.assertIn(f"{fp.DASH}｜無擔保", t)                      # 公司名稱缺值
        self.assertIn("幣別未載明", t)
        rev = t.split("⏳ 審理中")[1].split("✅ 近期已生效")[0]
        self.assertTrue(rev.rstrip().endswith(fp.DASH) or f"｜{fp.DASH}" in rev)

    def test_unknown_closing_type_is_a_data_anomaly_not_guessed(self):
        self.good([prow("1001", closing="駁回"), prow("1002")])
        rep = self.report()
        self.assertEqual(rep.counts["anomalies"], 1)
        self.assertIn("未知的結案類型", rep.text)
        self.assertEqual(rep.counts["reviewing"], 1)


# ─────────────────────────────────────────
# 範圍與欄位顯示
# ─────────────────────────────────────────
class TestScopeAndFields(Base):
    def test_only_the_three_convertible_categories_are_in_scope(self):
        rows = [prow("1001", cat="轉換公司債(無擔保)"), prow("1002", cat="轉換公司債(有擔保)"), prow("1003", cat="轉換公司債(海外無擔保)"),
                prow("1004", cat="交換公司債(海外無擔保)"), prow("1005", cat="現金增資"), prow("1006", cat="公司債"),
                prow("1007", cat="轉換公司債（有擔保）"),       # 全形括號也算（NFKC）
                prow("1008", cat="轉換公司債其他"), prow("1009", cat="轉換公司債")]     # 沒有括號的不是這三類
        self.good(rows)
        rep = self.report()
        self.assertEqual(rep.counts["in_scope"], 4)
        t = rep.text
        for code in ("1001", "1002", "1003", "1007"):
            self.assertIn(code, t)
        for code in ("1004", "1005", "1006", "1008", "1009"):
            self.assertNotIn(code, t)
        self.assertIn("交換公司債", fp.EXCLUDED_NOTE)
        self.assertIn("交換公司債", open(os.path.join(ROOT, "fsc_progress.py"), encoding="utf-8").read())

    def test_both_years_are_read_and_combined(self):
        self.good([prow("1001")], [prow("2001", receipt="2025-12-30", closing="生效", eff=days_ago(3))])
        t = self.text()
        self.assertIn("1001", t)
        self.assertIn("2001", t)
        self.assertIn("115年檔", t)
        self.assertIn("114年檔", t)

    def test_amount_in_hundred_million_with_currency(self):
        self.good([prow("1001", amount="1500000000", cur="台幣"), prow("1002", amount="750000000", cur="美元", cat="轉換公司債(海外無擔保)"),
                   prow("1003", amount="250000000", cur="台幣 ")])
        t = self.text()
        self.assertIn("15.00億 台幣", t)
        self.assertIn("7.50億 美元", t)
        self.assertIn("2.50億 台幣", t)

    def test_non_numeric_amount_is_shown_verbatim_and_marked(self):
        self.good([prow("1001", amount="1.0000000000000001E-9", cur=""), prow("1002", amount="1,200,000,000"), prow("1003", amount="")])
        t = self.text()
        self.assertIn("金額 1.0000000000000001E-9（非純數字，原樣顯示；幣別未載明）", t)
        self.assertIn("金額 1,200,000,000（非純數字，原樣顯示；台幣）", t)
        self.assertIn(f"金額 {fp.DASH}", t)
        self.assertNotIn("億 幣別未載明", t.split("1.0000000000000001E-9")[0][-30:])

    def test_same_day_same_category_rows_are_listed_one_per_line_never_merged(self):
        rows = [prow("1001", amount="300000000", flags=["同日同類別多筆"], seq=1), prow("1001", amount="700000000", flags=["同日同類別多筆"], seq=1, rownum=11),
                prow("1001", amount="700000000", seq=2, rownum=12)]
        self.good(rows)
        rev = self.text().split("⏳ 審理中")[1].split("✅ 近期已生效")[0]
        lines = [ln for ln in rev.splitlines() if ln.startswith("  • 1001")]
        self.assertEqual(len(lines), 3)
        self.assertEqual(sum("同日同類別多筆" in ln for ln in lines), 2)
        self.assertIn("3.00億", rev)
        self.assertEqual(rev.count("7.00億"), 2)

    def test_reviewing_sorted_by_receipt_date_newest_first_with_stable_ties(self):
        self.good([prow("1111", receipt="2026-09-01"), prow("2000", receipt="2026-10-01"), prow("1000", receipt="2026-10-01", seq=1),
                   prow("1000", receipt="2026-10-01", seq=2, rownum=11), prow("0500", receipt="bad"), prow("9000", receipt="2026-10-06")])
        rev = self.text().split("⏳ 審理中")[1].split("✅ 近期已生效")[0]
        codes = [ln.split()[1] for ln in rev.splitlines() if ln.startswith("  • ")]
        self.assertEqual(codes, ["9000", "1000", "1000", "2000", "1111", "0500"])       # 無法解析的收文日期排最後

    def test_unparseable_dates_are_shown_as_original_values_not_dropped(self):
        self.good([prow("1001", receipt="1150231", eff="abc", fix="1150101,zz")])
        t = self.text()
        self.assertIn("1150231（原值，無法解析）", t)
        self.assertIn("abc（原值，無法解析）", t)
        self.assertIn("自動補正日期 2 次", t)


# ─────────────────────────────────────────
# 近期視窗與邊界
# ─────────────────────────────────────────
class TestWindows(Base):
    def test_effective_window_is_inclusive_14_days_back_to_today(self):
        rows = [prow(f"10{n:02d}", closing="生效", eff=days_ago(n)) for n in (0, 1, 13, 14, 15, 16)]
        self.good(rows)
        rep = self.report()
        eff = rep.text.split("✅ 近期已生效")[1].split("🛑 近期終止")[0]
        for code, expect in (("1000", True), ("1001", True), ("1013", True), ("1014", True), ("1015", False), ("1016", False)):
            self.assertEqual(code in eff, expect, code)
        self.assertEqual(rep.counts["effective_recent"], 4)
        self.assertEqual(rep.counts["effective_total"], 6)

    def test_effective_after_today_is_a_data_anomaly_not_listed_as_recent(self):
        self.good([prow("1001", closing="生效", eff=days_ago(-1))])
        rep = self.report()
        self.assertEqual((rep.counts["anomalies"], rep.counts["effective_recent"]), (1, 0))
        self.assertIn("生效日期晚於今日", rep.text)

    def test_termination_window_inclusive_and_sorted_newest_first(self):
        rows = [prow("1001", closing="自行撤回", withdraw=days_ago(14)), prow("1002", closing="退件", reject=days_ago(15)),
                prow("1003", closing="廢止/撤銷", revoke=days_ago(0)), prow("1004", closing="自行撤回", withdraw=days_ago(5))]
        self.good(rows)
        term = self.text().split("🛑 近期終止")[1]
        codes = [ln.split()[1] for ln in term.splitlines() if ln.startswith("  • ")]
        self.assertEqual(codes, ["1003", "1004", "1001"])

    def test_termination_without_its_date_is_an_anomaly_not_silently_dropped(self):
        self.good([prow("1001", closing="自行撤回", withdraw=""), prow("1002", closing="退件", reject="1150231")])
        rep = self.report()
        self.assertEqual(rep.counts["anomalies"], 2)
        self.assertIn("自行撤回日期欄位無值", rep.text)
        self.assertIn("退件日期日期無法解析", rep.text)

    def test_effective_with_missing_or_multiple_dates_is_an_anomaly(self):
        self.good([prow("1001", closing="生效", eff=""), prow("1002", closing="生效", eff=fix_dates("2026-10-01", "2026-10-02"))])
        rep = self.report()
        self.assertEqual(rep.counts["anomalies"], 2)
        self.assertIn("結案類型=生效，但生效日期欄位無值", rep.text)
        self.assertIn("結案類型=生效，但生效日期欄位含多個日期", rep.text)
        self.assertNotIn("解析例外", rep.text)

    def test_effective_list_is_sorted_by_effective_date_newest_first(self):
        self.good([prow("1001", closing="生效", eff=days_ago(9)), prow("2001", closing="生效", eff=days_ago(1)), prow("3001", closing="生效", eff=days_ago(5))])
        eff = self.text().split("✅ 近期已生效")[1].split("🛑 近期終止")[0]
        self.assertEqual([ln.split()[1] for ln in eff.splitlines() if ln.startswith("  • ")], ["2001", "3001", "1001"])

    def test_termination_dated_after_today_is_not_listed_as_recent(self):
        self.good([prow("1001", closing="自行撤回", withdraw=days_ago(-1)), prow("1002", closing="退件", reject=days_ago(1))])
        rep = self.report()
        self.assertEqual((rep.counts["terminated_total"], rep.counts["terminated_recent"], rep.counts["anomalies"]), (2, 1, 0))
        self.assertNotIn("1001", rep.text.split("🛑 近期終止")[1])

    def test_taipei_midnight_crossing_uses_taipei_date_not_utc(self):
        # UTC 2026-10-12 16:30 = 台北 2026-10-13 00:30 → 今日是 10-13；生效日期 10-13 不是「未來」
        self.good([prow("1001", closing="生效", eff="2026-10-13"), prow("1002", closing="生效", eff="2026-09-29")])
        now = datetime(2026, 10, 12, 16, 30, tzinfo=timezone.utc)
        rep = self.report(now)
        self.assertEqual(rep.counts["anomalies"], 0)
        self.assertIn("2026-09-29 ～ 2026-10-13", rep.text)
        self.assertEqual(rep.counts["effective_recent"], 2)
        # 台北 23:59（UTC 15:59）仍是 10-12：10-13 的生效日期是未來 → 異常
        late = datetime(2026, 10, 12, 15, 59, tzinfo=timezone.utc)
        self.assertEqual(self.report(late).counts["anomalies"], 1)

    def test_result_does_not_depend_on_the_process_timezone(self):
        self.good([prow("1001", closing="生效", eff="2026-10-13")])
        now = datetime(2026, 10, 12, 16, 30, tzinfo=timezone.utc)
        results = set()
        old = os.environ.get("TZ")
        try:
            for tz in ("UTC", "America/Los_Angeles", "Asia/Tokyo", "Pacific/Kiritimati"):
                os.environ["TZ"] = tz
                time.tzset()
                results.add(self.text(now))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.assertEqual(len(results), 1)

    def test_naive_now_is_treated_as_taipei(self):
        self.good([prow("1001", closing="生效", eff="2026-10-13")])      # naive 2026-10-12 20:00 視為台北 → 10-13 是未來 → 異常
        old = os.environ.get("TZ")
        try:
            for tz in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):    # 不能被當成 runner 本地時間再換算
                os.environ["TZ"] = tz
                time.tzset()
                self.assertEqual(self.report(datetime(2026, 10, 12, 20, 0)).counts["anomalies"], 1, tz)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()


# ─────────────────────────────────────────
# diff 區段
# ─────────────────────────────────────────
class TestDiffSection(Base):
    def setUp(self):
        super().setUp()
        self.cat = "轉換公司債(無擔保)"

    def make(self, first_seen, **diff):
        rows = [prow("1001", closing="生效", eff=days_ago(1), name="新增公司", amount="900000000"),
                prow("1002", closing="生效", eff=days_ago(2), name="變動公司", amount="100000000", receipt="2026-10-01")]
        sha = self.d.year("115", rows, first_seen=first_seen,
                          history=[{"sha8": "oldoldol", "first_seen": "2026-09-01"}, {"sha8": ("115" * 22)[:8], "first_seen": first_seen}])
        self.d.year("114", [])
        if diff is not None:
            self.d.diff("115", "oldoldol", sha, **diff)
        return sha

    def test_shown_when_first_seen_within_3_days_and_hidden_after(self):
        for age, shown in ((0, True), (3, True), (4, False)):
            self.make(days_ago(age), changed=[{"id": f"1002|{self.cat}|1151001|100000000#1", "field": "結案類型", "old": "", "new": "生效"}])
            t = self.text()
            self.assertEqual("較上次資料版本的變動（115年檔" in t, shown, age)

    def test_added_vanished_changed_are_listed_for_cb_categories_only(self):
        self.make(days_ago(1),
                  added=[f"1001|{self.cat}|1151005|900000000#1", "9999|現金增資|1151005|1#1"],
                  vanished=[f"3003|轉換公司債(有擔保)|1150901|400000000#1", "8888|公司債|1150901|1#1"],
                  changed=[{"id": f"1002|{self.cat}|1151001|100000000#1", "field": "結案類型", "old": "", "new": "生效"},
                           {"id": f"1002|{self.cat}|1151001|100000000#1", "field": "生效日期", "old": "", "new": "1151008"},
                           {"id": "7777|現金增資|1151001|1#1", "field": "結案類型", "old": "", "new": "生效"}])
        t = self.text()
        sec = t.split("🔄 較上次資料版本的變動（115年檔")[1].split("（以上為申報進度")[0]
        self.assertIn("新增 1、消失 1、欄位變動 2", sec)
        self.assertIn("新增：1001 新增公司｜無擔保｜9.00億 台幣", sec)
        self.assertIn("消失：3003｜有擔保", sec)
        self.assertIn("變動：1002 變動公司｜無擔保｜收文日期（送件）2026-10-01｜結案類型：— → 生效", sec)
        self.assertIn("生效日期：— → 2026-10-08", sec)
        for noise in ("9999", "8888", "7777"):
            self.assertNotIn(noise, sec)

    def test_no_previous_version_means_no_diff_section_at_all(self):
        sha = self.make(days_ago(1))
        meta = json.load(open(os.path.join(self.d.dir, "meta", "115.json"), encoding="utf-8"))
        meta["history"] = [meta["history"][-1]]                                  # 只有一個版本
        self.d.write("meta/115.json", meta)
        os.remove(os.path.join(self.d.dir, "diffs", os.listdir(os.path.join(self.d.dir, "diffs"))[0]))
        t = self.text()
        self.assertNotIn("較上次資料版本的變動", t)
        self.assertNotIn("首次留存", t)
        self.assertNotIn("沒有可比對", t)
        self.assertNotIn("115", self.report().counts["diff"])

    def test_previous_version_exists_but_diff_file_is_missing_is_reported_not_hidden(self):
        self.make(days_ago(1))
        os.remove(os.path.join(self.d.dir, "diffs", os.listdir(os.path.join(self.d.dir, "diffs"))[0]))
        rep = self.report()
        self.assertIn("此版本有前一版，但找不到對應的 diffs 檔", rep.text)
        self.assertEqual(rep.counts["diff"]["115"]["bad"], 1)

    def test_year_without_previous_version_shows_no_diff_section_even_when_the_other_year_has_one(self):
        self.make(days_ago(1), added=[f"1001|{self.cat}|1151005|900000000#1"])
        t = self.text()
        self.assertEqual(t.count("較上次資料版本的變動"), 1)
        self.assertIn("115年檔", t.split("較上次資料版本的變動")[1][:30])

    def test_corrupt_or_malformed_diff_is_reported_not_fatal(self):
        sha = self.make(days_ago(1))
        self.d.write(f"diffs/115_oldoldol_{sha[:8]}.json", None, raw="{broken")
        rep = self.report()
        self.assertEqual(rep.state, "正常")
        self.assertIn("資料異常：diffs 檔無法使用", rep.text)
        self.assertEqual(rep.counts["diff"]["115"]["bad"], 1)
        self.d.write(f"diffs/115_oldoldol_{sha[:8]}.json", {"added": 1})
        self.assertIn("資料異常：diffs 檔無法使用", self.text())
        self.d.diff("115", "oldoldol", sha, added=["bad-id", 5], changed=["x", {"id": "bad", "field": "結案類型"}, {"id": f"1002|{self.cat}|1151001|1#1", "field": "公司名稱", "old": "a", "new": "b"}])
        t = self.text()
        self.assertIn("另有 5 筆資料異常", t)

    def test_diff_is_chosen_by_the_current_version_sha(self):
        sha = self.make(days_ago(1), added=[f"1001|{self.cat}|1151005|900000000#1"])
        self.d.diff("115", "zzzzzzzz", "f" * 64, added=[f"3003|{self.cat}|1151001|1#1"])           # 別的版本的 diff
        t = self.text()
        self.assertIn("新增 1、消失 0", t)
        self.assertNotIn("3003", t)

    def test_diff_choice_without_history_still_matches_only_the_current_sha(self):
        sha = self.make(days_ago(1), added=[f"1001|{self.cat}|1151005|900000000#1"])
        meta = json.load(open(os.path.join(self.d.dir, "meta", "115.json"), encoding="utf-8"))
        meta["history"] = [meta["history"][-1]]
        self.d.write("meta/115.json", meta)
        self.d.diff("115", "zzzzzzzz", "f" * 64, added=[f"3003|{self.cat}|1151001|1#1"])
        t = self.text()
        self.assertIn("新增 1、消失 0", t)
        self.assertNotIn("3003", t)

    def test_diff_section_is_capped_at_30_items(self):
        added = [f"{2000 + i}|{self.cat}|1151005|1#1" for i in range(35)]
        self.make(days_ago(1), added=added)
        sec = self.text().split("🔄 較上次資料版本的變動（115年檔")[1]
        self.assertEqual(sum(1 for ln in sec.splitlines() if ln.startswith("  • 新增")), 30)
        self.assertIn("另有 5 筆未列出", sec)
        self.assertIn("新增 35、", sec)


# ─────────────────────────────────────────
# fail-closed
# ─────────────────────────────────────────
class TestFailClosed(Base):
    MARK = "申報進度資料缺失/過舊"

    def assert_flagged(self, t, state, needle):
        self.assertIn(self.MARK, t)
        self.assertIn(f"目前狀態「{state}」", t)
        self.assertIn(needle, t)
        self.assertTrue(t.rstrip().endswith(fp.FOOTER))

    def test_no_data_directory_at_all(self):
        shutil.rmtree(self.d.dir)
        rep = self.report()
        self.assertEqual(rep.state, "缺失")
        self.assert_flagged(rep.text, "缺失", "meta/115.json 檔案不存在")
        self.assertIn("沒有可用的資料版本", rep.text)
        self.assertNotIn("審理中", rep.text.replace("沒有可用", ""))

    def test_missing_meta_missing_parsed_corrupt_json_wrong_structure_and_sha_mismatch(self):
        cases = []
        d = self.d
        d.good = None
        d.year("115", [prow()]); d.year("114", [])
        os.remove(os.path.join(d.dir, "parsed", "115.json")); cases.append(("parsed/115.json 檔案不存在",))
        self.assert_flagged(self.text(), "缺失", cases[-1][0])
        d.year("115", [prow()])
        os.remove(os.path.join(d.dir, "meta", "115.json"))
        self.assert_flagged(self.text(), "缺失", "meta/115.json 檔案不存在")
        d.year("115", [prow()])
        d.write("parsed/115.json", None, raw="{broken")
        self.assert_flagged(self.text(), "缺失", "parsed/115.json 無法讀取或不是有效的 JSON")
        d.year("115", [prow()])
        d.write("meta/115.json", None, raw="not json")
        self.assert_flagged(self.text(), "缺失", "meta/115.json 無法讀取")
        d.year("115", [prow()])
        d.write("parsed/115.json", {"sha256": "x", "rows": "nope"})
        self.assert_flagged(self.text(), "缺失", "parsed/115.json 結構不符")
        d.year("115", [prow()])
        d.write("meta/115.json", {"sha256": "x"})
        self.assert_flagged(self.text(), "缺失", "meta/115.json 結構不符")
        d.year("115", [prow()])
        d.write("meta/115.json", {"sha256": "x", "first_seen": "not-a-date"})
        self.assert_flagged(self.text(), "缺失", "首次看到日期無法解析")
        d.year("115", [prow()], parsed_sha="b" * 64)
        self.assert_flagged(self.text(), "缺失", "版本（sha256）不一致")

    def test_one_year_broken_still_shows_the_other_year_but_says_so(self):
        self.d.year("115", [prow("1001")])
        self.d.year("114", [prow("2001")], parsed_sha="b" * 64)
        rep = self.report()
        self.assertEqual(rep.state, "缺失")
        self.assertIn("114年檔：parsed/114.json 與 meta/114.json 的版本（sha256）不一致", rep.text)
        self.assertIn("1001", rep.text)
        self.assertNotIn("2001", rep.text)
        self.assertIn("只列出仍可讀取的版本內容", rep.text)

    def test_without_a_check_record_the_old_7_day_rule_applies_to_the_current_year_file(self):
        for age, state in ((0, "正常"), (7, "正常"), (8, "過舊"), (30, "過舊")):
            self.d.year("115", [prow("1001")], first_seen=days_ago(age))
            self.d.year("114", [])
            rep = self.report()
            self.assertEqual(rep.state, state, age)
            if state == "過舊":
                self.assert_flagged(rep.text, "過舊", f"尚無檢查紀錄，暫以此版本首次看到日判斷：{age} 天前")
                self.assertIn("1001", rep.text)                        # 仍列出，但已加上明確警示，不假裝最新
                self.assertIn("不要當成最新狀態", rep.text)
            else:
                self.assertNotIn(self.MARK, rep.text)

    def test_previous_year_file_being_old_is_not_a_stale_alarm(self):
        self.d.year("115", [prow("1001")], first_seen=days_ago(1))
        self.d.year("114", [prow("2001")], first_seen=days_ago(200))
        self.assertEqual(self.report().state, "正常")

    def test_first_seen_in_the_future_is_flagged(self):
        self.d.year("115", [prow("1001")], first_seen=days_ago(-2))
        self.d.year("114", [])
        rep = self.report()
        self.assertEqual(rep.state, "缺失")
        self.assertIn("晚於今日", rep.text)

    def test_row_level_anomalies_never_fail_the_report(self):
        rows = [prow("1001"), "not-a-dict", {"fields": "x"}, {"fields": {"證券代號": "1"}}, prow("", closing=""), prow("1004", closing="生效", eff=""),
                {"fields": {"證券代號": "1", "案件類別": "轉換公司債(無擔保)", "結案類型": "", "收文日期": "1151005"}, "row": "weird", "flags": "oops", "dup_seq": "x"}, prow("1005")]
        self.good(rows)
        rep = self.report()
        self.assertEqual(rep.state, "正常")
        self.assertEqual(rep.counts["anomalies"], 5)
        self.assertEqual(rep.counts["reviewing"], 3)               # 1001、1005 與最後那列（flags/dup_seq 型別怪但資料可用）
        self.assertIn("⚠ 資料異常 5 列", rep.text)
        self.assertEqual(rep.text.count("資料異常："), 5)
        self.assertIn("1001", rep.text)
        self.assertIn("1005", rep.text)

    def test_unexpected_exception_on_one_row_only_marks_that_row(self):
        self.good([prow("1001"), prow("1002"), prow("1003")])
        real = fp.classify_row
        calls = {"n": 0}

        def flaky(year, row, today):
            calls["n"] += 1
            if row["fields"]["證券代號"] == "1002" and year == "115":
                raise ValueError("boom")
            return real(year, row, today)
        with mock.patch.object(fp, "classify_row", flaky):
            rep = self.report()
        self.assertEqual((rep.state, rep.counts["anomalies"], rep.counts["reviewing"]), ("正常", 1, 2))
        self.assertIn("解析例外 ValueError", rep.text)

    def test_anything_unexpected_becomes_an_in_block_notice(self):
        self.good([prow()])
        with mock.patch.object(fp, "build_report", side_effect=RuntimeError("boom")):
            t = fp.build_progress_block(NOW, self.d.dir)
            line = fp.status_line(NOW, self.d.dir)
        self.assertIn(self.MARK, t)
        self.assertIn("RuntimeError", t)
        self.assertTrue(t.startswith(fp.TITLE) and t.endswith(fp.FOOTER))
        self.assertTrue(line.startswith("缺失"))

    def test_more_than_30_rows_are_never_silently_truncated(self):
        n = 35
        rows = [prow(f"{1000 + i}", receipt=days_ago(i + 1)) for i in range(n)]
        rows += [prow(f"{3000 + i}", closing="生效", eff=days_ago(i % 10), receipt="2026-09-01") for i in range(31)]
        rows += [prow(f"{5000 + i}", closing="退件", reject=days_ago(i % 10)) for i in range(32)]
        rows += [prow(f"{7000 + i}", closing="駁回") for i in range(33)]
        self.good(rows)
        t = self.text()
        rev = t.split("⏳ 審理中")[1].split("✅ 近期已生效")[0]
        self.assertIn(f"審理中 {n} 筆", t)
        self.assertEqual(sum(1 for ln in rev.splitlines() if ln.startswith("  • ")), 30)
        self.assertIn("…另有 5 筆未列出", rev)
        eff = t.split("✅ 近期已生效")[1].split("🛑 近期終止")[0]
        self.assertEqual(sum(1 for ln in eff.splitlines() if ln.startswith("  • ")), 30)
        self.assertIn("…另有 1 筆未列出", eff)
        term = t.split("🛑 近期終止")[1].split("⚠ 資料異常")[0]
        self.assertIn("…另有 2 筆未列出", term)
        self.assertIn("⚠ 資料異常 33 列", t)
        self.assertEqual(t.count("  • 資料異常："), 30)
        self.assertIn("…另有 3 列未列出", t)

    def test_exactly_30_rows_have_no_overflow_note(self):
        self.good([prow(f"{1000 + i}", receipt=days_ago(i + 1)) for i in range(30)])
        self.assertNotIn("另有", self.text())


# ─────────────────────────────────────────
# 依「最後成功檢查日」判斷過舊
# ─────────────────────────────────────────
class TestFreshnessByLastCheck(Base):
    STALE = "申報進度資料過舊(最後成功檢查 {d}),抓取可能失敗"

    def setup_years(self, first_seen="2026-10-12", check=None, **kw):
        self.d.year("115", [prow("1001")], first_seen=first_seen)
        self.d.year("114", [prow("2001", receipt="2025-12-01")], first_seen=kw.get("first_seen114", first_seen))
        if check is not None:
            self.d.check(check)

    def test_fresh_check_is_normal_and_shown_in_the_version_line(self):
        self.setup_years(check={"115": "2026-10-12", "114": "2026-10-12"})
        rep = self.report()
        self.assertEqual((rep.state, rep.label, rep.notes), ("正常", "正常", []))
        self.assertIn("最近成功檢查 2026-10-12", rep.text)
        self.assertNotIn("申報進度資料缺失/過舊", rep.text)

    def test_three_day_boundary_day_3_is_ok_day_4_is_stale(self):
        for gap, state in ((0, "正常"), (1, "正常"), (3, "正常"), (4, "過舊"), (20, "過舊")):
            self.setup_years(check={"115": days_ago(gap), "114": "2026-10-12"})
            rep = self.report()
            self.assertEqual(rep.state, state, gap)
            if state == "過舊":
                self.assertIn(self.STALE.format(d=days_ago(gap)), rep.text)
                self.assertIn("以下只列出仍可讀取的版本內容", rep.text)
                self.assertIn("1001", rep.text)                                   # 仍列出，但已有明確警示
            else:
                self.assertNotIn("過舊", rep.text)

    def test_each_year_is_judged_by_its_own_check_date_including_the_old_year_file(self):
        self.setup_years(first_seen="2026-10-12", check={"115": "2026-10-12", "114": days_ago(4)})
        rep = self.report()
        self.assertEqual(rep.state, "過舊")
        self.assertEqual(rep.reasons, ["114年檔：" + self.STALE.format(d=days_ago(4))])
        self.assertIn("1001", rep.text)
        self.assertIn("2001", rep.text)

    def test_source_not_updated_for_days_but_check_ok_is_not_stale(self):
        self.setup_years(first_seen=days_ago(40), check={"115": "2026-10-12", "114": "2026-10-12"})
        rep = self.report()
        self.assertEqual(rep.state, "正常")
        self.assertEqual(rep.label, "來源 40 天未更新")
        self.assertEqual(len(rep.versions), 2)                                  # 註記行不算版本行
        self.assertIn("來源 40 天未更新(最近檢查 2026-10-12,正常)", rep.text)
        self.assertNotIn("申報進度資料缺失/過舊", rep.text)
        self.assertTrue(fp.status_line(NOW, self.d.dir).startswith("來源 40 天未更新｜"))

    def test_source_age_note_starts_at_two_days(self):
        for age, shown in ((0, False), (1, False), (2, True), (3, True)):
            self.setup_years(first_seen=days_ago(age), check={"115": "2026-10-12", "114": "2026-10-12"})
            rep = self.report()
            self.assertEqual("天未更新(最近檢查" in rep.text, shown, age)

    def test_old_year_file_note_does_not_change_the_overall_label(self):
        self.d.year("115", [prow("1001")], first_seen="2026-10-12")
        self.d.year("114", [], first_seen=days_ago(200))
        self.d.check({"115": "2026-10-12", "114": "2026-10-12"})
        rep = self.report()
        self.assertEqual(rep.label, "正常")
        self.assertIn("114年檔 v2", rep.text)
        self.assertIn("來源 200 天未更新(最近檢查 2026-10-12,正常)", rep.text)

    def test_taipei_midnight_changes_the_gap(self):
        self.setup_years(check={"115": "2026-10-09", "114": "2026-10-09"})
        before = datetime(2026, 10, 12, 15, 59, tzinfo=timezone.utc)      # 台北 10-12 23:59 → 差 3 天
        after = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)        # 台北 10-13 00:00 → 差 4 天
        self.assertEqual(self.report(before).state, "正常")
        self.assertEqual(self.report(after).state, "過舊")
        self.assertIn(self.STALE.format(d="2026-10-09"), self.text(after))

    def test_process_timezone_does_not_matter(self):
        self.setup_years(check={"115": "2026-10-09", "114": "2026-10-09"})
        now = datetime(2026, 10, 12, 16, 30, tzinfo=timezone.utc)
        old = os.environ.get("TZ")
        try:
            states = set()
            for tz in ("UTC", "America/Los_Angeles", "Pacific/Kiritimati"):
                os.environ["TZ"] = tz
                time.tzset()
                states.add(self.report(now).state)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.assertEqual(states, {"過舊"})

    def test_missing_record_falls_back_to_old_rule_and_says_so(self):
        for age, state in ((7, "正常"), (8, "過舊")):
            self.setup_years(first_seen=days_ago(age))
            rep = self.report()
            self.assertEqual(rep.state, state, age)
            self.assertIn("尚無檢查紀錄", rep.text)
            self.assertIn("尚無檢查紀錄", rep.label)
            self.assertIn("沿用舊規則", rep.label)
            self.assertNotEqual(rep.label, "正常")                            # 不得靜默視為正常
        self.assertIn("尚無檢查紀錄，暫以此版本首次看到日判斷：8 天前", self.text())

    def test_corrupt_record_is_reported_as_abnormal_and_old_rule_applies(self):
        bads = ["{broken", "[]", json.dumps({"years": {}}), json.dumps({"version": 2, "years": {}}), json.dumps({"version": 1, "years": []}),
                json.dumps({"version": 1, "years": {"115": "2026-13-40"}}), json.dumps({"version": 1, "years": {"x": "2026-10-12"}}),
                json.dumps({"version": 1, "years": {"115": "2026-10-12"}, "extra": 1}), json.dumps({"version": 1, "years": {"115": 5}})]
        for bad in bads:
            self.setup_years(first_seen=days_ago(1))
            self.d.check(None, raw=bad)
            rep = self.report()
            self.assertEqual(rep.state, "正常", bad)
            self.assertIn("檢查紀錄異常", rep.text, bad)
            self.assertIn("檢查紀錄異常", rep.label, bad)
            self.assertNotIn("尚無檢查紀錄", rep.text)
            self.setup_years(first_seen=days_ago(9))
            self.assertEqual(self.report().state, "過舊", bad)

    def test_year_missing_from_the_record_uses_fallback_for_that_year_only(self):
        self.setup_years(check={"115": "2026-10-12"})
        rep = self.report()
        self.assertIn("114年檔：尚無檢查紀錄", rep.text)
        self.assertNotIn("115年檔：尚無檢查紀錄", rep.text)
        self.assertEqual(rep.state, "正常")

    def test_a_check_date_in_the_future_is_abnormal(self):
        self.setup_years(check={"115": days_ago(-1), "114": "2026-10-12"})
        rep = self.report()
        self.assertIn("檢查紀錄異常", rep.text)
        self.assertIn("晚於今日", rep.text)

    def test_with_a_good_record_the_old_first_seen_rule_is_not_used(self):
        self.setup_years(first_seen=days_ago(60), check={"115": "2026-10-12", "114": "2026-10-12"})
        self.assertEqual(self.report().state, "正常")

    def test_missing_data_still_beats_everything(self):
        self.setup_years(check={"115": "2026-10-12", "114": "2026-10-12"})
        os.remove(os.path.join(self.d.dir, "parsed", "115.json"))
        rep = self.report()
        self.assertEqual((rep.state, rep.label), ("缺失", "缺失"))


# ─────────────────────────────────────────
# 完全相同的重複列、範圍行
# ─────────────────────────────────────────
class TestIdenticalRows(Base):
    NOTE = "（來源表中完全相同的列 ×{n}）"

    def test_identical_rows_are_all_listed_each_with_the_note(self):
        rows = [prow("1001", seq=i + 1, rownum=10 + i) for i in range(3)] + [prow("1002")]
        self.good(rows)
        rep = self.report()
        rev = rep.text.split("⏳ 審理中")[1].split("✅ 近期已生效")[0]
        self.assertEqual(rev.count("  • 1001"), 3)                                  # 不合併
        self.assertEqual(rev.count(self.NOTE.format(n=3)), 3)
        lines = rev.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("  • 1001"):
                self.assertTrue(lines[i + 1].endswith(self.NOTE.format(n=3)))      # 註記在該筆的最後一行
        one = [i for i, ln in enumerate(lines) if ln.startswith("  • 1002")][0]
        self.assertNotIn("完全相同", lines[one] + lines[one + 1])
        self.assertEqual(rep.counts["reviewing"], 4)
        self.assertEqual(rep.counts["identical_rows"], 3)

    def test_any_differing_field_means_no_note(self):
        a, b = prow("1001", seq=1), prow("1001", seq=2, rownum=11)
        b["fields"]["承銷商"] = "另一家券商"
        self.good([a, b, prow("1001", amount="700000000", seq=3, rownum=12)])
        self.assertNotIn("完全相同", self.text())

    def test_note_counts_are_per_year_file(self):
        self.good([prow("1001")], [prow("1001")])
        self.assertNotIn("完全相同", self.text())
        self.good([prow("1001", seq=1), prow("1001", seq=2, rownum=11)], [prow("1001")])
        self.assertEqual(self.text().count(self.NOTE.format(n=2)), 2)

    def test_note_also_in_effective_and_terminated_sections(self):
        rows = [prow("1001", closing="生效", eff=days_ago(1), seq=1), prow("1001", closing="生效", eff=days_ago(1), seq=2, rownum=11),
                prow("1002", closing="退件", reject=days_ago(1), seq=1), prow("1002", closing="退件", reject=days_ago(1), seq=2, rownum=11)]
        self.good(rows)
        t = self.text()
        eff = t.split("✅ 近期已生效")[1].split("🛑 近期終止")[0]
        term = t.split("🛑 近期終止")[1]
        self.assertEqual(eff.count(self.NOTE.format(n=2)), 2)
        self.assertEqual(term.count(self.NOTE.format(n=2)), 2)

    def test_diff_block_annotates_identical_rows_and_counts_match_what_is_listed(self):
        cat = "轉換公司債(無擔保)"
        rows = [prow("1001", closing="生效", eff=days_ago(1), seq=1, receipt="2026-10-01"), prow("1001", closing="生效", eff=days_ago(1), seq=2, rownum=11, receipt="2026-10-01"),
                prow("1002", closing="生效", eff=days_ago(1), receipt="2026-10-02"), prow("1003", closing="生效", eff=days_ago(1), receipt="2026-10-03", seq=1)]
        sha = self.d.year("115", rows, first_seen=days_ago(1), history=[{"sha8": "oldoldol"}, {"sha8": "11511511"}])
        self.d.year("114", [])
        self.d.check({"115": "2026-10-12", "114": "2026-10-12"})
        self.d.diff("115", "oldoldol", sha,
                    added=[f"1001|{cat}|1151001|500000000#1", f"1001|{cat}|1151001|500000000#2"],
                    vanished=[f"9001|{cat}|1150901|1#1"],
                    changed=[{"id": f"1002|{cat}|1151002|500000000#1", "field": "結案類型", "old": "", "new": "生效"},
                             {"id": f"1001|{cat}|1151001|500000000#1", "field": "結案類型", "old": "", "new": "生效"},
                             {"id": f"1001|{cat}|1151001|500000000#2", "field": "結案類型", "old": "", "new": "生效"}])
        rep = self.report()
        sec = rep.text.split("🔄 較上次資料版本的變動")[1].split("（以上為申報進度")[0]
        d = rep.counts["diff"]["115"]
        items = [ln for ln in sec.splitlines() if ln.startswith("  • ")]
        self.assertEqual((d["added"], d["vanished"], d["changed"]), (2, 1, 3))
        self.assertEqual(len(items), d["added"] + d["vanished"] + d["changed"])           # 計數 = 顯示的行數
        self.assertIn("新增 2、消失 1、欄位變動 3（其中 4 筆屬於來源表中完全相同的重複列，各自獨立計入）", sec)
        dup_items = [ln for ln in items if self.NOTE.format(n=2) in ln]
        self.assertEqual(len(dup_items), 4)
        self.assertEqual(sum(1 for ln in items if ln.startswith("  • 消失") and "完全相同" in ln), 0)
        self.assertEqual(sum(1 for ln in items if "1002" in ln and "完全相同" in ln), 0)


class TestScopeLine(Base):
    def test_scope_line_states_the_cumulative_years(self):
        self.good([prow("1001")], [prow("2001", closing="生效", eff=days_ago(1))])
        self.assertIn("📊 範圍內 2 列（115＋114年累計；", self.text())

    def test_scope_line_when_only_one_year_is_readable(self):
        self.good([prow("1001")], [])
        os.remove(os.path.join(self.d.dir, "parsed", "114.json"))
        t = self.text()
        self.assertIn("📊 範圍內 1 列（115年累計；", t)
        self.assertNotIn("115＋114", t)

# ─────────────────────────────────────────
# 版本行、/status
# ─────────────────────────────────────────
class TestVersionAndStatus(Base):
    def test_version_line_shows_the_version_actually_used(self):
        self.d.year("115", [prow()], first_seen="2026-10-10", version="3", page_update_text="2026-10-09", filename_decoded="1151009申報v3.xlsx")
        self.d.year("114", [])
        self.d.check({"115": "2026-10-12", "114": "2026-10-12"})
        t = self.text()
        self.assertIn("115年檔 v3｜來源更新 2026-10-09｜此版本首次看到 2026-10-10（2 天前）｜最近成功檢查 2026-10-12｜sha 11511511｜1151009申報v3.xlsx", t)
        self.assertIn("本報告實際使用", t)

    def test_status_line_four_states(self):
        self.good([prow()])
        self.assertTrue(fp.status_line(NOW, self.d.dir).startswith("正常｜115年檔 v2"))
        self.good([prow()], first_seen=days_ago(5))                                  # 檢查正常、來源 5 天沒有新版本
        s = fp.status_line(NOW, self.d.dir)
        self.assertTrue(s.startswith("來源 5 天未更新｜115年檔 v2"), s)
        self.d.check({"115": days_ago(4), "114": "2026-10-12"})                       # 檢查日太舊
        s = fp.status_line(NOW, self.d.dir)
        self.assertTrue(s.startswith("過舊｜") and "最後成功檢查 2026-10-08" in s, s)
        os.remove(os.path.join(self.d.dir, "meta", "115.json"))
        s = fp.status_line(NOW, self.d.dir)
        self.assertTrue(s.startswith("缺失｜") and "meta/115.json" in s)

    def test_deterministic(self):
        self.good([prow(), prow("1002", closing="生效", eff=days_ago(2))])
        self.assertEqual(self.text(), self.text())


# ─────────────────────────────────────────
# 接進報告／日報／/cb／/all／/status
# ─────────────────────────────────────────
CB_ROW = {"text": "#2317 #鴻海 發佈時間：20261011 16:59:13 關於可轉換公司債發行", "date": "2026-10-11 17:00:00", "from": "x"}
OTHER_ROW = {"text": "#2801 #彰銀 發佈時間：20261011 16:17:31 公告授信資產轉讓", "date": "2026-10-11 17:00:00", "from": "x"}


class TestIntegration(Base):
    def render(self, rows):
        with mock.patch.dict(os.environ, {"FSC_DATA_DIR": self.d.dir}):
            return cf.CompleteFormatter().format_with_convertible_bond_priority(rows)

    def setUp(self):
        super().setUp()
        self.good([prow("1001"), prow("1002", closing="生效", eff=days_ago(1))])

    def test_block_sits_inside_the_cb_region_before_other_announcements(self):
        t = self.render([CB_ROW, OTHER_ROW])
        i_cb, i_blk, i_other = t.index("🔥 轉換公司債相關公告（優先顯示）"), t.index(fp.TITLE), t.index("📋 其他公告\n")
        self.assertLess(i_cb, t.index("【轉換公司債 - 第 1 則】"))
        self.assertLess(t.index("【轉換公司債 - 第 1 則】"), i_blk)
        self.assertLess(i_blk, i_other)
        self.assertEqual(t.count(fp.TITLE), 1)

    def test_block_present_even_when_there_are_no_cb_announcements(self):
        t = self.render([OTHER_ROW])
        self.assertIn("(無轉換公司債消息)", t)
        self.assertIn(fp.TITLE, t)
        self.assertLess(t.index("(無轉換公司債消息)"), t.index(fp.TITLE))
        self.assertLess(t.index(fp.TITLE), t.index("📋 其他公告\n"))

    def test_existing_layout_is_unchanged_apart_from_the_inserted_block(self):
        with mock.patch.object(cf, "build_progress_block", return_value=""):
            base = self.render([CB_ROW, OTHER_ROW])
        full = self.render([CB_ROW, OTHER_ROW])
        block = fp.extract_progress_block(full)
        self.assertTrue(block)
        nonblank = lambda s: [ln for ln in s.splitlines() if ln.strip() and "生成時間" not in ln]
        self.assertEqual(nonblank(full.replace(block, "", 1)), nonblank(base))          # 只多了區塊，既有各行一行不差、順序不變

    def test_block_text_never_contains_the_report_section_markers(self):
        for builder in (lambda: None, lambda: shutil.rmtree(self.d.dir)):
            builder()
            t = fp.build_progress_block(NOW, self.d.dir)
            for marker in ("轉換公司債相關公告", "【轉換公司債", "無轉換公司債", "📢 澄清媒體報導", "💰 財務資訊", "👥 人事異動",
                           "⚠️ 注意交易", "📋 重大訊息", "📄 其他", "── 其他公告", "📋 其他公告"):
                self.assertNotIn(marker, t, marker)

    def test_main_filter_keeps_the_block_and_the_announcement_count(self):
        report = self.render([CB_ROW, OTHER_ROW])
        for flt in (main_mod.filter_cb_only, bl.filter_cb_only):
            out = flt(report)
            self.assertIn("共 1 則", out)
            self.assertEqual(out.count(fp.TITLE), 1)
            self.assertIn(fp.FOOTER, out)
            self.assertNotIn("彰銀", out)
            self.assertFalse(main_mod._is_empty_result(out))

    def test_filters_append_the_block_on_days_without_cb_announcements(self):
        report = self.render([OTHER_ROW])
        for flt in (main_mod.filter_cb_only, bl.filter_cb_only):
            out = flt(report)
            self.assertIn("今日無轉換公司債相關公告。", out)
            self.assertIn(fp.TITLE, out)
            self.assertTrue(out.rstrip().endswith(fp.FOOTER))
            self.assertNotIn("彰銀", out)
        self.assertTrue(main_mod._is_empty_result(main_mod.filter_cb_only(report)))            # 日報的「無公告重抓」判斷維持不變

    def test_filters_unchanged_when_report_has_no_block(self):
        old_style = "📊 x\n(無轉換公司債消息)\n📋 其他公告\n"
        self.assertTrue(main_mod.filter_cb_only(old_style).endswith("今日無轉換公司債相關公告。"))
        self.assertTrue(bl.filter_cb_only(old_style).endswith("今日無轉換公司債相關公告。"))

    def test_broken_data_still_reaches_cb_and_all_with_an_explicit_warning(self):
        shutil.rmtree(self.d.dir)
        report = self.render([OTHER_ROW])
        self.assertIn("申報進度資料缺失/過舊", main_mod.filter_cb_only(report))
        self.assertIn("申報進度資料缺失/過舊", report)

    def test_extract_without_footer_stops_before_the_other_announcements(self):
        t = fp.TITLE + "\n" + "=" * 60 + "\nbody\n\n📋 其他公告\n====\nx"
        self.assertEqual(fp.extract_progress_block(t), fp.TITLE + "\n" + "=" * 60 + "\nbody")
        self.assertEqual(fp.extract_progress_block("沒有區塊"), "")
        self.assertEqual(fp.extract_progress_block(""), "")


class _StatusHarness(unittest.TestCase):
    pass


try:
    from tests.test_bot_listener import MainHarness, upd as _upd  # type: ignore
except Exception:  # pragma: no cover
    from test_bot_listener import MainHarness, upd as _upd  # type: ignore


class TestStatusCommand(MainHarness, Base):
    def setUp(self):
        Base.setUp(self)

    def test_status_reply_contains_one_progress_line_with_state_and_version(self):
        self.good([prow()])
        with mock.patch.dict(os.environ, {"FSC_DATA_DIR": self.d.dir}):
            code, out, handled, sent, _ = self.run_main([[], [_upd(1, "/status", 0)]], env_extra={"BOT_RELAY_ENABLED": "0"})
        texts = [t for _, t in sent if "Bot 狀態" in t]
        self.assertEqual(len(texts), 1)
        lines = [ln for ln in texts[0].splitlines() if ln.startswith("📑 申報進度資料：")]
        self.assertEqual(len(lines), 1)
        self.assertIn("115年檔 v2", lines[0])
        self.assertTrue(lines[0].split("：", 1)[1].startswith(("正常", "過舊")))

    def test_status_reply_says_missing_instead_of_crashing(self):
        shutil.rmtree(self.d.dir)
        with mock.patch.dict(os.environ, {"FSC_DATA_DIR": self.d.dir}):
            code, out, handled, sent, _ = self.run_main([[], [_upd(1, "/status", 0)]], env_extra={"BOT_RELAY_ENABLED": "0"})
        self.assertEqual(code, 0)
        self.assertTrue(any("📑 申報進度資料：缺失｜" in t for _, t in sent))


# ─────────────────────────────────────────
# 與留存程式的一致性、真實資料冒煙、無網路
# ─────────────────────────────────────────
class TestConsistency(unittest.TestCase):
    def test_constants_match_the_archive(self):
        self.assertEqual(fp.FSC_YEARS, tuple(y for y, _ in af.YEARS))
        self.assertEqual(fp.EXPECTED_COLUMNS, fs.EXPECTED_COLUMNS)
        self.assertEqual(af.F_AMOUNT_CHANGE, "同日同類別多筆")
        self.assertTrue(set(fp.DIFF_FIELDS) <= set(af.DIFF_FIELDS) | {"收文日期"})
        self.assertEqual(set(fp.TERMINATION_COLUMNS.values()) | {"生效日期"}, {"自行撤回日期", "退件日期", "廢止/撤銷日期", "生效日期"})

    def test_module_has_no_network_imports(self):
        src = open(os.path.join(ROOT, "fsc_progress.py"), encoding="utf-8").read()
        for bad in ("import requests", "urllib", "http.client", "socket", "subprocess", "import telegram", "telethon"):
            self.assertNotIn(bad, src, bad)

    def test_real_repo_data_smoke(self):
        # 不假設內容（bot 每天會更新 data/fsc）：只確認不丟例外、結構完整、狀態是合法值
        rep = fp.build_report(NOW)
        self.assertIn(rep.state, ("正常", "過舊", "缺失"))
        self.assertTrue(rep.text.startswith(fp.TITLE) and rep.text.rstrip().endswith(fp.FOOTER))
        self.assertEqual(fp.extract_progress_block("前\n" + rep.text + "\n後"), rep.text.strip())


if __name__ == "__main__":
    unittest.main()
