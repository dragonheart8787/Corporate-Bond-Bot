"""bot_relay 的測試：全部用 mock，不會碰到真的 GitHub / Telegram，也不會發送任何訊息。

執行：python -m unittest discover -s tests -t . -v
"""

import base64
import contextlib
import io
import json
import os
import time
import unittest
import urllib.error
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import bot_relay as relay

TW = ZoneInfo("Asia/Taipei")
FAKE_GH_TOKEN = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
FAKE_BOT_TOKEN = "123456789:" + "AAFakeTokenFakeTokenFakeTokenFake12"


class FakeResp:
    def __init__(self, status=204, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def make_opener(*outcomes):
    """依序回傳／拋出 outcomes；耗盡後重複最後一個。同時記錄每次請求。"""
    calls = []
    seq = list(outcomes)

    def opener(req, timeout=None):
        calls.append(req)
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item

    opener.calls = calls
    return opener


def http_error(code, body=b"boom"):
    return urllib.error.HTTPError("https://api.github.com/x", code, "err", {}, io.BytesIO(body))


def capture(fn, *a, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


class TestRedact(unittest.TestCase):
    def test_telegram_token_in_requests_style_error(self):
        msg = f"HTTPSConnectionPool: Max retries exceeded with url: /bot{FAKE_BOT_TOKEN}/getUpdates"
        out = relay.redact(msg)
        self.assertNotIn(FAKE_BOT_TOKEN, out)
        self.assertIn("***", out)

    def test_env_secret_is_masked_even_without_known_format(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_SESSION_STRING": "super-secret-session-value"}):
            self.assertNotIn("super-secret-session-value", relay.redact("x super-secret-session-value y"))

    def test_github_token_masked(self):
        self.assertNotIn(FAKE_GH_TOKEN, relay.redact(f"Authorization: Bearer {FAKE_GH_TOKEN}"))

    def test_chat_id_mask_is_stable_distinct_and_does_not_leak_digits(self):
        a, b = relay.mask_chat_id(-5062625686), relay.mask_chat_id(-1001234567890)
        self.assertEqual(a, relay.mask_chat_id("-5062625686"))
        self.assertNotEqual(a, b)
        self.assertNotIn("5062625686", a)
        self.assertTrue(a.startswith("chat#"))


class TestGhRequest(unittest.TestCase):
    def test_success_sends_auth_and_json(self):
        op = make_opener(FakeResp(204))
        status, _ = relay.gh_request("POST", "/repos/o/r/actions/workflows/bot.yml/dispatches", FAKE_GH_TOKEN,
                                     {"ref": "master"}, opener=op)
        self.assertEqual(status, 204)
        req = op.calls[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"ref": "master"})
        self.assertEqual(req.get_header("Authorization"), f"Bearer {FAKE_GH_TOKEN}")

    def test_non_2xx_raises_with_status_and_permission_hint_and_no_token(self):
        op = make_opener(http_error(403, b'{"message":"Resource not accessible by integration"}'))
        with self.assertRaises(relay.GitHubApiError) as cm:
            relay.gh_request("POST", "/x", FAKE_GH_TOKEN, {}, opener=op)
        self.assertEqual(cm.exception.status, 403)
        self.assertIn("actions: write", str(cm.exception))
        self.assertNotIn(FAKE_GH_TOKEN, str(cm.exception))

    def test_network_error_has_no_status(self):
        op = make_opener(urllib.error.URLError("dns down"))
        with self.assertRaises(relay.GitHubApiError) as cm:
            relay.gh_request("GET", "/x", FAKE_GH_TOKEN, opener=op)
        self.assertIsNone(cm.exception.status)

    def test_missing_token_is_config_error(self):
        with self.assertRaises(relay.GitHubConfigError):
            relay.gh_request("GET", "/x", "", opener=make_opener(FakeResp(200)))

    def test_200_but_not_2xx_range_status(self):
        op = make_opener(FakeResp(302))
        with self.assertRaises(relay.GitHubApiError):
            relay.gh_request("GET", "/x", FAKE_GH_TOKEN, opener=op)


class TestWithRetry(unittest.TestCase):
    def test_retries_then_succeeds_with_exponential_backoff(self):
        n = {"i": 0}

        def flaky():
            n["i"] += 1
            if n["i"] < 3:
                raise relay.GitHubApiError(500, "x")
            return "ok"

        sleeps = []
        result, _ = capture(relay.with_retry, flaky, attempts=5, base_delay=5, sleep=sleeps.append, label="t ")
        self.assertEqual(result, "ok")
        self.assertEqual(n["i"], 3)
        self.assertEqual(sleeps, [5, 15])

    def test_final_failure_is_raised_not_swallowed(self):
        def always():
            raise relay.GitHubApiError(500, "still down")

        sleeps = []
        with self.assertRaises(relay.GitHubApiError):
            capture(relay.with_retry, always, attempts=3, base_delay=1, sleep=sleeps.append)
        self.assertEqual(len(sleeps), 2)   # 最後一次失敗後不再 sleep

    def test_config_error_is_not_retried(self):
        n = {"i": 0}

        def cfg():
            n["i"] += 1
            raise relay.GitHubConfigError(None, "no token")

        with self.assertRaises(relay.GitHubConfigError):
            capture(relay.with_retry, cfg, attempts=5, base_delay=1, sleep=lambda s: None)
        self.assertEqual(n["i"], 1)


class TestDispatch(unittest.TestCase):
    def test_posts_to_correct_endpoint_with_ref_and_inputs(self):
        op = make_opener(FakeResp(204))
        _, out = capture(relay.dispatch_workflow, "o/r", "bot.yml", "master",
                         {"handoff_prev_run_id": "42"}, FAKE_GH_TOKEN, opener=op, sleep=lambda s: None)
        self.assertEqual(len(op.calls), 1)
        self.assertTrue(op.calls[0].full_url.endswith("/repos/o/r/actions/workflows/bot.yml/dispatches"))
        self.assertEqual(json.loads(op.calls[0].data), {"ref": "master", "inputs": {"handoff_prev_run_id": "42"}})
        self.assertNotIn(FAKE_GH_TOKEN, out)

    def test_dry_run_makes_no_request(self):
        op = make_opener(FakeResp(204))
        _, out = capture(relay.dispatch_workflow, "o/r", "bot.yml", "master", {}, FAKE_GH_TOKEN,
                         dry_run=True, opener=op)
        self.assertEqual(op.calls, [])
        self.assertIn("dry-run", out)

    def test_retries_on_permission_error_then_gives_up_and_raises(self):
        op = make_opener(http_error(403))
        with self.assertRaises(relay.GitHubApiError):
            capture(relay.dispatch_workflow, "o/r", "bot.yml", "master", {}, FAKE_GH_TOKEN,
                    attempts=3, base_delay=0, opener=op, sleep=lambda s: None)
        self.assertEqual(len(op.calls), 3)   # 權限不足也重試（使用者要求），最終仍拋出

    def test_recovers_when_first_attempts_fail(self):
        op = make_opener(http_error(502), urllib.error.URLError("x"), FakeResp(204))
        _, out = capture(relay.dispatch_workflow, "o/r", "bot.yml", "master", {}, FAKE_GH_TOKEN,
                         attempts=4, base_delay=0, opener=op, sleep=lambda s: None)
        self.assertEqual(len(op.calls), 3)
        self.assertIn("已觸發", out)


class TestRelayState(unittest.TestCase):
    def make(self, **kw):
        return relay.RelayState(start_ts=1000.0, max_runtime_sec=1000.0, lead_sec=100.0, **kw)   # relay_at = 1900

    def test_idle_before_relay_time_then_triggers_once(self):
        st, calls = self.make(), []
        self.assertEqual(st.step(1500, lambda: calls.append(1)), "idle")
        self.assertEqual(st.step(1900, lambda: calls.append(1)), "ok")
        self.assertTrue(st.done)
        self.assertEqual(st.step(1950, lambda: calls.append(1)), "idle")   # 不會重複觸發
        self.assertEqual(len(calls), 1)

    def test_failure_schedules_retry_then_succeeds(self):
        st = self.make(retry_interval_sec=30)
        n = {"i": 0}

        def fn():
            n["i"] += 1
            if n["i"] == 1:
                raise relay.GitHubApiError(403, "denied")

        self.assertEqual(st.step(1900, fn), "retry_later")
        self.assertEqual(st.failures, 1)
        self.assertIn("denied", st.last_error)
        self.assertEqual(st.step(1910, fn), "idle")           # 還沒到下次重試時間
        self.assertEqual(st.step(1931, fn), "ok")
        self.assertEqual(n["i"], 2)

    def test_gives_up_when_no_time_left_for_another_retry(self):
        st = self.make(retry_interval_sec=60)

        def fn():
            raise relay.GitHubApiError(500, "down")

        self.assertEqual(st.step(1900, fn), "retry_later")    # 1900+60=1960 < deadline 2000，還有時間再試
        self.assertEqual(st.step(1960, fn), "gave_up")        # 1960+60=2020 >= deadline 2000，沒時間了
        self.assertTrue(st.gave_up and not st.done)
        self.assertEqual(st.failures, 2)
        self.assertEqual(st.step(1995, fn), "idle")           # 放棄後不再嘗試

    def test_error_text_is_redacted(self):
        st = self.make()

        def fn():
            raise relay.GitHubApiError(None, f"bad {FAKE_GH_TOKEN}")

        st.step(1900, fn)
        self.assertNotIn(FAKE_GH_TOKEN, st.last_error)


class TestPrevRunAndLastSent(unittest.TestCase):
    def test_prev_run_by_id(self):
        body = json.dumps({"id": 7, "updated_at": "2026-10-07T02:11:41Z", "conclusion": "success"}).encode()
        info = relay.fetch_prev_run_info("o/r", FAKE_GH_TOKEN, "8", "7", opener=make_opener(FakeResp(200, body)))
        self.assertEqual(info["run_id"], 7)
        self.assertEqual(info["ended_at"].hour, 2)

    def test_prev_run_without_id_skips_self(self):
        body = json.dumps({"workflow_runs": [
            {"id": 9, "updated_at": "2026-10-07T05:00:00Z", "conclusion": "success"},
            {"id": 8, "updated_at": "2026-10-07T02:00:00Z", "conclusion": "failure"},
        ]}).encode()
        info = relay.fetch_prev_run_info("o/r", FAKE_GH_TOKEN, "9", "", opener=make_opener(FakeResp(200, body)))
        self.assertEqual(info["run_id"], 8)

    def test_prev_run_api_error_is_logged_and_returns_none(self):
        info, out = capture(relay.fetch_prev_run_info, "o/r", FAKE_GH_TOKEN, "9", "",
                            opener=make_opener(http_error(500)))
        self.assertIsNone(info)
        self.assertIn("查詢上一棒", out)

    def test_read_last_sent_decodes(self):
        body = json.dumps({"content": base64.b64encode(b"20261007\n").decode()}).encode()
        self.assertEqual(relay.read_last_sent("o/r", FAKE_GH_TOKEN, "master", opener=make_opener(FakeResp(200, body))),
                         "20261007")

    def test_read_last_sent_404_means_not_sent(self):
        self.assertEqual(relay.read_last_sent("o/r", FAKE_GH_TOKEN, "master", opener=make_opener(http_error(404))), "")

    def test_read_last_sent_other_errors_propagate(self):
        with self.assertRaises(relay.GitHubApiError):
            relay.read_last_sent("o/r", FAKE_GH_TOKEN, "master", opener=make_opener(http_error(500)))

    def test_daily_run_active(self):
        busy = json.dumps({"workflow_runs": [{"status": "completed"}, {"status": "in_progress"}]}).encode()
        idle = json.dumps({"workflow_runs": [{"status": "completed"}]}).encode()
        self.assertTrue(relay.daily_run_active("o/r", FAKE_GH_TOKEN, opener=make_opener(FakeResp(200, busy))))
        self.assertFalse(relay.daily_run_active("o/r", FAKE_GH_TOKEN, opener=make_opener(FakeResp(200, idle))))


class TestDailyTrigger(unittest.TestCase):
    def setUp(self):
        self.t = relay.DailyTrigger()
        self.dispatched = []
        self.sent = ""
        self.active = False

    def tick(self, now_tw, now_ts=10_000.0, **over):
        kw = dict(
            read_sent=lambda: self.sent,
            run_active=lambda: self.active,
            dispatch=lambda: self.dispatched.append(1),
        )
        kw.update(over)
        return capture(self.t.tick, now_ts, now_tw, **kw)[0]

    def dt(self, h, m=0, day=7):
        return datetime(2026, 10, day, h, m, tzinfo=TW)

    def test_window_boundaries_in_taipei_time(self):
        self.assertEqual(self.tick(self.dt(17, 59))[0], "outside_window")
        self.assertEqual(self.tick(self.dt(18, 29))[0], "outside_window")
        self.assertEqual(self.tick(self.dt(18, 30))[0], "dispatched")

    def test_after_midnight_still_targets_previous_report_date(self):
        action, date = self.tick(self.dt(0, 30, day=8))
        self.assertEqual((action, date), ("dispatched", "20261007"))   # rollover：06:00 前屬於前一日

    def test_window_closes_at_rollover_hour(self):
        self.assertEqual(self.tick(self.dt(5, 59, day=8))[0], "dispatched")
        self.t = relay.DailyTrigger()
        self.assertEqual(self.tick(self.dt(6, 0, day=8))[0], "outside_window")

    def test_result_does_not_depend_on_runner_timezone(self):
        # runner 的 TZ 設成別的時區，判斷依據仍是傳入的台北時間
        old = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/Los_Angeles"
            time.tzset()
            self.assertEqual(self.tick(self.dt(18, 30))[0], "dispatched")
            self.t = relay.DailyTrigger()
            self.assertEqual(self.tick(self.dt(12, 0))[0], "outside_window")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_already_sent_does_not_dispatch(self):
        self.sent = "20261007"
        self.assertEqual(self.tick(self.dt(19))[0], "already_sent")
        self.assertEqual(self.dispatched, [])

    def test_sent_marker_of_previous_day_does_not_block(self):
        self.sent = "20261006"
        self.assertEqual(self.tick(self.dt(19))[0], "dispatched")

    def test_active_run_blocks_dispatch(self):
        self.active = True
        self.assertEqual(self.tick(self.dt(19))[0], "run_active")
        self.assertEqual(self.dispatched, [])

    def test_throttle_and_cooldown(self):
        self.assertEqual(self.tick(self.dt(19), now_ts=1000)[0], "dispatched")
        self.assertEqual(self.tick(self.dt(19), now_ts=1100)[0], "throttled")      # < 300s
        self.assertEqual(self.tick(self.dt(19), now_ts=1400)[0], "cooldown")       # < 45 分鐘
        self.assertEqual(self.tick(self.dt(20), now_ts=1000 + 46 * 60)[0], "dispatched")
        self.assertEqual(len(self.dispatched), 2)

    def test_max_attempts_per_date(self):
        self.t.max_attempts_per_date = 2
        self.t.redispatch_interval_sec = 0
        self.t.check_interval_sec = 0
        for i in range(2):
            self.assertEqual(self.tick(self.dt(19), now_ts=1000 + i)[0], "dispatched")
        self.assertEqual(self.tick(self.dt(19), now_ts=1010)[0], "max_attempts")
        self.assertEqual(len(self.dispatched), 2)

    def test_marker_read_failure_still_dispatches(self):
        def boom():
            raise relay.GitHubApiError(500, "x")

        self.assertEqual(self.tick(self.dt(19), read_sent=boom)[0], "dispatched")

    def test_dispatch_failure_is_reported_not_swallowed(self):
        def boom():
            raise relay.GitHubApiError(403, "denied")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            action, _ = self.t.tick(1000, self.dt(19), read_sent=lambda: "", run_active=lambda: False, dispatch=boom)
        self.assertEqual(action, "dispatch_failed")
        self.assertIn("::error", buf.getvalue())

    def test_disabled(self):
        self.t.enabled = False
        self.assertEqual(self.tick(self.dt(19))[0], "disabled")

    def test_holding_after_bot_fetch_prevents_concurrent_session_use(self):
        self.t.hold_until_ts = 2000.0
        self.assertEqual(self.tick(self.dt(19), now_ts=1500)[0], "holding")
        self.assertEqual(self.dispatched, [])
        self.assertEqual(self.tick(self.dt(19), now_ts=2001)[0], "dispatched")


def runs_body(*runs):
    return json.dumps({"workflow_runs": [{"id": i, "status": st} for i, st in runs]}).encode()


def routed_opener(*, runs=None, get_error=None, post=None, telegram=None):
    """GET .../runs 回 runs；POST dispatches 回 post（預設 204）；Telegram 回 telegram（預設 200）。記錄所有請求。"""
    calls = []

    def opener(req, timeout=None):
        calls.append(req)
        url, method = req.full_url, req.get_method()
        if "api.telegram.org" in url:
            r = telegram if telegram is not None else FakeResp(200, b"{}")
        elif method == "GET":
            if get_error is not None:
                raise get_error
            r = FakeResp(200, runs_body(*(runs or [])))
        else:
            r = post if post is not None else FakeResp(204)
        if isinstance(r, Exception):
            raise r
        return r

    opener.calls = calls
    opener.posts = lambda: [c for c in calls if c.get_method() == "POST" and "dispatches" in c.full_url]
    opener.telegrams = lambda: [c for c in calls if "api.telegram.org" in c.full_url]
    return opener


class TestRecover(unittest.TestCase):
    ENV = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": FAKE_GH_TOKEN, "GITHUB_RUN_ID": "55", "BOT_RELAY_REF": "master"}
    ALERT = {"TELEGRAM_BOT_TOKEN": FAKE_BOT_TOKEN, "BOT_ALERT_CHAT_ID": "424242"}

    def run_recover(self, env, opener):
        sleeps = []
        code, out = capture(relay.cmd_recover, env, sleep=sleeps.append, opener=opener)
        return code, out, sleeps

    def test_first_failure_backs_off_60s_and_dispatches_with_incremented_count(self):
        op = routed_opener()
        code, out, sleeps = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": ""}, op)
        self.assertEqual(code, 0)
        self.assertEqual(sleeps[0], 60)
        body = json.loads(op.posts()[0].data)
        self.assertEqual(body["inputs"]["handoff_fail_count"], "1")
        self.assertEqual(body["inputs"]["handoff_prev_run_id"], "55")

    def test_backoff_grows_with_consecutive_failures(self):
        for count, delay in (("0", 60), ("1", 120), ("2", 240), ("3", 480)):
            op = routed_opener()
            _, _, sleeps = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": count}, op)
            self.assertEqual(sleeps[0], delay)
            self.assertEqual(json.loads(op.posts()[0].data)["inputs"]["handoff_fail_count"], str(int(count) + 1))

    def test_recover_sends_only_non_empty_inputs(self):
        op = routed_opener()
        self.run_recover({**self.ENV, "BOT_FAIL_COUNT": "0"}, op)
        inputs = json.loads(op.posts()[0].data)["inputs"]
        self.assertNotIn("handoff_min_update_id", inputs)
        self.assertEqual(set(inputs), {"handoff_prev_run_id", "handoff_fail_count"})

    def test_stops_after_max_consecutive_failures_and_alerts(self):
        op = routed_opener()
        code, out, sleeps = self.run_recover(
            {**self.ENV, **self.ALERT, "BOT_FAIL_COUNT": str(relay.MAX_CONSECUTIVE_START_FAILURES)}, op)
        self.assertEqual(code, 1)
        self.assertEqual(op.posts(), [])
        self.assertEqual(sleeps, [])                 # 到上限不再睡、不再打 API
        self.assertIn("::error", out)
        self.assertEqual(len(op.telegrams()), 1)     # 有送告警
        self.assertNotIn(FAKE_BOT_TOKEN, out)
        self.assertNotIn("424242", out)

    def test_cap_is_four_so_at_most_five_consecutive_runs(self):
        self.assertEqual(relay.MAX_CONSECUTIVE_START_FAILURES, 4)

    def test_dispatch_failure_returns_nonzero_with_error_and_alerts(self):
        op = routed_opener(post=http_error(403))
        code, out, _ = self.run_recover({**self.ENV, **self.ALERT, "BOT_FAIL_COUNT": "0"}, op)
        self.assertEqual(code, 1)
        self.assertIn("::error", out)
        self.assertNotIn(FAKE_GH_TOKEN, out)
        self.assertEqual(len(op.telegrams()), 1)

    def test_alert_failure_is_logged_not_raised(self):
        op = routed_opener(post=http_error(403), telegram=urllib.error.URLError("dns"))
        code, out, _ = self.run_recover({**self.ENV, **self.ALERT, "BOT_FAIL_COUNT": "0"}, op)
        self.assertEqual(code, 1)
        self.assertIn("Telegram 告警送出失敗", out)
        self.assertNotIn("424242", out)

    def test_no_alert_credentials_only_logs(self):
        op = routed_opener(post=http_error(403))
        code, out, _ = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": "0"}, op)
        self.assertEqual(code, 1)
        self.assertEqual(op.telegrams(), [])
        self.assertIn("告警只寫入 log", out)

    def test_dry_run_sends_nothing(self):
        op = routed_opener()
        code, _, _ = self.run_recover({**self.ENV, "BOT_RELAY_DRY_RUN": "1"}, op)
        self.assertEqual(code, 0)
        self.assertEqual(op.calls, [])

    # ── 取消 / 逾時路徑 ──
    def test_cancelled_job_still_recovers_with_short_backoff(self):
        for count, delay in (("", 15), ("1", 30), ("2", 45), ("3", 60)):
            op = routed_opener()
            code, out, sleeps = self.run_recover({**self.ENV, "BOT_JOB_STATUS": "cancelled", "BOT_FAIL_COUNT": count}, op)
            self.assertEqual(code, 0)
            self.assertEqual(sleeps[0], delay)
            self.assertEqual(len(op.posts()), 1)
            self.assertIn("被取消或逾時", out)

    def test_cancelled_counts_toward_cap(self):
        op = routed_opener()
        code, out, _ = self.run_recover({**self.ENV, **self.ALERT, "BOT_JOB_STATUS": "cancelled", "BOT_FAIL_COUNT": "4"}, op)
        self.assertEqual(code, 1)
        self.assertEqual(op.posts(), [])
        self.assertEqual(len(op.telegrams()), 1)

    # ── 已有後繼就不重複排 ──
    def test_existing_pending_successor_is_not_replaced(self):
        op = routed_opener(runs=[(55, "in_progress"), (56, "pending")])
        code, out, _ = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": "0"}, op)
        self.assertEqual(code, 0)
        self.assertEqual(op.posts(), [])
        self.assertIn("已有排隊中的後繼", out)

    def test_own_run_and_finished_runs_do_not_count_as_successor(self):
        op = routed_opener(runs=[(55, "queued"), (50, "completed"), (49, "in_progress")])
        code, _, _ = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": "0"}, op)
        self.assertEqual(code, 0)
        self.assertEqual(len(op.posts()), 1)

    def test_successor_lookup_failure_still_dispatches(self):
        op = routed_opener(get_error=http_error(500))
        code, out, _ = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": "0"}, op)
        self.assertEqual(code, 0)
        self.assertEqual(len(op.posts()), 1)
        self.assertIn("無法確認是否已有後繼 run", out)


class TestPendingSuccessor(unittest.TestCase):
    def test_statuses(self):
        for st, expected in (("pending", True), ("queued", True), ("waiting", True), ("in_progress", False), ("completed", False)):
            op = routed_opener(runs=[(9, st)])
            self.assertIs(relay.pending_successor_exists("o/r", FAKE_GH_TOKEN, "1", opener=op), expected, st)

    def test_queries_bot_yml_runs_endpoint(self):
        op = routed_opener(runs=[])
        relay.pending_successor_exists("o/r", FAKE_GH_TOKEN, "1", opener=op)
        self.assertIn("/repos/o/r/actions/workflows/bot.yml/runs", op.calls[0].full_url)


class TestPendingStatusCoverage(unittest.TestCase):
    """排隊中的三種狀態各自都要被認出來，否則自救會把使用者排好的 run 取代掉。"""

    def check(self, status):
        op = routed_opener(runs=[(9, status)])
        return relay.pending_successor_exists("o/r", FAKE_GH_TOKEN, "1", opener=op)

    def test_queued_counts(self):
        self.assertTrue(self.check("queued"))

    def test_waiting_counts(self):
        self.assertTrue(self.check("waiting"))

    def test_pending_counts(self):
        self.assertTrue(self.check("pending"))

    def test_status_set_contains_all_three(self):
        self.assertTrue({"queued", "waiting", "pending"} <= relay._PENDING_STATUSES)
        self.assertNotIn("in_progress", relay._PENDING_STATUSES)

    def test_query_has_no_status_filter_that_could_hide_pending_runs(self):
        op = routed_opener(runs=[])
        relay.pending_successor_exists("o/r", FAKE_GH_TOKEN, "1", opener=op)
        self.assertNotIn("status=", op.calls[0].full_url)


class TestDispatchRefIsDefaultBranch(unittest.TestCase):
    def test_default_ref_uses_relay_ref_only(self):
        self.assertEqual(relay.default_ref({"BOT_RELAY_REF": "master"}), "master")
        self.assertEqual(relay.default_ref({"BOT_RELAY_REF": "refs/heads/main"}), "main")

    def test_default_ref_ignores_temporary_github_ref_values(self):
        env = {"GITHUB_REF_NAME": "feature/tmp", "GITHUB_REF": "refs/heads/feature/tmp", "GITHUB_HEAD_REF": "feature/tmp"}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(relay.default_ref(env), "master")
        self.assertIn("退回 master", out.getvalue())          # 退回時有 log，不是靜默

    def test_recover_dispatches_on_default_branch_even_when_run_is_on_a_feature_branch(self):
        op = routed_opener()
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": FAKE_GH_TOKEN, "GITHUB_RUN_ID": "5",
               "BOT_RELAY_REF": "master", "GITHUB_REF_NAME": "feature/tmp", "GITHUB_REF": "refs/heads/feature/tmp"}
        code, _ = capture(relay.cmd_recover, env, sleep=lambda s: None, opener=op)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(op.posts()[0].data)["ref"], "master")

    def test_recover_without_relay_ref_falls_back_to_master_not_current_ref(self):
        op = routed_opener()
        env = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": FAKE_GH_TOKEN, "GITHUB_RUN_ID": "5", "GITHUB_REF_NAME": "feature/tmp"}
        capture(relay.cmd_recover, env, sleep=lambda s: None, opener=op)
        self.assertEqual(json.loads(op.posts()[0].data)["ref"], "master")


class TestResolveRuntime(unittest.TestCase):
    def resolve(self, **env):
        return relay.resolve_runtime(env)

    def test_default_is_300_minutes_with_relay_at_285(self):
        sec, lead, note = self.resolve()
        self.assertEqual((sec, lead), (300 * 60, 900))
        self.assertEqual((sec - lead) // 60, 285)
        self.assertIn("預設", note)

    def test_empty_input_is_default(self):
        self.assertEqual(self.resolve(BOT_MAX_RUNTIME_MINUTES="  ")[0], 300 * 60)

    def test_manual_15_and_20_minutes_scale_the_lead(self):
        sec, lead, note = self.resolve(BOT_MAX_RUNTIME_MINUTES="15")
        self.assertEqual((sec, lead), (900, 300))        # 第 10 分鐘就先排後繼
        self.assertIn("手動指定", note)
        sec, lead, _ = self.resolve(BOT_MAX_RUNTIME_MINUTES="20")
        self.assertEqual((sec, lead), (1200, 400))

    def test_out_of_range_is_clamped_with_warning(self):
        sec, _, note = self.resolve(BOT_MAX_RUNTIME_MINUTES="1")
        self.assertEqual(sec, 5 * 60)
        self.assertIn("⚠️", note)
        sec, _, note = self.resolve(BOT_MAX_RUNTIME_MINUTES="9999")
        self.assertEqual(sec, 300 * 60)
        self.assertIn("⚠️", note)
        sec, _, note = self.resolve(BOT_MAX_RUNTIME_MINUTES="-5")
        self.assertEqual(sec, 5 * 60)

    def test_garbage_falls_back_to_default_with_warning(self):
        sec, _, note = self.resolve(BOT_MAX_RUNTIME_MINUTES="abc")
        self.assertEqual(sec, 300 * 60)
        self.assertIn("不是整數", note)

    def test_minutes_takes_precedence_over_seconds_env(self):
        self.assertEqual(self.resolve(BOT_MAX_RUNTIME_MINUTES="15", BOT_MAX_RUNTIME_SEC="2")[0], 900)

    def test_seconds_env_still_works_for_tests(self):
        sec, lead, _ = self.resolve(BOT_MAX_RUNTIME_SEC="2", BOT_RELAY_LEAD_SEC="1")
        self.assertEqual((sec, lead), (2, 1))

    def test_lead_never_exceeds_a_third_of_runtime_nor_configured_value(self):
        self.assertEqual(self.resolve(BOT_MAX_RUNTIME_MINUTES="300", BOT_RELAY_LEAD_SEC="60")[1], 60)
        self.assertEqual(self.resolve(BOT_MAX_RUNTIME_MINUTES="5", BOT_RELAY_LEAD_SEC="900")[1], 100)


class TestTelegramAlert(unittest.TestCase):
    def test_posts_to_bot_api_without_logging_secrets(self):
        op = routed_opener()
        ok, out = capture(relay.send_telegram_alert, "壞了", {"TELEGRAM_BOT_TOKEN": FAKE_BOT_TOKEN, "BOT_ALERT_CHAT_ID": "424242"}, opener=op)
        self.assertTrue(ok)
        self.assertEqual(len(op.telegrams()), 1)
        self.assertIn("/sendMessage", op.calls[0].full_url)
        self.assertIn("壞了", json.loads(op.calls[0].data)["text"])
        self.assertNotIn(FAKE_BOT_TOKEN, out)
        self.assertNotIn("424242", out)

    def test_missing_config_makes_no_request(self):
        op = routed_opener()
        ok, out = capture(relay.send_telegram_alert, "x", {"TELEGRAM_BOT_TOKEN": FAKE_BOT_TOKEN}, opener=op)
        self.assertFalse(ok)
        self.assertEqual(op.calls, [])

    def test_http_error_and_non_2xx_return_false_and_log(self):
        for op in (routed_opener(telegram=http_error(400)), routed_opener(telegram=FakeResp(500))):
            ok, out = capture(relay.send_telegram_alert, "x", {"TELEGRAM_BOT_TOKEN": FAKE_BOT_TOKEN, "BOT_ALERT_CHAT_ID": "424242"}, opener=op)
            self.assertFalse(ok)
            self.assertIn("告警", out)
            self.assertNotIn(FAKE_BOT_TOKEN, out)


class TestDailyHoldCap(unittest.TestCase):
    def test_hold_extends_but_is_capped_per_window(self):
        t = relay.DailyTrigger()
        capture(t.hold, 1000.0, 120, 600)
        self.assertEqual(t.hold_until_ts, 1120.0)
        # 反覆下指令：每次延長，但從第一輪開始算 600 秒封頂
        for now in (1100.0, 1200.0, 1300.0, 1400.0, 1500.0, 1590.0):
            capture(t.hold, now, 120, 600)
        self.assertEqual(t.hold_until_ts, 1600.0)
        _, out = capture(t.hold, 1595.0, 120, 600)
        self.assertEqual(t.hold_until_ts, 1600.0)
        self.assertIn("上限", out)

    def test_released_at_cap_even_with_continuous_commands(self):
        t = relay.DailyTrigger(check_interval_sec=0)
        capture(t.hold, 1000.0, 120, 600)
        for now in (1100.0, 1200.0, 1300.0, 1400.0, 1500.0, 1590.0):
            capture(t.hold, now, 120, 600)
        dt = datetime(2026, 10, 7, 19, 0, tzinfo=TW)
        act, _ = t.tick(1599.0, dt, read_sent=lambda: "", run_active=lambda: False, dispatch=lambda: None)
        self.assertEqual(act, "holding")
        act, _ = t.tick(1601.0, dt, read_sent=lambda: "", run_active=lambda: False, dispatch=lambda: None)
        self.assertEqual(act, "dispatched")

    def test_new_window_after_expiry(self):
        t = relay.DailyTrigger()
        capture(t.hold, 1000.0, 120, 600)
        capture(t.hold, 5000.0, 120, 600)
        self.assertEqual(t.hold_until_ts, 5120.0)


if __name__ == "__main__":
    unittest.main()
