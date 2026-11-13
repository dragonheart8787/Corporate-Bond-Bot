"""archive-fsc 失敗／恢復通知（scripts/fsc_alert.py）的測試。全部離線：假的 GitHub API 回應、假的送訊函式；socket 在本模組內被封鎖。"""

import contextlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)

import fsc_alert as fa  # noqa: E402

_real_socket = socket.socket


def setUpModule():
    def blocked(*a, **k):
        raise AssertionError("測試不得建立真的網路連線")
    socket.socket = blocked  # type: ignore[assignment]


def tearDownModule():
    socket.socket = _real_socket  # type: ignore[assignment]


TOKEN = "123456789:AAFAKE-token-value-for-tests"
CHAT = "-1001234567890"
GH = "ghs_FAKEGITHUBTOKENVALUE0123456789"
SENT = ["機密公司甲", "機密承銷商乙", "88888888"]


def run(rid, concl, status="completed", title="留存證期局申報案件彙總表"):
    return {"id": rid, "status": status, "conclusion": concl, "display_title": title, "name": title}


class FakeResp:
    def __init__(self, obj, status=200):
        self._b, self.status = json.dumps(obj).encode("utf-8"), status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeApi:
    """依序回傳每一頁；記錄請求。pages: [[run, ...], ...]（每頁最多 100 筆）。"""

    def __init__(self, pages=None, error=None, raw=None):
        self.pages, self.error, self.raw, self.requests = pages or [[]], error, raw, []

    def __call__(self, req, timeout=None):
        self.requests.append((req.full_url, dict(req.header_items()), timeout))
        if self.error:
            raise self.error
        if self.raw is not None:
            return FakeResp(self.raw)
        import urllib.parse
        page = int(urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)["page"][0])
        return FakeResp({"workflow_runs": self.pages[page - 1] if page <= len(self.pages) else []})


def hist(*concls, start=99):
    """由新到舊的歷史 run（id 由 start 往下）。"""
    return [run(start - i, c) for i, c in enumerate(concls)]


LOG_115_GATE = ("== [頁面] HTTP 200\n::error title=留存失敗 115年度申報案件::格式切換閘門：共有案件鍵 41／上一版 50（82.0%）低於 90%，不寫入\n"
                "### 114年度申報案件（unchanged）\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fsc = os.path.join(self.tmp, "fsc")
        os.makedirs(self.fsc)
        with open(os.path.join(self.fsc, "_last_check.json"), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "years": {"114": "2026-11-13", "115": "2026-11-12"}}, f)
        self.logfile = os.path.join(self.tmp, "archive.log")
        self.sent = []

    def write_log(self, text):
        with open(self.logfile, "w", encoding="utf-8") as f:
            f.write(text)

    def env(self, **kw):
        e = {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "100", "GITHUB_REF_NAME": "master", "GITHUB_SERVER_URL": "https://github.com",
             "GITHUB_WORKFLOW": "留存證期局申報案件彙總表（只存檔；失敗才通知）", "GH_API_TOKEN": GH, "ARCHIVE_OUTCOME": "failure", "COMMIT_OUTCOME": "success",
             "ARCHIVE_LOG": self.logfile, "ARCHIVE_FSC_DIR": self.fsc, "ARCHIVE_DRY_RUN": "0", "ALERT_TEST": "0",
             "TELEGRAM_BOT_TOKEN": TOKEN, "BOT_ALERT_CHAT_ID": CHAT}
        e.update(kw)
        return e

    def go(self, api, env=None, send_ok=True, send_exc=None):
        def send(text):
            self.sent.append(text)
            if send_exc:
                raise send_exc
            return send_ok
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fa.main(env or self.env(), opener=api, send=send)
        return code, out.getvalue()


# ─────────────────────────────────────────
class TestClassify(unittest.TestCase):
    CASES = [
        ("115年度申報案件", "頁面上找不到儲存格文字完全等於「115年度申報案件」的列", fa.CAT_PAGE),
        ("115年度申報案件", "頁面上有 2 列的儲存格文字完全等於「115年度申報案件」，無法確定是哪一列", fa.CAT_PAGE),
        ("115年度申報案件", "「115年度申報案件」那一列沒有 .xlsx 也沒有 .ods 連結（.xls 不使用）", fa.CAT_LINK),
        ("115年度申報案件", "「115年度申報案件」那一列有 2 個不同的 .ods 連結：['a.ods', 'b.ods']，無法確定", fa.CAT_LINK),
        ("115年度申報案件", "「115年度申報案件」那一列有 2 個不同的 .xlsx 連結：['a'，'b']，無法確定", fa.CAT_LINK),
        ("115年度申報案件", "「115年度申報案件」的 xlsx 連結不是 https（http），不下載", fa.CAT_LINK),
        ("115年度申報案件", "「115年度申報案件」的 ods 連結不是 https 或主機 'x.example' 不在白名單", fa.CAT_LINK),
        ("115年度申報案件", "格式切換閘門：共有案件鍵 41／上一版 50（82.0%）低於 90%，不寫入", fa.CAT_GATE),
        ("115年度申報案件", "格式切換閘門：檔名日期與頁面更新日期相差 4 天（超過 3 天），不寫入", fa.CAT_GATE),
        ("115年度申報案件", "HTTP 403 （被拒絕：可能是來源擋雲端／資料中心 IP，或需要特定標頭）", fa.CAT_DOWNLOAD),
        ("115年度申報案件", "連線失敗：URLError: boom（轉址目的主機：x）", fa.CAT_DOWNLOAD),
        ("115年度申報案件", "回應超過大小上限，被截斷", fa.CAT_DOWNLOAD),
        ("頁面", "HTTP 500", fa.CAT_DOWNLOAD),
        ("頁面", "逾時（20 秒）", fa.CAT_DOWNLOAD),
        ("115年度申報案件", "ods 無法解析：檔案開頭不是 zip（HTML 文件）；副檔名與實際格式不符或被阻擋頁取代", fa.CAT_CONTENT),
        ("115年度申報案件", "表頭與預期的 18 欄不符（欄數 17；缺少 ['案件性質']；多出 []；名稱或欄數不同），不寫入", fa.CAT_CONTENT),
        ("115年度申報案件", "資料列數 40 比前一版 50 少 10 列（≥20%），疑似資料被截斷或改版，不寫入", fa.CAT_CONTENT),
        ("最後檢查紀錄", "既有的最後檢查紀錄 x 結構不符（欄位或日期格式異常），為避免覆蓋，不更新", fa.CAT_OTHER),
        ("115年度申報案件", "未預期的錯誤 RuntimeError: boom", fa.CAT_OTHER),
    ]

    def test_categories_for_real_archive_messages(self):
        for scope, msg, want in self.CASES:
            with self.subTest(msg=msg[:30]):
                self.assertEqual(fa.classify(scope, msg), want)

    def test_real_messages_are_really_produced_by_archive_fsc(self):
        """分類用的字樣必須真的出現在 archive_fsc／fsc_sources 的原始碼裡（改字樣時這裡會提醒同步更新）。"""
        src = open(os.path.join(ROOT, "scripts", "archive_fsc.py"), encoding="utf-8").read() + open(os.path.join(ROOT, "scripts", "fsc_sources.py"), encoding="utf-8").read()
        for needle in ("格式切換閘門", "找不到儲存格文字完全等於", "沒有 .xlsx 也沒有 .ods", "個不同的 .ods", "不在白名單", "留存失敗 頁面", "回應超過大小上限"):
            self.assertIn(needle, src, needle)

    def test_parse_failures_from_log(self):
        log = ("::error title=留存失敗 115年度申報案件::格式切換閘門：x\n::error title=留存失敗 114年度申報案件::HTTP 500\n"
               "::warning title=工作表名稱不同 114年度申報案件::x\n::error title=別的錯誤::HTTP 500\nplain text\n")
        got = [(f.scope, f.category) for f in fa.parse_failures(log)]
        self.assertEqual(got, [("115年度申報案件", fa.CAT_GATE), ("114年度申報案件", fa.CAT_DOWNLOAD)])
        self.assertEqual(fa.parse_failures(""), [])


class TestDecide(unittest.TestCase):
    def d(self, failed, h):
        return fa.decide(failed, h)

    def test_first_failure_after_success_or_no_history(self):
        for h in (["success"], ["success", "failure", "failure"], []):
            with self.subTest(h=h):
                d = self.d(True, h)
                self.assertEqual((d.kind, d.streak, d.send), ("first", 1, True))

    def test_streak_2_to_5_silent(self):
        for k in range(1, 5):
            d = self.d(True, ["failure"] * k + ["success"])
            self.assertEqual((d.kind, d.streak, d.send), ("none", k + 1, False), k)

    def test_6th_repeats_7th_silent_12th_repeats(self):
        d6 = self.d(True, ["failure"] * 5 + ["success"])
        self.assertEqual((d6.kind, d6.streak, d6.send), ("repeat", 6, True))
        d7 = self.d(True, ["failure"] * 6 + ["success"])
        self.assertEqual((d7.kind, d7.streak, d7.send), ("none", 7, False))
        d12 = self.d(True, ["failure"] * 11 + ["success"])
        self.assertEqual((d12.kind, d12.streak, d12.send), ("repeat", 12, True))
        d13 = self.d(True, ["failure"] * 12 + ["success"])
        self.assertFalse(d13.send)

    def test_window_exhausted_reports_at_least(self):
        d = self.d(True, ["failure"] * 5)             # 回溯範圍內全是失敗：N 至少 6
        self.assertEqual((d.kind, d.streak, d.at_least), ("repeat", 6, True))

    def test_recovered_only_when_previous_failed(self):
        d = self.d(False, ["failure", "failure", "success"])
        self.assertEqual((d.kind, d.streak, d.send), ("recovered", 2, True))
        self.assertEqual(self.d(False, ["failure"]).streak, 1)
        for h in (["success", "failure"], [], ["success"]):
            self.assertFalse(self.d(False, h).send, h)

    def test_api_failure_defaults(self):
        d = self.d(True, None)
        self.assertEqual((d.kind, d.streak, d.send), ("unknown", None, True))
        d = self.d(False, None)
        self.assertFalse(d.send)
        self.assertIn("無法查詢", d.reason)


class TestHistory(unittest.TestCase):
    def env(self, **kw):
        return {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "100", "GITHUB_REF_NAME": "master", "GH_API_TOKEN": GH, **kw}

    def test_conclusion_of_skips_dry_run_in_progress_cancelled_skipped(self):
        self.assertEqual(fa.conclusion_of(run(1, "success")), "success")
        self.assertEqual(fa.conclusion_of(run(1, "failure")), "failure")
        self.assertEqual(fa.conclusion_of(run(1, "timed_out")), "failure")
        self.assertIsNone(fa.conclusion_of(run(1, "success", title="留存證期局申報案件彙總表（dry-run）")))
        self.assertIsNone(fa.conclusion_of(run(1, None, status="in_progress")))
        self.assertIsNone(fa.conclusion_of(run(1, "cancelled")))
        self.assertIsNone(fa.conclusion_of(run(1, "skipped")))
        self.assertIsNone(fa.conclusion_of({"status": "completed", "conclusion": "weird"}))

    def test_excludes_current_run_and_dry_runs_and_cancelled(self):
        api = FakeApi([[run(100, "failure"), run(99, "success", title="x（dry-run）"), run(98, "cancelled"), run(97, "failure"), run(96, "success"), run(95, "failure")]])
        self.assertEqual(fa.fetch_history(self.env(), api), ["failure", "success"])      # 本次(100)、dry-run(99)、取消(98) 都不算；遇到第一個成功就停

    def test_dry_run_success_between_failures_does_not_break_the_streak(self):
        api = FakeApi([[run(99, "success", title="留存證期局申報案件彙總表（dry-run）"), run(98, "failure"), run(97, "failure"), run(96, "success")]])
        self.assertEqual(fa.fetch_history(self.env(), api), ["failure", "failure", "success"])

    def test_pagination_stops_at_first_success(self):
        p1 = [run(1000 + i, "failure") for i in range(100)]
        p2 = [run(900, "failure"), run(899, "success")] + [run(800 + i, "failure") for i in range(98)]
        api = FakeApi([p1, p2, p1])
        h = fa.fetch_history(self.env(), api)
        self.assertEqual(len(api.requests), 2)
        self.assertEqual(h.count("success"), 1)
        self.assertEqual(h[:101], ["failure"] * 101)

    def test_request_shape_and_token_only_in_header(self):
        api = FakeApi([[run(99, "success")]])
        fa.fetch_history(self.env(), api)
        url, headers, timeout = api.requests[0]
        self.assertIn("/repos/o/r/actions/workflows/archive-fsc.yml/runs?", url)
        self.assertIn("status=completed", url)
        self.assertIn("branch=master", url)
        self.assertNotIn(GH, url)
        self.assertEqual(headers["Authorization"], f"Bearer {GH}")
        self.assertEqual(timeout, fa.API_TIMEOUT)

    def test_api_problems_raise(self):
        for api in (FakeApi(error=urllib.error.HTTPError("u", 403, "Forbidden", {}, None)), FakeApi(error=urllib.error.URLError("down")),
                    FakeApi(error=TimeoutError()), FakeApi(raw={"message": "x"}), FakeApi(raw=[1, 2])):
            with self.subTest(api=api.error or api.raw), self.assertRaises(Exception):
                fa.fetch_history(self.env(), api)
        with self.assertRaises(RuntimeError):
            fa.fetch_history({"GITHUB_REPOSITORY": "o/r"}, FakeApi())           # 缺 token
        with self.assertRaises(RuntimeError):
            fa.fetch_history({"GH_API_TOKEN": GH}, FakeApi())                    # 缺 repo


class TestMainDecisionsEndToEnd(Base):
    def test_first_failure_sends_once_with_expected_content(self):
        self.write_log(LOG_115_GATE)
        code, out = self.go(FakeApi([hist("success", "success")]))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1)
        m = self.sent[0]
        for want in ("證期局申報彙總表留存失敗", "留存證期局申報案件彙總表（只存檔；失敗才通知）", "https://github.com/o/r/actions/runs/100",
                     "115年度申報案件（閘門擋下）", "114年=2026-11-13、115年=2026-11-12", "連續失敗：1 次（前一次成功）"):
            self.assertIn(want, m)
        self.assertNotIn("仍未恢復", m)

    def test_previous_failed_streak_2_is_silent(self):
        self.write_log(LOG_115_GATE)
        code, out = self.go(FakeApi([hist("failure", "success")]))
        self.assertEqual((code, self.sent), (0, []))
        self.assertIn("不是 6 的倍數", out)

    def test_6th_consecutive_repeats_and_7th_silent(self):
        self.write_log(LOG_115_GATE)
        self.go(FakeApi([hist(*(["failure"] * 5 + ["success"]))]))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("連續失敗：6 次", self.sent[0])
        self.assertIn("仍未恢復", self.sent[0])
        self.sent.clear()
        self.go(FakeApi([hist(*(["failure"] * 6 + ["success"]))]))
        self.assertEqual(self.sent, [])

    def test_success_after_failure_sends_recovered(self):
        code, out = self.go(FakeApi([hist("failure", "failure", "failure", "success")]), env=self.env(ARCHIVE_OUTCOME="success"))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1)
        m = self.sent[0]
        self.assertIn("已恢復", m)
        self.assertIn("先前連續失敗：3 次", m)
        self.assertIn("114年=2026-11-13、115年=2026-11-12", m)
        self.assertNotIn("失敗項目", m)

    def test_success_after_success_is_silent(self):
        self.go(FakeApi([hist("success", "failure")]), env=self.env(ARCHIVE_OUTCOME="success"))
        self.assertEqual(self.sent, [])

    def test_api_failure_failure_run_sends_with_unknown_streak_and_logs_why(self):
        self.write_log(LOG_115_GATE)
        code, out = self.go(FakeApi(error=urllib.error.URLError("down")))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("連續失敗次數：無法查詢", self.sent[0])
        self.assertIn("查詢前幾次 run 失敗", out)
        self.assertIn("寧可多發不要漏發", out)

    def test_api_failure_with_success_run_sends_nothing(self):
        code, out = self.go(FakeApi(error=urllib.error.URLError("down")), env=self.env(ARCHIVE_OUTCOME="success"))
        self.assertEqual((code, self.sent), (0, []))
        self.assertIn("不發「已恢復」", out)

    def test_missing_gh_token_is_an_api_failure_and_still_notifies(self):
        self.write_log(LOG_115_GATE)
        e = self.env()
        del e["GH_API_TOKEN"]
        code, out = self.go(FakeApi(), env=e)
        self.assertEqual(len(self.sent), 1)

    def test_commit_step_failure_alone_counts_as_failure(self):
        self.write_log("")
        code, out = self.go(FakeApi([hist("success")]), env=self.env(ARCHIVE_OUTCOME="success", COMMIT_OUTCOME="failure"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("data/fsc 提交推送（提交推送失敗）", self.sent[0])

    def test_skipped_archive_step_counts_as_failure(self):
        self.go(FakeApi([hist("success")]), env=self.env(ARCHIVE_OUTCOME="skipped", COMMIT_OUTCOME="skipped"))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("前置步驟失敗", self.sent[0])

    def test_two_years_failing_lists_both_with_categories(self):
        self.write_log("::error title=留存失敗 115年度申報案件::HTTP 403\n::error title=留存失敗 114年度申報案件::「114年度申報案件」那一列沒有 .xlsx 也沒有 .ods 連結（.xls 不使用）\n")
        self.go(FakeApi([hist("success")]))
        self.assertIn("115年度申報案件（下載錯誤）；114年度申報案件（連結異常）", self.sent[0])

    def test_unreadable_or_missing_log_still_notifies(self):
        e = self.env(ARCHIVE_LOG=os.path.join(self.tmp, "nope.log"))
        code, out = self.go(FakeApi([hist("success")]), env=e)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("無法從輸出判斷", self.sent[0])

    def test_last_check_missing_or_corrupt(self):
        os.remove(os.path.join(self.fsc, "_last_check.json"))
        self.write_log(LOG_115_GATE)
        self.go(FakeApi([hist("success")]))
        self.assertIn("（無紀錄）", self.sent[0])
        self.sent.clear()
        with open(os.path.join(self.fsc, "_last_check.json"), "w") as f:
            f.write("{broken")
        self.go(FakeApi([hist("success")]))
        self.assertIn("（無紀錄）", self.sent[0])
        self.sent.clear()
        with open(os.path.join(self.fsc, "_last_check.json"), "w") as f:
            json.dump({"version": 1, "years": {"115": "not-a-date"}}, f)
        self.go(FakeApi([hist("success")]))
        self.assertIn("（紀錄格式異常）", self.sent[0])


class TestNeverSendsWhenItShouldNot(Base):
    def test_dry_run_sends_nothing_and_does_not_even_query(self):
        api = FakeApi([hist("success")])
        code, out = self.go(api, env=self.env(ARCHIVE_DRY_RUN="1"))
        self.assertEqual((code, self.sent, api.requests), (0, [], []))

    def test_cancelled_sends_nothing(self):
        api = FakeApi([hist("success")])
        for kw in ({"ARCHIVE_OUTCOME": "cancelled"}, {"COMMIT_OUTCOME": "cancelled"}):
            self.go(api, env=self.env(**kw))
        self.assertEqual((self.sent, api.requests), ([], []))

    def test_unknown_outcomes_send_nothing(self):
        api = FakeApi([hist("success")])
        code, out = self.go(api, env=self.env(ARCHIVE_OUTCOME=""))
        self.assertEqual((code, self.sent), (0, []))
        self.assertIn("未知的步驟結果", out)

    def test_test_alert_prints_message_and_never_sends_even_with_secrets_and_a_send_decision(self):
        self.write_log(LOG_115_GATE)
        with mock.patch("bot_relay.send_telegram_alert") as real_send:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = fa.main(self.env(ALERT_TEST="1"), opener=FakeApi([hist("success")]))     # send=None → 若會送就走 bot_relay
            self.assertEqual(code, 0)
            real_send.assert_not_called()
        o = out.getvalue()
        self.assertIn("TEST_ALERT=true", o)
        self.assertIn("【測試預覽，不會送出】", o)
        self.assertIn("115年度申報案件（閘門擋下）", o)
        for secret in (TOKEN, CHAT, GH):
            self.assertNotIn(secret, o)

    def test_test_alert_on_a_successful_run_prints_both_synthetic_samples_and_never_sends(self):
        """本次成功（真實決策是不發）時，test_alert 仍要印出一則失敗樣本與一則恢復樣本；全程不呼叫 Telegram。"""
        with mock.patch("bot_relay.send_telegram_alert") as real_send:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = fa.main(self.env(ALERT_TEST="1", ARCHIVE_OUTCOME="success", TELEGRAM_BOT_TOKEN="", BOT_ALERT_CHAT_ID=""),
                               opener=FakeApi([hist("success", "success")]))                  # send=None：若會送出就走 bot_relay
            real_send.assert_not_called()
        o = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("本次真實狀態不需要發送通知", o)
        self.assertIn("【失敗樣本（合成資料", o)
        self.assertIn("【恢復樣本（合成資料", o)
        self.assertIn("證期局申報彙總表留存失敗", o)
        self.assertIn("證期局申報彙總表留存已恢復", o)
        self.assertEqual(o.count("【測試預覽，不會送出】"), 2)
        self.assertIn("Telegram secrets 已被替換為空字串：是", o)
        self.assertNotIn("::warning", o)
        # 樣本是合成的：不含 fixtures 裡的真實檢查日（2026-11-13／2026-11-12），只有合成日期
        for real in ("2026-11-13", "2026-11-12"):
            self.assertNotIn(real, o)
        self.assertEqual(o.count("（合成樣本）"), 2)

    def test_test_alert_on_a_failed_run_also_prints_both_samples(self):
        self.write_log(LOG_115_GATE)
        code, out = self.go(FakeApi([hist("failure", "success")]), env=self.env(ALERT_TEST="1"))
        self.assertEqual(self.sent, [])
        self.assertIn("本次真實狀態不需要發送通知", out)         # 連續第 2 次：真實決策不發
        self.assertIn("【失敗樣本（合成資料", out)
        self.assertIn("【恢復樣本（合成資料", out)
        self.sent.clear()
        code, out = self.go(FakeApi([hist("success")]), env=self.env(ALERT_TEST="1"))
        self.assertEqual(self.sent, [])
        self.assertIn("【本次真實狀態會發送的內容（預覽，不送出）】", out)   # 連續第 1 次：另外預覽真實內容
        self.assertIn("115年度申報案件（閘門擋下）", out)
        self.assertIn("【失敗樣本（合成資料", out)

    def test_samples_come_from_the_same_function_as_real_messages(self):
        env = self.env()
        fail_s, rec_s = fa.sample_messages(env)
        real_fail = fa.build_message(env, fa.Decision("first", 1), True, [fa.Failure("115年度申報案件", fa.CAT_GATE)], False, self.fsc)
        real_rec = fa.build_message(env, fa.Decision("recovered", 2), False, [], False, self.fsc)
        keys = lambda m: [ln.split("：")[0] for ln in m.splitlines() if "：" in ln]
        self.assertEqual(keys(fail_s), keys(real_fail))
        self.assertEqual(keys(rec_s), keys(real_rec))
        for m in (fail_s, rec_s):
            for secret in (TOKEN, CHAT, GH):
                self.assertNotIn(secret, m)
            for s_ in SENT:
                self.assertNotIn(s_, m)

    def test_test_alert_with_non_empty_secrets_warns_but_still_never_sends(self):
        with mock.patch("bot_relay.send_telegram_alert") as real_send:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                fa.main(self.env(ALERT_TEST="1"), opener=FakeApi([hist("success")]))        # env() 帶著 token／chat id
            real_send.assert_not_called()
        self.assertIn("::warning title=test_alert 時 secrets 應為空字串::", out.getvalue())
        self.assertIn("已被替換為空字串：否", out.getvalue())
        for secret in (TOKEN, CHAT, GH):
            self.assertNotIn(secret, out.getvalue())

    def test_test_alert_samples_are_printed_even_when_the_api_fails(self):
        code, out = self.go(FakeApi(error=urllib.error.URLError("down")), env=self.env(ALERT_TEST="1", ARCHIVE_OUTCOME="success"))
        self.assertEqual((code, self.sent), (0, []))
        self.assertIn("【失敗樣本（合成資料", out)
        self.assertIn("【恢復樣本（合成資料", out)

    def test_dry_run_beats_test_alert(self):
        api = FakeApi([hist("success")])
        code, out = self.go(api, env=self.env(ALERT_TEST="1", ARCHIVE_DRY_RUN="1"))
        self.assertEqual((self.sent, api.requests), ([], []))
        self.assertNotIn("樣本", out)

    def test_there_is_no_env_switch_that_forces_a_real_send_in_test_mode(self):
        src = open(os.path.join(ROOT, "scripts", "fsc_alert.py"), encoding="utf-8").read()
        i = src.index("if test:")
        j = src.index("if not d.send:")
        self.assertIn("return 0", src[i:j])                      # test 分支一定在送出之前 return
        for name in ("FORCE", "REAL_SEND", "SEND_FOR_REAL"):
            self.assertNotIn(name, src)


class TestNotificationFailureNeverMasksTheOriginalFailure(Base):
    def test_send_returns_false_is_a_warning_not_an_error_and_exit_is_zero(self):
        self.write_log(LOG_115_GATE)
        code, out = self.go(FakeApi([hist("success")]), send_ok=False)
        self.assertEqual(code, 0)
        self.assertIn("::warning title=通知未送出::", out)
        self.assertNotIn("::error", out)

    def test_send_raises_is_contained(self):
        self.write_log(LOG_115_GATE)
        code, out = self.go(FakeApi([hist("success")]), send_exc=RuntimeError("boom"))
        self.assertEqual(code, 0)
        self.assertIn("::warning title=通知未送出::", out)
        self.assertNotIn("boom", out)
        self.assertNotIn("::error", out)

    def test_unexpected_exception_anywhere_is_a_warning_exit_zero(self):
        out = io.StringIO()
        with mock.patch.object(fa, "decide", side_effect=ValueError("x")), contextlib.redirect_stdout(out):
            code = fa.main(self.env(), opener=FakeApi([hist("success")]), send=lambda t: True)
        self.assertEqual(code, 0)
        self.assertIn("::warning title=通知程式例外::ValueError", out.getvalue())

    def test_real_sender_without_secrets_returns_false_without_network(self):
        self.write_log(LOG_115_GATE)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fa.main(self.env(TELEGRAM_BOT_TOKEN="", BOT_ALERT_CHAT_ID=""), opener=FakeApi([hist("success")]))        # send=None → bot_relay，沒 secrets 不會連線
        self.assertEqual(code, 0)
        self.assertIn("::warning title=通知未送出::", out.getvalue())

    def test_sending_reuses_bot_relay_alert_with_only_the_two_secret_env_names(self):
        self.write_log(LOG_115_GATE)
        with mock.patch("bot_relay.send_telegram_alert", return_value=True) as real_send, contextlib.redirect_stdout(io.StringIO()):
            fa.main(self.env(), opener=FakeApi([hist("success")]))
        real_send.assert_called_once()
        text, env = real_send.call_args[0]
        self.assertEqual(set(env), {"TELEGRAM_BOT_TOKEN", "BOT_ALERT_CHAT_ID"})
        self.assertIn("證期局申報彙總表留存失敗", text)
        src = open(os.path.join(ROOT, "scripts", "fsc_alert.py"), encoding="utf-8").read()
        self.assertNotIn("api.telegram.org", src)                # 不另寫一套送訊程式


class TestMessageHygiene(Base):
    def test_no_secrets_no_company_names_no_amounts_in_message_or_log(self):
        # ::error:: 原文裡就算混進公司名稱與金額，也不得被轉貼到通知（只輸出分類）
        self.write_log("::error title=留存失敗 115年度申報案件::資料異常 機密公司甲 機密承銷商乙 88888888 HTTP 500\n")
        code, out = self.go(FakeApi([hist("success")]))
        everything = self.sent[0] + out
        for s in SENT + [TOKEN, CHAT, GH, "AAFAKE", "1234567890"]:
            self.assertNotIn(s, everything, s)

    def test_message_for_recovery_has_no_secrets_either(self):
        code, out = self.go(FakeApi([hist("failure", "success")]), env=self.env(ARCHIVE_OUTCOME="success"))
        for s in [TOKEN, CHAT, GH]:
            self.assertNotIn(s, self.sent[0] + out)

    def test_message_is_chinese_and_bounded(self):
        self.write_log("".join(f"::error title=留存失敗 {y}年度申報案件::HTTP 500\n" for y in range(100, 140)))
        self.go(FakeApi([hist("success")]))
        self.assertLess(len(self.sent[0]), 3500)                 # Telegram 單則 4096 字上限內
        self.assertRegex(self.sent[0], r"[一-鿿]")

    def test_log_never_prints_the_api_token(self):
        self.write_log(LOG_115_GATE)
        code, out = self.go(FakeApi(error=urllib.error.HTTPError("https://x/?t=" + GH, 401, "bad", {}, None)))
        self.assertNotIn(GH, out)


if __name__ == "__main__":
    unittest.main()
