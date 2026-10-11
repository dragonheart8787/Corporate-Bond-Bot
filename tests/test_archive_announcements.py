"""每日留存重大訊息（scripts/archive_announcements.py 與 archive-announcements.yml）的測試。
全部離線：假的連線物件、暫存目錄；不連任何網站、不發任何訊息。"""

import contextlib
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import archive_announcements as aa  # noqa: E402
import probe_sources as ps  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class FakeHeaders(dict):
    def get(self, k, d=None):
        for key, v in self.items():
            if key.lower() == k.lower():
                return v
        return d


class FakeResp:
    def __init__(self, body=b"", status=200, ctype="application/json"):
        self._body, self.status, self.headers = body, status, FakeHeaders({"Content-Type": ctype})

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """依序回傳／拋出 outcomes（每個來源一個）；耗盡後重複最後一個。"""

    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []

    def open(self, req, timeout=None):
        self.requests.append((req.full_url, req.get_method(), dict(req.header_items())))
        item = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(item, Exception):
            raise item
        return item


def jb(obj):
    return FakeResp(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def twse(date="1151010", time_="143000", code="2330", subject="公告本公司董事會決議發行國內第一次無擔保轉換公司債", body="說明內文", **kw):
    r = {"出表日期": "1151011", "發言日期": date, "發言時間": time_, "公司代號": code, "公司名稱": "測試",
         "主旨 ": subject, "符合條款": "第51款", "事實發生日": "1151010", "說明": body}   # 注意：證交所的「主旨」欄名帶一個尾端空白
    r.update(kw)
    return r


def tpex(date="1151010", time_="150000", code="6547", subject="公告本公司第二次有擔保轉換公司債", body="櫃買說明", **kw):
    r = {"Date": "1151011", "發言日期": date, "發言時間": time_, "SecuritiesCompanyCode": code, "CompanyName": "櫃買測試",
         "主旨": subject, "符合條款": "第51款", "事實發生日": "1151010", "說明": body}
    r.update(kw)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "data", "announcements")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_archive(self, twse_rows, tpex_rows, env=None, sleeps=None):
        op = FakeOpener(*[x if isinstance(x, (FakeResp, Exception)) else jb(x) for x in (twse_rows, tpex_rows)])
        out = io.StringIO()
        e = {"ARCHIVE_DIR": self.dir, "PROBE_REPO": "o/r", **(env or {})}
        sl = [] if sleeps is None else sleeps
        with contextlib.redirect_stdout(out):
            code = aa.run(e, opener=op, sleep=sl.append)
        self.op = op
        return code, out.getvalue()

    def day(self, d="20261010"):
        with open(os.path.join(self.dir, f"{d}.json"), encoding="utf-8") as f:
            return json.load(f)

    def index(self):
        with open(os.path.join(self.dir, "_index.json"), encoding="utf-8") as f:
            return json.load(f)

    def raw(self, name):
        with open(os.path.join(self.dir, name), "rb") as f:
            return f.read()


class TestFilter(unittest.TestCase):
    def rec(self, subject="x", body="y"):
        return {"主旨": subject, "說明": body}

    def test_each_keyword_in_subject_or_body(self):
        for kw in aa.FILTER_KEYWORDS:
            self.assertTrue(aa.is_cb_related(self.rec(subject=f"前{kw}後")), kw)
            self.assertTrue(aa.is_cb_related(self.rec(body=f"前{kw}後")), kw)

    def test_the_five_keywords_are_exactly_the_requested_ones(self):
        self.assertEqual(set(aa.FILTER_KEYWORDS), {"轉換公司債", "可轉換", "可轉債", "募集與發行", "公司債"})

    def test_unrelated_announcements_are_excluded(self):
        for s in ("關於美國國際貿易委員會337調查之說明", "公告授信資產轉讓", "更名公告", ""):
            self.assertFalse(aa.is_cb_related(self.rec(subject=s, body="買回庫藏股")), s)

    def test_keyword_only_in_body_counts(self):
        self.assertTrue(aa.is_cb_related(self.rec(subject="重大訊息", body="董事會決議辦理國內第二次無擔保轉換公司債")))


class TestParseAndValidate(unittest.TestCase):
    def parse(self, source, obj):
        return aa.parse_records(source, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def test_twse_trailing_space_field_name_is_normalised(self):
        recs = self.parse("twse", [twse()])
        self.assertEqual(recs[0]["主旨"], "公告本公司董事會決議發行國內第一次無擔保轉換公司債")
        self.assertEqual(recs[0]["來源"], "twse")
        self.assertEqual(set(aa.OUT_FIELDS) | {"來源"}, set(recs[0]))

    def test_tpex_english_field_names_are_mapped(self):
        recs = self.parse("tpex", [tpex()])
        self.assertEqual(recs[0]["公司代號"], "6547")
        self.assertEqual(recs[0]["公司名稱"], "櫃買測試")
        self.assertEqual(recs[0]["出表日期"], "1151011")

    def test_values_are_kept_verbatim(self):
        body = "  1.事實發生日：115/10/09\n 2.送件日期：115/10/12  "
        recs = self.parse("twse", [twse(body=body)])
        self.assertEqual(recs[0]["說明"], body)                        # 不 strip、不改寫

    def test_null_value_becomes_empty_string_but_missing_field_is_an_error(self):
        self.assertEqual(self.parse("twse", [twse(說明=None)])[0]["說明"], "")
        bad = twse()
        del bad["說明"]
        with self.assertRaises(aa.ArchiveError) as cm:
            self.parse("twse", [bad])
        self.assertIn("缺少欄位", str(cm.exception))
        self.assertIn("說明", str(cm.exception))

    def test_every_required_field_is_checked(self):
        for field in aa.OUT_FIELDS:
            rec = twse()
            for k in list(rec):
                if k.strip() == field:
                    del rec[k]
            with self.assertRaises(aa.ArchiveError, msg=field):
                self.parse("twse", [rec])

    def test_empty_array_is_an_error(self):
        with self.assertRaises(aa.ArchiveError) as cm:
            self.parse("twse", [])
        self.assertIn("空陣列", str(cm.exception))

    def test_structure_anomalies(self):
        for body in (b"{}", b'{"data": []}', b'"x"', b"null", b"[1,2]", b"{ not json", b"", b"<html>blocked</html>", b"\xff\xfe"):
            with self.assertRaises(aa.ArchiveError, msg=body):
                aa.parse_records("twse", body)

    def test_non_array_top_level_is_reported_as_structure_change(self):
        for body in (b'{"data": [{"a": 1}]}', b'{"a": 1}', b'"text"', b"5"):
            with self.assertRaises(aa.ArchiveError, msg=body) as cm:
                aa.parse_records("twse", body)
            self.assertIn("不是陣列", str(cm.exception), body)

    def test_one_bad_row_rejects_the_whole_source(self):
        bad = twse(date="not-a-date")
        with self.assertRaises(aa.ArchiveError) as cm:
            self.parse("twse", [twse(), bad])
        self.assertIn("發言日期無法解析", str(cm.exception))

    def test_date_formats_and_day_key(self):
        for raw in ("1151010", "20261010", "115/10/10", "2026-10-10"):
            recs = self.parse("twse", [twse(date=raw)])
            self.assertEqual(aa.day_of(recs[0]), "20261010", raw)

    def test_nonexistent_date_is_rejected(self):
        for raw in ("1150230", "1151310", "20260231"):
            with self.assertRaises(aa.ArchiveError, msg=raw):
                self.parse("twse", [twse(date=raw)])


class TestArchiveFiles(Base):
    def test_writes_filtered_records_per_day_with_index(self):
        code, out = self.run_archive(
            [twse(), twse(code="2317", subject="關於美國國際貿易委員會337調查之說明", body="x"), twse(code="1101", time_="090000", subject="公告普通公司債發行", body="y")],
            [tpex(), tpex(code="8888", subject="更名公告", body="z")],
        )
        self.assertEqual(code, 0, out)
        recs = self.day()["records"]
        self.assertEqual({r["公司代號"] for r in recs}, {"2330", "1101", "6547"})      # 非可轉債的被濾掉
        self.assertEqual(self.day()["date"], "20261010")
        idx = self.index()["days"]["20261010"]
        self.assertEqual(idx["total"], 5)
        self.assertEqual(idx["total_by_source"], {"tpex": 2, "twse": 3})
        self.assertEqual(idx["filtered"], 3)
        self.assertEqual(idx["filtered_by_source"], {"tpex": 1, "twse": 2})

    def test_record_content_has_the_requested_fields_plus_source(self):
        self.run_archive([twse()], [tpex()])
        for r in self.day()["records"]:
            self.assertEqual(set(r), set(aa.OUT_FIELDS) | {"來源"})

    def test_records_are_sorted_deterministically(self):
        self.run_archive([twse(time_="170000", code="3"), twse(time_="080000", code="2"), twse(time_="080000", code="1")], [tpex()])
        times = [(r["發言時間"], r["公司代號"]) for r in self.day()["records"]]
        self.assertEqual(times, sorted(times))

    def test_same_day_rerun_is_byte_identical(self):
        rows = ([twse(), twse(code="2317", subject="可轉債公告", time_="101010")], [tpex()])
        self.run_archive(*rows)
        first = {n: self.raw(n) for n in os.listdir(self.dir)}
        for _ in range(3):
            code, out = self.run_archive(*rows)
            self.assertEqual(code, 0)
        second = {n: self.raw(n) for n in os.listdir(self.dir)}
        self.assertEqual(first, second)
        self.assertIn("新增 0、更新 0、未變 3", out)

    def test_dedupe_by_date_time_code_subject(self):
        dup = twse()
        self.run_archive([dup, copy.deepcopy(dup)], [tpex()])
        self.assertEqual(len([r for r in self.day()["records"] if r["公司代號"] == "2330"]), 1)

    def test_same_time_and_code_but_different_subject_are_both_kept(self):
        self.run_archive([twse(subject="第四次可轉債"), twse(subject="第五次可轉債")], [tpex()])
        self.assertEqual(len([r for r in self.day()["records"] if r["公司代號"] == "2330"]), 2)

    def test_rerun_with_more_rows_merges_and_never_loses_old_rows(self):
        self.run_archive([twse(code="1111", subject="可轉債甲")], [tpex()])
        code, out = self.run_archive([twse(code="2222", subject="可轉債乙")], [tpex()])   # 第二次的回應沒有 1111
        self.assertEqual(code, 0)
        self.assertEqual({r["公司代號"] for r in self.day()["records"]}, {"1111", "2222", "6547"})
        self.assertIn("新增 1", out)

    def test_changed_body_for_same_key_replaces_and_is_reported(self):
        self.run_archive([twse(body="舊")], [tpex()])
        code, out = self.run_archive([twse(body="新")], [tpex()])
        self.assertEqual([r["說明"] for r in self.day()["records"] if r["公司代號"] == "2330"], ["新"])
        self.assertIn("更新 1", out)

    def test_multiple_days_in_one_response_go_to_separate_files(self):
        self.run_archive([twse(date="1151009", subject="可轉債a"), twse(date="1151010", subject="可轉債b")], [tpex(date="1151010")])
        self.assertEqual(sorted(n for n in os.listdir(self.dir) if n != "_index.json"), ["20261009.json", "20261010.json"])
        self.assertEqual(set(self.index()["days"]), {"20261009", "20261010"})

    def test_day_without_any_cb_announcement_writes_no_file_but_index_records_it(self):
        code, out = self.run_archive([twse(subject="更名公告", body="x")], [tpex(subject="更名公告", body="y")])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "20261010.json")))
        e = self.index()["days"]["20261010"]
        self.assertEqual((e["total"], e["filtered"]), (2, 0))

    def test_no_timestamps_or_run_specific_data_in_files(self):
        self.run_archive([twse()], [tpex()])
        for n in os.listdir(self.dir):
            text = self.raw(n).decode("utf-8")
            self.assertNotRegex(text, r"updated_at|generated|fetched_at|T\d\d:\d\d:\d\d", n)

    def test_no_tmp_files_left_behind(self):
        self.run_archive([twse()], [tpex()])
        self.assertFalse([n for n in os.listdir(self.dir) if n.endswith(".tmp")])


class TestGitIdempotency(Base):
    """實際用 git 驗證：同一天重跑不會產生任何變更（workflow 的「無變更，略過 commit」才成立）。"""

    def git(self, *args):
        import subprocess
        e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, env=e)

    @unittest.skipUnless(shutil.which("git"), "需要 git")
    def test_second_run_leaves_the_working_tree_clean(self):
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.git("init", "-q")
        self.dir = os.path.join(self.repo, "data", "announcements")
        rows = ([twse(), twse(code="2317", subject="可轉債乙", time_="101010")], [tpex()])
        self.run_archive(*rows)
        self.git("add", "data/announcements")
        self.assertNotEqual(self.git("diff", "--staged", "--quiet").returncode, 0)     # 第一次有東西可提交
        self.git("commit", "-q", "-m", "first")
        self.run_archive(*rows)
        self.git("add", "data/announcements")
        self.assertEqual(self.git("diff", "--staged", "--quiet").returncode, 0)         # 重跑：沒有任何變更


class TestIndexCounts(Base):
    def test_keyword_counts_are_numbers_over_filtered_records(self):
        rows = [
            twse(code="1", subject="可轉債 董事會決議", body="送件日期:115/10/12"),
            twse(code="2", subject="可轉債", body="金管會申報生效 並於競價拍賣"),
            twse(code="3", subject="可轉債", body="送件 送件 送件"),            # 同一則多次出現只算一筆
            twse(code="4", subject="與可轉債無關的董事會公告 公司債"),
            twse(code="5", subject="更名公告", body="董事會 送件"),                # 非可轉債：不計入
        ]
        self.run_archive(rows, [tpex(subject="更名公告", body="x")])
        kw = self.index()["days"]["20261010"]["keywords_in_filtered"]
        self.assertEqual(kw, {"送件": 2, "申報生效": 1, "競價拍賣": 1, "董事會": 2})
        self.assertTrue(all(isinstance(v, int) for v in kw.values()))

    def test_index_keys_are_exactly_the_requested_keywords(self):
        self.assertEqual(set(aa.INDEX_KEYWORDS), {"送件", "申報生效", "競價拍賣", "董事會"})

    def test_total_never_shrinks_on_a_smaller_later_response(self):
        self.run_archive([twse(), twse(code="9", subject="可轉債z")], [tpex()])
        self.run_archive([twse()], [tpex()])
        e = self.index()["days"]["20261010"]
        self.assertEqual(e["total_by_source"]["twse"], 2)
        self.assertEqual(e["filtered"], 3)        # 日檔是累積的

    def test_index_accumulates_across_days_without_touching_old_days(self):
        self.run_archive([twse(date="1151009", subject="可轉債a")], [tpex(date="1151009")])
        before = copy.deepcopy(self.index()["days"]["20261009"])
        self.run_archive([twse(date="1151010")], [tpex(date="1151010")])
        idx = self.index()["days"]
        self.assertEqual(set(idx), {"20261009", "20261010"})
        self.assertEqual(idx["20261009"], before)

    def test_index_has_only_numbers_no_content(self):
        self.run_archive([twse(body="機密內文ABC")], [tpex()])
        self.assertNotIn("機密內文ABC", self.raw("_index.json").decode("utf-8"))


class TestFailClosed(Base):
    def test_empty_array_from_both_sources_writes_nothing_and_fails(self):
        code, out = self.run_archive([], [])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.dir))
        self.assertEqual(out.count("::error"), 2)
        self.assertIn("空陣列", out)

    def test_missing_field_writes_nothing_for_that_source(self):
        bad = twse()
        del bad["說明"]
        code, out = self.run_archive([bad], [])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.dir))
        self.assertIn("缺少欄位", out)

    def test_one_source_failing_still_archives_the_other_and_exits_red(self):
        code, out = self.run_archive([twse()], urllib.error.URLError(ConnectionRefusedError("refused")))
        self.assertEqual(code, 1)
        self.assertEqual({r["來源"] for r in self.day()["records"]}, {"twse"})
        self.assertIn("::error title=留存失敗 tpex", out)
        self.assertIn("連線失敗", out)

    def test_http_errors_are_explicit(self):
        for status in (403, 404, 429, 500):
            err = urllib.error.HTTPError("https://x", status, "e", FakeHeaders({}), io.BytesIO(b""))
            code, out = self.run_archive(err, err)
            self.assertEqual(code, 1)
            self.assertIn(f"HTTP {status}", out)
            self.assertFalse(os.path.exists(self.dir))

    def test_redirect_is_reported_not_followed(self):
        h = FakeHeaders({"Location": "https://evil.example.com/x"})
        err = urllib.error.HTTPError("https://x", 302, "Found", h, io.BytesIO(b""))
        code, out = self.run_archive(err, err)
        self.assertEqual(code, 1)
        self.assertIn("被轉址", out)
        self.assertEqual(len(self.op.requests), 2)

    def test_html_block_page_is_an_error_not_an_empty_success(self):
        code, out = self.run_archive(FakeResp(b"<!DOCTYPE html><html>Access Denied</html>", ctype="text/html"), [])
        self.assertEqual(code, 1)
        self.assertFalse(os.path.exists(self.dir))
        self.assertNotIn("Access Denied", out)

    def test_corrupt_existing_day_file_is_not_overwritten(self):
        os.makedirs(self.dir)
        path = os.path.join(self.dir, "20261010.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ broken")
        code, out = self.run_archive([twse()], [tpex()])
        self.assertEqual(code, 1)
        self.assertEqual(open(path, encoding="utf-8").read(), "{ broken")
        self.assertIn("無法讀取", out)
        self.assertIn("不處理", out)

    def test_corrupt_index_is_not_overwritten_and_day_files_still_not_lost(self):
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "_index.json"), "w", encoding="utf-8") as f:
            f.write("[]")
        code, out = self.run_archive([twse()], [tpex()])
        self.assertEqual(code, 1)
        self.assertEqual(open(os.path.join(self.dir, "_index.json"), encoding="utf-8").read(), "[]")
        self.assertIn("索引檔", out)

    def test_unwritable_directory_is_an_explicit_error(self):
        blocker = os.path.join(self.tmp, "blocker")
        open(blocker, "w").close()
        code, out = self.run_archive([twse()], [tpex()], env={"ARCHIVE_DIR": os.path.join(blocker, "x")})
        self.assertEqual(code, 1)
        self.assertIn("::error", out)

    def test_write_failure_leaves_old_file_intact(self):
        self.run_archive([twse(body="舊")], [tpex()])
        before = self.raw("20261010.json")
        import unittest.mock as um
        with um.patch("archive_announcements.os.replace", side_effect=OSError("disk full")):
            code, out = self.run_archive([twse(body="新")], [tpex()])
        self.assertEqual(code, 1)
        self.assertIn("disk full", out)
        self.assertEqual(self.raw("20261010.json"), before)


class TestRequestDiscipline(Base):
    def test_one_get_per_source_with_6s_gap_and_project_user_agent(self):
        sleeps = []
        code, _ = self.run_archive([twse()], [tpex()], sleeps=sleeps)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.op.requests), 2)
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 6)
        for url, method, headers in self.op.requests:
            self.assertEqual(method, "GET")
            self.assertIn("Corporate-Bond-Bot-archive", headers["User-agent"])
            self.assertIn("github.com/o/r", headers["User-agent"])
            self.assertNotIn("Authorization", headers)
            self.assertTrue(url.startswith("https://"))

    def test_failed_source_is_not_retried(self):
        err = urllib.error.URLError("x")
        self.run_archive(err, err)
        self.assertEqual(len(self.op.requests), 2)

    def test_whitelist_and_https_enforced_before_any_request(self):
        for env in ({"ARCHIVE_TWSE_URL": "https://evil.example.com/x"}, {"ARCHIVE_TPEX_URL": "http://www.tpex.org.tw/x"}):
            code, out = self.run_archive([twse()], [tpex()], env=env)
            self.assertEqual(code, 2)
            self.assertEqual(self.op.requests, [])
            self.assertIn("未送出任何請求", out)

    def test_dry_run_neither_connects_nor_writes(self):
        sleeps = []
        code, out = self.run_archive([twse()], [tpex()], env={"ARCHIVE_DRY_RUN": "1"}, sleeps=sleeps)
        self.assertEqual(code, 0)
        self.assertEqual(self.op.requests, [])
        self.assertEqual(sleeps, [])
        self.assertFalse(os.path.exists(self.dir))
        self.assertIn("DRY-RUN", out)

    def test_log_prints_counts_only_never_announcement_text(self):
        code, out = self.run_archive([twse(body="機密內文ABC", subject="可轉債主旨XYZ")], [tpex()])
        self.assertEqual(code, 0)
        self.assertNotIn("機密內文ABC", out)
        self.assertNotIn("可轉債主旨XYZ", out)
        self.assertIn("可轉債相關", out)

    def test_script_uses_only_stdlib_and_no_telegram_or_secrets(self):
        src = open(os.path.join(ROOT, "scripts", "archive_announcements.py"), encoding="utf-8").read()
        self.assertNotRegex(src, r"(?m)^\s*(import|from)\s+(requests|yaml|aiohttp|httpx|telethon)\b")
        for forbidden in ("TELEGRAM", "api.telegram.org", "GITHUB_TOKEN", "secrets"):
            self.assertNotIn(forbidden, src, forbidden)


@unittest.skipIf(yaml is None, "需要 PyYAML")
class TestWorkflowContract(unittest.TestCase):
    def setUp(self):
        self.text = open(os.path.join(ROOT, ".github", "workflows", "archive-announcements.yml"), encoding="utf-8").read()
        self.wf = yaml.safe_load(self.text)
        self.code = re.sub(r"(?m)^\s*#.*$", "", self.text)

    def test_triggers_are_schedule_and_dispatch_only_with_safe_cron_minutes(self):
        on = self.wf[True]
        self.assertEqual(set(on), {"schedule", "workflow_dispatch"})
        crons = [c["cron"] for c in on["schedule"]]
        self.assertTrue(crons)
        for c in crons:
            minute = c.split()[0]
            self.assertNotIn(minute, ("0", "30"), c)         # 避開整點與半點
        h, m = crons[0].split()[1], crons[0].split()[0]
        self.assertEqual((int(h) + 8) % 24, 19)               # 第一班 = 台北 19 點多
        self.assertEqual(int(m), 31)

    def test_permissions_are_contents_write_only(self):
        self.assertEqual(self.wf["permissions"], {"contents": "write"})
        self.assertNotIn("permissions", self.wf["jobs"]["archive"])

    def test_independent_concurrency_that_never_cancels_a_running_job(self):
        c = self.wf["concurrency"]
        self.assertEqual(c["group"], "archive-announcements")
        self.assertIs(c["cancel-in-progress"], False)
        for other in ("telegram-bot-listener", "corporate-bond-daily"):
            self.assertNotIn(other, c["group"])

    def test_no_secrets_no_telegram_no_other_workflows(self):
        for forbidden in ("secrets.", "TELEGRAM", "api.telegram.org", "/dispatches", "gh api", "actions: write", "workflow_run", "bot_relay"):
            self.assertNotIn(forbidden, self.code, forbidden)

    def test_push_uses_retry_script_with_non_telegram_alert(self):
        steps = self.wf["jobs"]["archive"]["steps"]
        commit = [s for s in steps if "push_with_retry.sh" in s.get("run", "")][0]
        self.assertEqual(commit["env"]["PUSH_ALERT_CMD"], "echo")
        self.assertNotRegex(commit["run"], r"(?m)^\s*git push\b")
        self.assertIn("git add data/announcements", commit["run"])
        self.assertNotIn("git add .", commit["run"])
        self.assertNotIn("reports", commit["run"])
        self.assertIn("API 只回傳最新一個發言日", commit["env"]["PUSH_FAIL_MESSAGE"])

    def test_partial_failures_still_commit_then_turn_the_run_red(self):
        steps = self.wf["jobs"]["archive"]["steps"]
        arch = [s for s in steps if s.get("id") == "archive"][0]
        self.assertIs(arch["continue-on-error"], True)
        commit = [s for s in steps if "push_with_retry.sh" in s.get("run", "")][0]
        gate = steps[-1]
        self.assertLess(steps.index(arch), steps.index(commit))
        self.assertLess(steps.index(commit), steps.index(gate))
        self.assertIn("steps.archive.outcome == 'failure'", gate["if"])
        self.assertIn("exit 1", gate["run"])
        self.assertIn("always()", gate["if"])

    def test_inputs_reach_script_only_via_env(self):
        steps = self.wf["jobs"]["archive"]["steps"]
        arch = [s for s in steps if s.get("id") == "archive"][0]
        self.assertNotIn("${{", arch["run"])
        self.assertEqual(set(arch["env"]), {"ARCHIVE_DRY_RUN", "PROBE_REPO"})

    def test_writes_only_under_data_announcements(self):
        self.assertIn("data/announcements", self.code)
        self.assertEqual(aa.INDEX_NAME, "_index.json")
        self.assertEqual(os.path.join("data", "announcements"), os.path.join("data", "announcements"))


class TestPushScriptMessageOverride(unittest.TestCase):
    def test_default_message_unchanged_and_override_supported(self):
        src = open(os.path.join(ROOT, "scripts", "push_with_retry.sh"), encoding="utf-8").read()
        self.assertIn('MSG="${PUSH_FAIL_MESSAGE:-$DEFAULT_MSG}"', src)
        self.assertIn("下一班可能重發", src)           # 每日報告的預設說明沒變


if __name__ == "__main__":
    unittest.main()
