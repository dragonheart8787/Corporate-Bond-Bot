"""bot_listener 與 main 的測試：全部 mock，絕不會真的呼叫 Telegram / GitHub，也不會發送任何訊息。

執行：python -m unittest discover -s tests -t . -v
"""

import contextlib
import io
import os
import tempfile
import time
import unittest
from unittest import mock

import bot_listener as bl
import bot_relay as relay

CHAT_A = -5062625686
CHAT_B = 777000111
FAKE_BOT_TOKEN = "123456789:" + "AAFakeTokenFakeTokenFakeTokenFake12"
FAKE_GH_TOKEN = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
NOW = 1_800_000_000.0


def upd(uid, text, age, chat=CHAT_A, kind="message"):
    return {"update_id": uid, kind: {"text": text, "date": NOW - age, "chat": {"id": chat}}}


class TestClassifyPending(unittest.TestCase):
    def classify(self, items, **kw):
        return bl.classify_pending(items, NOW, **kw)

    def assert_reconciles(self, r):
        c = r.counts
        parts = ("fresh", "below_floor", "non_message", "non_command", "duplicate", "expired_notified", "expired_dropped")
        self.assertEqual(sum(c[k] for k in parts), c["total"], c)

    def test_fresh_command_is_executed_not_notified(self):
        r = self.classify([upd(1, "/cb", 120)])
        self.assertEqual([u["update_id"] for u in r.fresh], [1])
        self.assertEqual(r.expired, {})
        self.assert_reconciles(r)

    def test_expired_cb_is_not_executed_but_user_is_told(self):
        r = self.classify([upd(1, "/cb", 6 * 3600)])
        self.assertEqual(r.fresh, [])
        self.assertEqual(list(r.expired.values())[0].keys(), {"/cb"})
        self.assertEqual(r.counts["expired_notified"], 1)
        self.assert_reconciles(r)

    def test_expired_status_and_help_are_dropped_silently(self):
        r = self.classify([upd(1, "/status", 7200), upd(2, "/help", 7200)])
        self.assertEqual(r.expired, {})
        self.assertEqual(r.counts["expired_dropped"], 2)
        self.assert_reconciles(r)

    def test_beyond_telegram_retention_is_dropped(self):
        r = self.classify([upd(1, "/cb", 30 * 3600)])
        self.assertEqual(r.expired, {})
        self.assertEqual(r.counts["expired_dropped"], 1)

    def test_user_resent_after_expiry_means_no_stale_notice(self):
        r = self.classify([upd(1, "/cb", 5 * 3600), upd(2, "/cb", 60)])
        self.assertEqual([u["update_id"] for u in r.fresh], [2])
        self.assertEqual(r.expired, {})
        self.assert_reconciles(r)

    def test_same_chat_repeated_command_runs_once(self):
        r = self.classify([upd(1, "/cb", 100), upd(2, "/cb", 90), upd(3, "/cb", 80)])
        self.assertEqual(len(r.fresh), 1)
        self.assertEqual(r.counts["duplicate"], 2)
        self.assert_reconciles(r)

    def test_same_command_in_different_chats_each_runs(self):
        r = self.classify([upd(1, "/cb", 100, CHAT_A), upd(2, "/cb", 100, CHAT_B)])
        self.assertEqual(len(r.fresh), 2)

    def test_duplicate_expired_commands_collapse_to_one_notice_line(self):
        r = self.classify([upd(1, "/cb", 4 * 3600), upd(2, "/cb", 3 * 3600)])
        self.assertEqual(r.counts["expired_notified"], 1)
        self.assertEqual(r.counts["duplicate"], 1)
        self.assert_reconciles(r)

    def test_handoff_floor_skips_updates_predecessor_already_handled(self):
        r = self.classify([upd(5, "/cb", 30), upd(9, "/cb", 20, CHAT_B)], min_update_id=9)
        self.assertEqual([u["update_id"] for u in r.fresh], [9])
        self.assertEqual(r.counts["below_floor"], 1)
        self.assert_reconciles(r)

    def test_channel_post_is_recognised(self):
        r = self.classify([upd(1, "/cb", 30, kind="channel_post")])
        self.assertEqual(len(r.fresh), 1)

    def test_non_command_and_non_message_are_counted(self):
        r = self.classify([upd(1, "你好", 10), {"update_id": 2, "poll": {}}, upd(3, "/unknown", 10)])
        self.assertEqual(r.counts["non_command"], 2)
        self.assertEqual(r.counts["non_message"], 1)
        self.assert_reconciles(r)

    def test_command_with_bot_suffix_is_parsed(self):
        r = self.classify([upd(1, "/cb@my_bot", 30)])
        self.assertEqual(len(r.fresh), 1)


class TestExpiredNotice(unittest.TestCase):
    def test_notice_lists_taipei_time_and_command_and_asks_to_resend(self):
        # 1_800_000_000 = 2027-01-15 08:00:00 UTC = 16:00 台北
        text = bl.build_expired_notice({"/cb": 1_800_000_000.0}, gap_minutes=390)
        self.assertIn("01/15 16:00", text)
        self.assertIn("/cb", text)
        self.assertIn("390 分鐘", text)
        self.assertIn("重新發送", text)

    def test_notice_never_contains_chat_id(self):
        chat = str(CHAT_A)
        sent = []
        with mock.patch.object(bl, "send_message", lambda tok, c, t: sent.append((c, t)) or True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bl.send_expired_notices("T", {chat: {"/cb": NOW - 7200}}, 10)
        self.assertEqual(sent[0][0], chat)                  # 訊息送到正確的聊天室
        self.assertNotIn("5062625686", out.getvalue())      # 但 log 裡沒有 chat id
        self.assertNotIn("5062625686", sent[0][1])          # 訊息內文也沒有

    def test_notice_chat_cap(self):
        chats = {str(1000 + i): {"/cb": NOW - 7200} for i in range(bl.MAX_EXPIRED_NOTICE_CHATS + 3)}
        sent = []
        with mock.patch.object(bl, "send_message", lambda tok, c, t: sent.append(c) or True):
            with contextlib.redirect_stdout(io.StringIO()):
                n = bl.send_expired_notices("T", chats, None)
        self.assertEqual(n, bl.MAX_EXPIRED_NOTICE_CHATS)

    def test_failed_notice_is_logged(self):
        out = io.StringIO()
        with mock.patch.object(bl, "send_message", lambda *a: False), contextlib.redirect_stdout(out):
            n = bl.send_expired_notices("T", {str(CHAT_A): {"/cb": NOW - 7200}}, None)
        self.assertEqual(n, 0)
        self.assertIn("失敗", out.getvalue())


class FakeResp:
    def __init__(self, data=None, ok=True, status=200):
        self._data, self.ok, self.status_code = data or {"ok": True, "result": []}, ok, status

    def json(self):
        return self._data


class TestAckAndSend(unittest.TestCase):
    def test_ack_retries_then_succeeds(self):
        calls = {"n": 0}

        def fake_get(url, params=None, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError(f"net down {url}")
            return FakeResp()

        out = io.StringIO()
        with mock.patch.object(bl.requests, "get", fake_get), mock.patch.object(bl.time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(out):
            self.assertTrue(bl.ack_updates(FAKE_BOT_TOKEN, 100))
        self.assertEqual(calls["n"], 3)
        self.assertNotIn(FAKE_BOT_TOKEN, out.getvalue())     # 例外訊息帶著網址（含 token）也要被遮罩

    def test_ack_final_failure_is_loud(self):
        out = io.StringIO()
        with mock.patch.object(bl.requests, "get", lambda *a, **k: FakeResp({"ok": False, "description": "nope"})), \
                mock.patch.object(bl.time, "sleep", lambda s: None), contextlib.redirect_stdout(out):
            self.assertFalse(bl.ack_updates("T", 100))
        self.assertIn("最終失敗", out.getvalue())

    def test_send_message_failure_returns_false_and_logs_without_chat_id(self):
        out = io.StringIO()
        with mock.patch.object(bl.requests, "post", lambda *a, **k: FakeResp(ok=False, status=500)), \
                mock.patch.object(bl.time, "sleep", lambda s: None), contextlib.redirect_stdout(out):
            self.assertFalse(bl.send_message(FAKE_BOT_TOKEN, str(CHAT_A), "hi"))
        log = out.getvalue()
        self.assertIn("最終送出失敗", log)
        self.assertNotIn("5062625686", log)
        self.assertNotIn(FAKE_BOT_TOKEN, log)

    def test_send_message_exception_is_redacted(self):
        def boom(url, **k):
            raise OSError(f"Max retries exceeded with url: {url}")

        out = io.StringIO()
        with mock.patch.object(bl.requests, "post", boom), mock.patch.object(bl.time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(out):
            bl.send_message(FAKE_BOT_TOKEN, str(CHAT_A), "hi")
        self.assertNotIn(FAKE_BOT_TOKEN, out.getvalue())


class TestWaitForDailyIdle(unittest.TestCase):
    """抓取前等每日報告跑完，避免同一組 Telegram session 同時從兩台機器使用。"""

    def run_wait(self, active_seq, token=FAKE_GH_TOKEN, max_wait=30, env=None):
        seq = list(active_seq)
        calls, sleeps = {"n": 0}, []

        def fake_active(repo, tok):
            calls["n"] += 1
            item = seq.pop(0) if len(seq) > 1 else seq[0]
            if isinstance(item, Exception):
                raise item
            return item

        out = io.StringIO()
        with mock.patch.object(relay, "daily_run_active", fake_active), \
                mock.patch.object(bl.time, "sleep", sleeps.append), \
                mock.patch.dict(os.environ, env or {}), contextlib.redirect_stdout(out):
            result = bl.wait_for_daily_idle("o/r", token, max_wait=max_wait, poll=10)
        return result, calls["n"], sleeps, out.getvalue()

    def test_idle_returns_immediately_without_sleeping(self):
        ok, n, sleeps, _ = self.run_wait([False])
        self.assertTrue(ok)
        self.assertEqual((n, sleeps), (1, []))

    def test_waits_while_daily_is_running_then_proceeds(self):
        ok, n, sleeps, out = self.run_wait([True, True, False])
        self.assertTrue(ok)
        self.assertEqual(sleeps, [10, 10])
        self.assertIn("每日報告正在執行", out)

    def test_timeout_is_fail_closed_returns_false_with_log(self):
        ok, n, sleeps, out = self.run_wait([True], max_wait=30)
        self.assertFalse(ok)                       # False = 不可抓取
        self.assertEqual(sum(sleeps), 30)
        self.assertIn("略過即時抓取", out)

    def test_default_max_wait_is_120_seconds(self):
        self.assertEqual(bl.DAILY_IDLE_MAX_WAIT_SEC, 120)

    def test_persistent_api_error_is_fail_closed_after_retries(self):
        ok, n, sleeps, out = self.run_wait([relay.GitHubApiError(500, "down")])
        self.assertFalse(ok)
        self.assertEqual(n, bl.DAILY_IDLE_API_RETRIES)
        self.assertEqual(sleeps, [bl.DAILY_IDLE_API_RETRY_SEC] * (bl.DAILY_IDLE_API_RETRIES - 1))
        self.assertIn("無法確認每日報告", out)
        self.assertIn("略過即時抓取", out)

    def test_transient_api_error_then_idle_proceeds(self):
        ok, n, _, out = self.run_wait([relay.GitHubApiError(502, "blip"), False])
        self.assertTrue(ok)
        self.assertEqual(n, 2)
        self.assertIn("無法確認每日報告", out)

    def test_no_token_or_daily_trigger_disabled_skips_the_check(self):
        self.assertEqual(self.run_wait([True], token="")[:2], (True, 0))
        self.assertEqual(self.run_wait([True], env={"BOT_DAILY_TRIGGER": "0"})[:2], (True, 0))


class MainHarness(unittest.TestCase):
    """用腳本化的 Telegram 更新把整個 main() 跑一輪（約 1 秒）。"""

    def run_main(self, script, env_extra=None, relay_dispatch=None, prev_info=None):
        """
        script：getUpdates 依序回傳的批次（第一批供「啟動取回積壓」使用）。
        回傳 (exit_code, stdout, handled 指令清單, 送出的訊息, relay.dispatch 呼叫)。
        """
        env = {
            "TELEGRAM_BOT_TOKEN": FAKE_BOT_TOKEN,
            "GITHUB_TOKEN": FAKE_GH_TOKEN,
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_RUN_ID": "900",
            "BOT_MAX_RUNTIME_SEC": "2",
            "BOT_RELAY_LEAD_SEC": "1",      # 第 1 秒起接力
            "BOT_RELAY_RETRY_SEC": "0.2",
            "BOT_DAILY_TRIGGER": "0",
            "BOT_ALERT_CHAT_ID": "424242",
        }
        env.update(env_extra or {})
        batches = [list(b) for b in script]
        handled, sent, dispatches = [], [], []

        def fake_get(url, params=None, timeout=None):
            if "getUpdates" in url:
                return FakeResp({"ok": True, "result": batches.pop(0) if batches else []})
            raise AssertionError(url)

        def fake_dispatch(repo, wf, ref, inputs, token, **kw):
            dispatches.append((wf, ref, dict(inputs)))
            if relay_dispatch:
                relay_dispatch(len(dispatches))

        real_sleep = time.sleep
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(bl.requests, "get", fake_get), \
                mock.patch.object(bl, "handle_report_command", lambda tok, chat, gt, gr, mode: handled.append((str(chat), mode))), \
                mock.patch.object(bl, "send_message", lambda tok, chat, text: sent.append((str(chat), text)) or True), \
                mock.patch.object(bl, "get_report_updated_time", lambda *a: "測試"), \
                mock.patch.object(relay, "dispatch_workflow", fake_dispatch), \
                mock.patch.object(relay, "fetch_prev_run_info", lambda *a, **k: prev_info), \
                mock.patch.object(bl, "IDLE_SLEEP_SEC", 0), \
                mock.patch.object(bl.time, "sleep", lambda s: real_sleep(min(s, 0.02))), \
                contextlib.redirect_stdout(out):
            code = bl.main()
        return code, out.getvalue(), handled, sent, dispatches


class TestMainRelay(MainHarness):
    def test_relay_triggers_next_run_once_with_handoff_inputs(self):
        code, out, _, _, dispatches = self.run_main([[]])
        self.assertEqual(code, 0)
        self.assertEqual(len(dispatches), 1)
        wf, ref, inputs = dispatches[0]
        self.assertEqual(wf, "bot.yml")
        self.assertEqual(inputs["handoff_prev_run_id"], "900")
        self.assertEqual(inputs["handoff_fail_count"], "0")
        self.assertIn("已觸發下一棒", out)

    def test_relay_failure_retries_then_succeeds(self):
        def fail_first(n):
            if n == 1:
                raise relay.GitHubApiError(403, "denied")

        code, out, _, _, dispatches = self.run_main([[]], relay_dispatch=fail_first)
        self.assertEqual(code, 0)
        self.assertEqual(len(dispatches), 2)
        self.assertIn("接力觸發失敗", out)
        self.assertIn("已觸發下一棒", out)

    def test_relay_permanent_failure_exits_nonzero_alerts_and_logs_error(self):
        def always_fail(n):
            raise relay.GitHubApiError(403, "Resource not accessible by integration")

        code, out, _, sent, dispatches = self.run_main([[]], relay_dispatch=always_fail)
        self.assertEqual(code, 1)                              # run 變紅，需要被看見
        self.assertGreaterEqual(len(dispatches), 2)
        self.assertIn("::error title=bot 接力失敗", out)
        alerts = [t for c, t in sent if c == "424242"]
        self.assertTrue(alerts and "接力" in alerts[0])        # 有送告警
        self.assertIn("以失敗狀態退出", out)

    def test_relay_failure_without_alert_chat_only_logs(self):
        def always_fail(n):
            raise relay.GitHubApiError(500, "x")

        code, out, _, sent, _ = self.run_main([[]], env_extra={"BOT_ALERT_CHAT_ID": ""}, relay_dispatch=always_fail)
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertIn("只寫入 log", out)

    def test_relay_can_be_disabled(self):
        code, out, _, _, dispatches = self.run_main([[]], env_extra={"BOT_RELAY_ENABLED": "0"})
        self.assertEqual(code, 0)
        self.assertEqual(dispatches, [])


class TestDispatchRef(MainHarness):
    def test_relay_dispatches_on_default_branch_not_the_current_github_ref(self):
        env = {"BOT_RELAY_REF": "master", "GITHUB_REF_NAME": "feature/tmp", "GITHUB_REF": "refs/heads/feature/tmp"}
        _, _, _, _, dispatches = self.run_main([[]], env_extra=env)
        self.assertTrue(dispatches)
        self.assertEqual({ref for _, ref, _ in dispatches}, {"master"})

    def test_missing_relay_ref_falls_back_to_master_never_to_github_ref(self):
        saved = os.environ.pop("BOT_RELAY_REF", None)
        try:
            _, out, _, _, dispatches = self.run_main([[]], env_extra={"GITHUB_REF_NAME": "feature/tmp"})
        finally:
            if saved is not None:
                os.environ["BOT_RELAY_REF"] = saved
        self.assertEqual({ref for _, ref, _ in dispatches}, {"master"})
        self.assertIn("退回 master", out)

    def test_daily_trigger_also_uses_default_branch(self):
        import datetime as _dt
        env = {"BOT_DAILY_TRIGGER": "1", "BOT_RELAY_ENABLED": "0", "BOT_RELAY_REF": "master", "GITHUB_REF_NAME": "feature/tmp"}
        with mock.patch.object(relay, "read_last_sent", lambda *a, **k: ""), \
                mock.patch.object(relay, "daily_run_active", lambda *a, **k: False), \
                mock.patch.object(bl, "datetime", wraps=_dt.datetime) as dtm:
            dtm.now.return_value = _dt.datetime(2026, 10, 7, 19, 0, tzinfo=bl.TW_ZONE)
            _, _, _, _, dispatches = self.run_main([[]], env_extra=env)
        daily = [d for d in dispatches if d[0] == "daily.yml"]
        self.assertTrue(daily)
        self.assertEqual({ref for _, ref, _ in daily}, {"master"})


class TestMaxRuntimeInput(MainHarness):
    def test_startup_log_states_runtime_source_and_relay_lead(self):
        _, out, _, _, dispatches = self.run_main([[]])
        self.assertIn("運行時間：0 分鐘", out)          # 測試用 2 秒
        self.assertIn("BOT_MAX_RUNTIME_SEC=2", out)
        self.assertIn("接力提前量 1 秒", out)
        self.assertEqual(len(dispatches), 1)

    def test_minutes_input_wins_over_seconds_and_is_reported(self):
        # resolve_runtime 的細節在 test_bot_relay；這裡確認 main() 真的用它（15 分鐘 → 接力點第 10 分鐘）
        seen = {}
        real = relay.resolve_runtime

        def spy(env=None):
            res = real(env)
            seen["res"] = res
            return 2, 1, res[2]      # 實際只跑 2 秒，其餘照真實解析結果

        with mock.patch.object(relay, "resolve_runtime", spy):
            _, out, _, _, _ = self.run_main([[]], env_extra={"BOT_MAX_RUNTIME_MINUTES": "15", "BOT_RELAY_LEAD_SEC": "900"})
        self.assertEqual(seen["res"][:2], (900, 300))
        self.assertIn("max_runtime_minutes=15（手動指定）", out)


class TestReportCommandFailClosed(unittest.TestCase):
    """日報在跑（或查不到）時，/cb 不可去抓；要明確告知使用者並改讀已存檔報告。"""

    def run_cmd(self, idle):
        sent, fetched = [], []
        with mock.patch.object(bl, "wait_for_daily_idle", lambda *a, **k: idle), \
                mock.patch.object(bl, "can_run_fetch", lambda: True), \
                mock.patch.object(bl, "run_fetch_subprocess", lambda: fetched.append(1) or 0), \
                mock.patch.object(bl, "send_message", lambda tok, chat, text: sent.append(text) or True), \
                mock.patch.object(bl, "load_report_text", lambda *a: ("📊 測試報告 轉換公司債", "github")), \
                mock.patch.object(bl, "staleness_note", lambda *a: ""), \
                mock.patch.object(bl, "PRE_FETCH_DELAY_SEC", 0), mock.patch.object(bl, "POST_FETCH_DELAY_SEC", 0), \
                mock.patch.object(bl.time, "sleep", lambda s: None), contextlib.redirect_stdout(io.StringIO()):
            bl.handle_report_command(FAKE_BOT_TOKEN, "1", FAKE_GH_TOKEN, "o/r", "all")
        return sent, fetched

    def test_busy_skips_fetch_tells_user_and_still_replies_with_stored_report(self):
        sent, fetched = self.run_cmd(idle=False)
        self.assertEqual(fetched, [])
        self.assertTrue(any("這次不即時抓取" in t for t in sent))
        self.assertTrue(any("測試報告" in t for t in sent))

    def test_idle_fetches(self):
        sent, fetched = self.run_cmd(idle=True)
        self.assertEqual(fetched, [1])
        self.assertFalse(any("這次不即時抓取" in t for t in sent))


class TestMainDedupeAndStartup(MainHarness):
    def test_same_update_id_delivered_twice_is_handled_once(self):
        # 第 1 批 = 啟動取回（空）；第 2、3 批 = 同一個 update_id 重複出現
        code, out, handled, _, _ = self.run_main(
            [[], [upd(7, "/cb", 0)], [upd(7, "/cb", 0)]], env_extra={"BOT_RELAY_ENABLED": "0"}
        )
        self.assertEqual(handled, [(str(CHAT_A), "cb")])
        self.assertIn("略過已處理過的 update_id=7", out)

    def test_updates_below_handoff_floor_are_skipped(self):
        code, out, handled, _, _ = self.run_main(
            [[], [upd(3, "/cb", 0), upd(12, "/all", 0)]],
            env_extra={"BOT_RELAY_ENABLED": "0", "BOT_MIN_UPDATE_ID": "10"},
        )
        self.assertEqual(handled, [(str(CHAT_A), "all")])
        self.assertIn("交接水位線", out)

    def test_startup_executes_fresh_notifies_expired_and_logs_counts(self):
        backlog = [
            upd(1, "/cb", 6 * 3600, CHAT_B),     # 過期 → 通知、不執行
            upd(2, "/cb", 60, CHAT_A),           # 夠新 → 執行
            upd(3, "你好", 30, CHAT_A),           # 非指令
        ]
        # 啟動時 now 是真實時間，update 的 date 要相對於真實時間
        t = time.time()
        for u in backlog:
            msg = u["message"]
            msg["date"] = t - (NOW - msg["date"])
        code, out, handled, sent, _ = self.run_main([backlog], env_extra={"BOT_RELAY_ENABLED": "0"})
        self.assertEqual(handled, [(str(CHAT_A), "cb")])
        notices = [(c, tx) for c, tx in sent if c == str(CHAT_B)]
        self.assertEqual(len(notices), 1)
        self.assertIn("重新發送", notices[0][1])
        self.assertIn("補執行 1", out)
        self.assertIn("過期已通知 1", out)
        self.assertIn("非指令 1", out)

    def test_startup_log_has_run_info_and_final_tally(self):
        code, out, *_ = self.run_main([[]], env_extra={"BOT_RELAY_ENABLED": "0"})
        self.assertIn("run=900", out)
        self.assertIn("本棒結算", out)

    def test_gap_between_previous_and_this_run_is_logged(self):
        from datetime import datetime, timedelta, timezone

        ended = datetime.now(timezone.utc) - timedelta(minutes=390)
        prev = {"run_id": 899, "ended_at": ended, "conclusion": "failure"}
        code, out, *_ = self.run_main([[]], env_extra={"BOT_RELAY_ENABLED": "0"}, prev_info=prev)
        self.assertIn("上一棒 run=899（failure）結束於", out)
        self.assertIn("空窗 390.", out)
        self.assertIn("空窗超過 5 分鐘", out)

    def test_short_gap_does_not_warn(self):
        from datetime import datetime, timedelta, timezone

        prev = {"run_id": 899, "ended_at": datetime.now(timezone.utc) - timedelta(seconds=40), "conclusion": "success"}
        code, out, *_ = self.run_main([[]], env_extra={"BOT_RELAY_ENABLED": "0"}, prev_info=prev)
        self.assertIn("空窗 0.", out)
        self.assertNotIn("空窗超過 5 分鐘", out)


class TestNoSecretsInLogs(MainHarness):
    def test_full_run_never_prints_tokens_or_chat_ids(self):
        t = time.time()
        backlog = [upd(1, "/cb", 6 * 3600, CHAT_B), upd(2, "/cb", 30, CHAT_A), upd(3, "/status", 10, CHAT_A)]
        for u in backlog:
            u["message"]["date"] = t - (NOW - u["message"]["date"])

        def fail(n):
            raise relay.GitHubApiError(None, f"網路錯誤：Max retries exceeded url=/bot{FAKE_BOT_TOKEN}/x token={FAKE_GH_TOKEN}")

        code, out, *_ = self.run_main([backlog], relay_dispatch=fail)
        for secret in (FAKE_BOT_TOKEN, FAKE_GH_TOKEN, "5062625686", "777000111", "424242"):
            self.assertNotIn(secret, out, f"log 洩漏了 {secret!r}")


class TestMainPyHardening(unittest.TestCase):
    def test_missing_chat_id_path_does_not_print_bot_token_and_records_failure(self):
        import main as m

        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                env = {"TELEGRAM_BOT_TOKEN": FAKE_BOT_TOKEN, "TELEGRAM_CHAT_ID": "", "REPORT_DATE": "20261007"}
                out = io.StringIO()
                with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
                    with self.assertRaises(SystemExit):
                        m.mode_send()
                self.assertNotIn(FAKE_BOT_TOKEN, out.getvalue())
                with open(os.path.join("outputs", "daily", "send_result.txt"), encoding="utf-8") as f:
                    self.assertEqual(f.read().strip(), "20261007 failed")
            finally:
                os.chdir(cwd)

    def test_already_sent_is_recorded_as_skipped(self):
        import main as m

        with tempfile.TemporaryDirectory() as d:
            cwd = os.getcwd()
            os.chdir(d)
            try:
                os.makedirs("reports")
                with open(m.SENT_MARKER, "w", encoding="utf-8") as f:
                    f.write("20261007\n")
                env = {"SKIP_IF_ALREADY_SENT": "true", "REPORT_DATE": "20261007"}
                with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                    m.mode_send()
                with open(os.path.join("outputs", "daily", "send_result.txt"), encoding="utf-8") as f:
                    self.assertEqual(f.read().strip(), "20261007 skipped_already_sent")
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
