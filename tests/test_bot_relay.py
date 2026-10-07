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


class TestRecover(unittest.TestCase):
    ENV = {"GITHUB_REPOSITORY": "o/r", "GITHUB_TOKEN": FAKE_GH_TOKEN, "GITHUB_RUN_ID": "55", "BOT_RELAY_REF": "master"}

    def run_recover(self, env, opener):
        sleeps = []
        code, out = capture(relay.cmd_recover, env, sleep=sleeps.append, opener=opener)
        return code, out, sleeps

    def test_first_failure_backs_off_60s_and_dispatches_with_incremented_count(self):
        op = make_opener(FakeResp(204))
        code, out, sleeps = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": ""}, op)
        self.assertEqual(code, 0)
        self.assertEqual(sleeps[0], 60)
        body = json.loads(op.calls[0].data)
        self.assertEqual(body["inputs"]["handoff_fail_count"], "1")
        self.assertEqual(body["inputs"]["handoff_prev_run_id"], "55")

    def test_backoff_grows_with_consecutive_failures(self):
        op = make_opener(FakeResp(204))
        _, _, sleeps = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": "2"}, op)
        self.assertEqual(sleeps[0], 240)
        self.assertEqual(json.loads(op.calls[0].data)["inputs"]["handoff_fail_count"], "3")

    def test_recover_sends_only_non_empty_inputs(self):
        op = make_opener(FakeResp(204))
        self.run_recover({**self.ENV, "BOT_FAIL_COUNT": "0"}, op)
        inputs = json.loads(op.calls[0].data)["inputs"]
        self.assertNotIn("handoff_min_update_id", inputs)
        self.assertEqual(set(inputs), {"handoff_prev_run_id", "handoff_fail_count"})

    def test_stops_after_max_consecutive_failures(self):
        op = make_opener(FakeResp(204))
        code, out, sleeps = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": str(relay.MAX_CONSECUTIVE_START_FAILURES)}, op)
        self.assertEqual(code, 1)
        self.assertEqual(op.calls, [])
        self.assertIn("::error", out)

    def test_dispatch_failure_returns_nonzero_with_error(self):
        code, out, _ = self.run_recover({**self.ENV, "BOT_FAIL_COUNT": "0"}, make_opener(http_error(403)))
        self.assertEqual(code, 1)
        self.assertIn("::error", out)
        self.assertNotIn(FAKE_GH_TOKEN, out)

    def test_dry_run_sends_nothing(self):
        op = make_opener(FakeResp(204))
        code, _, _ = self.run_recover({**self.ENV, "BOT_RELAY_DRY_RUN": "1"}, op)
        self.assertEqual(code, 0)
        self.assertEqual(op.calls, [])


if __name__ == "__main__":
    unittest.main()
