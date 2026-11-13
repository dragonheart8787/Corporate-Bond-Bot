"""archive-fsc 的「管道測試」（send_test）：workflow 條件的模擬、fsc_alert.run_pipe_test 的送訊與各失敗路徑、回溯排除。
全部離線：Telegram 與 GitHub API 都是假的物件；socket 在本模組內被封鎖；不寫檔、不 commit。"""

import builtins
import contextlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fsc_alert as fa  # noqa: E402
from test_fsc_alert import FakeApi, GH, hist, run as api_run  # noqa: E402

_real_socket = socket.socket


def setUpModule():
    def blocked(*a, **k):
        raise AssertionError("測試不得建立真的網路連線")
    socket.socket = blocked  # type: ignore[assignment]


def tearDownModule():
    socket.socket = _real_socket  # type: ignore[assignment]


TOKEN = "123456789:AAFAKE-token-value-for-tests"
CHAT = "-1001234567890"
WF = os.path.join(ROOT, ".github", "workflows", "archive-fsc.yml")


# ─────────────────────────────────────────
# workflow 條件模擬（把 if／env／run-name 的 ${{ }} 表達式翻成 Python 求值；validate 步驟用真的 bash 執行）
# ─────────────────────────────────────────
class Ctx:
    def __init__(self, event_name, inputs, outcomes=None, job_ok=True, ref="refs/heads/master"):
        self.event_name, self.inputs, self.outcomes, self.job_ok, self.ref = event_name, dict(inputs), dict(outcomes or {}), job_ok, ref

    def secrets(self, name):
        return {"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": CHAT}.get(name, "")


def ev_expr(expr, ctx):
    e = expr.strip()
    e = e.replace("!=", " __NE__ ")
    e = re.sub(r"github\.event\.inputs\.(\w+)", lambda m: f"ctx.inputs.get('{m.group(1)}', '')", e)
    e = e.replace("github.event_name", "ctx.event_name")
    e = re.sub(r"github\.ref\b(?!_)", "ctx.ref", e)
    e = re.sub(r"steps\.(\w+)\.outcome", lambda m: f"ctx.outcomes.get('{m.group(1)}', 'skipped')", e)
    e = re.sub(r"secrets\.(\w+)", lambda m: f"ctx.secrets('{m.group(1)}')", e)
    e = e.replace("github.token", "'ghs-token'").replace("runner.temp", "'/tmp'").replace("github.repository", "'o/r'")
    e = e.replace("always()", "True").replace("cancelled()", "False").replace("success()", "ctx.job_ok")
    e = e.replace("&&", " and ").replace("||", " or ")
    e = re.sub(r"!(?!=)", " not ", e)
    e = e.replace(" __NE__ ", " != ")
    return eval(e, {"ctx": ctx})


def tostr(v):
    return {True: "true", False: "false", None: ""}.get(v, v) if isinstance(v, (bool, type(None))) else str(v)


def render(template, ctx):
    if not isinstance(template, str):
        return template
    whole = re.fullmatch(r"\$\{\{(.*)\}\}", template.strip(), re.S)
    if whole and "${{" not in whole.group(1):
        return ev_expr(whole.group(1), ctx)
    return re.sub(r"\$\{\{(.*?)\}\}", lambda m: tostr(ev_expr(m.group(1), ctx)), template)


STATUS_FN = ("always()", "cancelled()", "failure()", "success()")


@unittest.skipIf(yaml is None, "需要 PyYAML")
class WorkflowSim(unittest.TestCase):
    def setUp(self):
        self.text = open(WF, encoding="utf-8").read()
        self.wf = yaml.safe_load(self.text)
        self.steps = self.wf["jobs"]["archive"]["steps"]

    def sid(self, s):
        return s.get("id") or ("checkout" if "uses" in s else "gate" if "失敗閘門" in s["name"] else s["name"])

    def simulate(self, event, inputs, results=None, ref="refs/heads/master"):
        """回傳 {步驟 id: ran}、{步驟 id: outcome}、ctx。results：步驟 id → 'failure' 以覆寫預設的成功。"""
        results = results or {}
        ctx = Ctx(event, inputs, ref=ref)
        ran, outcome = {}, {}
        for st in self.steps:
            i = self.sid(st)
            cond = st.get("if")
            if cond is None:
                go = ctx.job_ok
            else:
                val = render(cond, ctx)
                go = bool(val) if any(f in cond for f in STATUS_FN) else (ctx.job_ok and bool(val))
            ran[i] = go
            if not go:
                outcome[i] = "skipped"
            else:
                if st.get("id") == "validate":
                    env = {k: render(v, ctx) for k, v in st["env"].items()}
                    r = subprocess.run(["bash", "-e", "-c", st["run"]], env={**os.environ, **{k: str(v) for k, v in env.items()}}, capture_output=True, text=True)
                    outcome[i] = "success" if r.returncode == 0 else "failure"
                    st_out = r.stdout
                    self.last_validate_out = st_out
                else:
                    outcome[i] = results.get(i, "success")
                if outcome[i] == "failure":
                    coe = render(st.get("continue-on-error", False), ctx)
                    if not (coe is True or coe == "true"):
                        ctx.job_ok = False
            ctx.outcomes[i] = outcome[i]
        return ran, outcome, ctx


class TestSendTestWorkflowBehaviour(WorkflowSim):
    def notify(self):
        return [s for s in self.steps if s.get("id") == "notify"][0]

    def test_valid_send_test_runs_only_validate_checkout_and_notify(self):
        ran, out, ctx = self.simulate("workflow_dispatch", {"dry_run": "false", "test_alert": "false", "send_test": "true"})
        self.assertEqual([i for i, r in ran.items() if r], ["validate", "checkout", "notify"])
        for skipped in ("archive", "commit"):
            self.assertFalse(ran[skipped])
        self.assertFalse(ran["gate"])                                                        # 失敗閘門
        env = {k: render(v, ctx) for k, v in self.notify()["env"].items()}
        self.assertEqual(env["SEND_TEST"], "1")
        self.assertEqual(env["ALERT_EVENT_NAME"], "workflow_dispatch")
        self.assertEqual((env["ARCHIVE_DRY_RUN"], env["ALERT_TEST"]), ("0", "0"))
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], TOKEN)                                   # 真實送訊需要 secrets
        self.assertEqual(env["BOT_ALERT_CHAT_ID"], CHAT)
        self.assertIs(render(self.notify()["continue-on-error"], ctx), False)                # 送訊失敗要讓 run 變紅

    def test_send_test_failure_makes_the_run_red(self):
        ran, out, ctx = self.simulate("workflow_dispatch", {"dry_run": "false", "test_alert": "false", "send_test": "true"}, results={"notify": "failure"})
        self.assertFalse(ctx.job_ok)

    def test_illegal_combinations_fail_in_the_first_step_and_nothing_else_runs(self):
        for inputs in ({"dry_run": "true", "test_alert": "false", "send_test": "true"},
                       {"dry_run": "false", "test_alert": "true", "send_test": "true"},
                       {"dry_run": "true", "test_alert": "true", "send_test": "true"}):
            with self.subTest(inputs=inputs):
                ran, out, ctx = self.simulate("workflow_dispatch", inputs)
                self.assertEqual(self.steps[0]["id"], "validate")                             # 最早的步驟，早於 checkout
                self.assertEqual(out["validate"], "failure")
                self.assertIn("::error title=send_test 輸入不合法::", self.last_validate_out)
                self.assertEqual([i for i, r in ran.items() if r], ["validate"])               # checkout、抓取、通知（含送訊）全都不執行
                self.assertFalse(ctx.job_ok)

    def test_send_test_from_a_non_dispatch_event_is_rejected_and_never_sends(self):
        ran, out, ctx = self.simulate("schedule", {"send_test": "true"})                     # 理論上不可能，仍要擋
        self.assertEqual(out["validate"], "failure")
        self.assertEqual([i for i, r in ran.items() if r], ["validate"])
        # 就算 validate 被繞過，SEND_TEST 的 env 表達式對 schedule 也一定是 '0'
        env = {k: render(v, Ctx("schedule", {"send_test": "true"})) for k, v in self.notify()["env"].items()}
        self.assertEqual(env["SEND_TEST"], "0")

    def test_send_test_on_a_non_master_ref_fails_in_validate_and_never_sends(self):
        legal = {"dry_run": "false", "test_alert": "false", "send_test": "true"}
        for ref in ("refs/heads/claude/archive-fsc-send-test", "refs/heads/main", "refs/heads/master2", "refs/tags/master", "refs/pull/1/merge", ""):
            with self.subTest(ref=ref):
                ran, out, ctx = self.simulate("workflow_dispatch", legal, ref=ref)
                self.assertEqual(out["validate"], "failure")
                self.assertIn("只能在 master 分支上執行", self.last_validate_out)
                self.assertEqual([i for i, r in ran.items() if r], ["validate"])             # checkout、抓取、通知（含送訊）都不執行
                self.assertFalse(ctx.job_ok)
                # 雙重檢查：就算 validate 被繞過，通知步驟的 SEND_TEST 對非 master 也一定是 '0'
                env = {k: render(v, Ctx("workflow_dispatch", legal, ref=ref)) for k, v in self.notify()["env"].items()}
                self.assertEqual(env["SEND_TEST"], "0")
        ran, out, ctx = self.simulate("workflow_dispatch", legal, ref="refs/heads/master")   # 對照：master 合法
        self.assertEqual(out["validate"], "success")
        self.assertTrue(ran["notify"])

    def test_non_master_dispatch_without_send_test_still_runs_the_normal_flow(self):
        ran, out, ctx = self.simulate("workflow_dispatch", {"dry_run": "false", "test_alert": "true", "send_test": "false"}, ref="refs/heads/claude/x")
        self.assertEqual(out["validate"], "success")
        self.assertTrue(ran["archive"])
        env = {k: render(v, ctx) for k, v in self.notify()["env"].items()}
        self.assertEqual(env["SEND_TEST"], "0")

    def test_schedule_run_cannot_send_a_test_and_runs_the_normal_flow(self):
        ran, out, ctx = self.simulate("schedule", {})
        self.assertEqual([i for i, r in ran.items() if r], ["validate", "checkout", "archive", "commit", "notify"])
        env = {k: render(v, ctx) for k, v in self.notify()["env"].items()}
        self.assertEqual(env["SEND_TEST"], "0")
        self.assertEqual(env["ALERT_EVENT_NAME"], "schedule")
        self.assertIs(render(self.notify()["continue-on-error"], ctx), True)                 # 一般通知永遠不影響結果

    def test_dispatch_without_send_test_is_the_normal_flow(self):
        for inputs in ({"dry_run": "false", "test_alert": "false", "send_test": "false"}, {"dry_run": "false", "test_alert": "true", "send_test": "false"}):
            ran, out, ctx = self.simulate("workflow_dispatch", inputs)
            self.assertTrue(ran["archive"])
            env = {k: render(v, ctx) for k, v in self.notify()["env"].items()}
            self.assertEqual(env["SEND_TEST"], "0")

    def test_dry_run_still_skips_the_notify_step(self):
        ran, out, ctx = self.simulate("workflow_dispatch", {"dry_run": "true", "test_alert": "false", "send_test": "false"})
        self.assertFalse(ran["notify"])

    def test_normal_archive_failure_still_reaches_notify_and_the_red_gate(self):
        ran, out, ctx = self.simulate("schedule", {}, results={"archive": "failure"})
        self.assertTrue(ran["notify"])
        self.assertTrue(ran["gate"])                                                         # 失敗閘門照跑（exit 1）

    def test_run_name_markers(self):
        rn = self.wf["run-name"]
        base = "留存證期局申報案件彙總表"
        self.assertEqual(render(rn, Ctx("schedule", {})), base)
        self.assertEqual(render(rn, Ctx("workflow_dispatch", {"dry_run": "true", "send_test": "false"})), base + "（dry-run）")
        self.assertEqual(render(rn, Ctx("workflow_dispatch", {"dry_run": "false", "send_test": "true"})), base + "（管道測試）")
        self.assertIn(fa.SEND_TEST_MARK, render(rn, Ctx("workflow_dispatch", {"send_test": "true"})))

    def test_inputs_and_permissions_and_secret_placement(self):
        inputs = self.wf[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(set(inputs), {"dry_run", "test_alert", "send_test"})
        self.assertEqual(inputs["send_test"]["type"], "boolean")
        self.assertIs(inputs["send_test"]["default"], False)
        self.assertEqual(self.wf["permissions"], {"contents": "write", "actions": "read"})
        code = re.sub(r"(?m)^\s*#.*$", "", self.text)
        self.assertEqual(set(re.findall(r"secrets\.([A-Za-z0-9_]+)", code)), {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"})
        for st in self.steps:
            if st.get("id") != "notify":
                self.assertNotIn("secrets.", json.dumps(st, ensure_ascii=False))
        self.assertEqual(set(self.wf[True]), {"schedule", "workflow_dispatch"})               # 觸發器沒有新增

    def test_validate_step_has_no_network_and_no_secrets(self):
        v = self.steps[0]
        self.assertNotIn("uses", v)
        self.assertNotRegex(v["run"], r"curl|wget|git |python|gh ")
        self.assertNotIn("secrets.", json.dumps(v, ensure_ascii=False))


# ─────────────────────────────────────────
# fsc_alert.run_pipe_test
# ─────────────────────────────────────────
class TgResp:
    def __init__(self, obj, status=200):
        self._b, self.status = json.dumps(obj).encode("utf-8"), status

    def read(self, n=-1):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTelegram:
    def __init__(self, resp=None, error=None):
        self.resp, self.error, self.requests = resp, error, []

    def __call__(self, req, timeout=None):
        self.requests.append((req.full_url, req.data, timeout))
        if self.error is not None:
            raise self.error
        return self.resp if self.resp is not None else TgResp({"ok": True, "result": {"message_id": 1}})


def http_err(code, body, url="https://api.telegram.org/botSECRET/sendMessage"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(raw))


def pipe_env(**kw):
    e = {"SEND_TEST": "1", "ALERT_EVENT_NAME": "workflow_dispatch", "ALERT_REF": "refs/heads/master", "ARCHIVE_DRY_RUN": "0", "ALERT_TEST": "0", "GITHUB_REPOSITORY": "o/r",
         "GITHUB_RUN_ID": "4242", "GITHUB_SERVER_URL": "https://github.com", "TELEGRAM_BOT_TOKEN": TOKEN, "BOT_ALERT_CHAT_ID": CHAT,
         "GH_API_TOKEN": GH, "ARCHIVE_OUTCOME": "skipped", "COMMIT_OUTCOME": "skipped"}
    e.update(kw)
    return e


class PipeBase(unittest.TestCase):
    def go(self, tg=None, env=None, api=None):
        tg = tg or FakeTelegram()
        api = api or FakeApi([hist("success")])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = fa.main(env or pipe_env(), opener=api, telegram_opener=tg)
        self.tg, self.api = tg, api
        return code, out.getvalue()

    def assert_clean(self, out):
        for s in (TOKEN, CHAT, GH, "AAFAKE", "1234567890", "botSECRET"):
            self.assertNotIn(s, out, s)


class TestPipeTestSuccess(PipeBase):
    def test_sends_exactly_the_fixed_message_with_run_link_and_reports_success(self):
        code, out = self.go()
        self.assertEqual(code, 0)
        self.assertIn("✅ 管道測試訊息已送出", out)
        self.assertEqual(len(self.tg.requests), 1)
        url, data, timeout = self.tg.requests[0]
        self.assertTrue(url.startswith(f"https://api.telegram.org/bot{TOKEN}/sendMessage"))
        body = json.loads(data.decode("utf-8"))
        self.assertEqual(body["chat_id"], CHAT)
        # bot_relay 固定在訊息前加「🚨 」；其餘就是固定的一行加 run 連結，沒有任何資料
        self.assertEqual(body["text"], "🚨 【管道測試】archive-fsc 通知管道測試（非真實失敗）\nrun：https://github.com/o/r/actions/runs/4242")
        self.assert_clean(out)

    def test_message_carries_no_data_year_date_count_or_company(self):
        code, out = self.go()
        text = json.loads(self.tg.requests[0][1].decode("utf-8"))["text"]
        for bad in ("115", "114", "年度", "連續", "失敗項目", "最後成功", "公司", "金額", "2026", "次"):
            self.assertNotIn(bad, text.replace("非真實失敗", "").replace("通知管道測試", ""), bad)
        self.assertEqual(fa.PIPE_TEST_MESSAGE, "【管道測試】archive-fsc 通知管道測試（非真實失敗）")
        self.assertEqual(len(text.splitlines()), 2)

    def test_does_not_query_runs_read_files_write_files_or_commit(self):
        opened = []
        real_open = builtins.open

        def spy(file, mode="r", *a, **k):
            if any(c in str(mode) for c in "wax+"):
                opened.append((str(file), mode))
            return real_open(file, mode, *a, **k)
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        os.chdir(d)
        try:
            with mock.patch.object(builtins, "open", spy), mock.patch.object(subprocess, "run", side_effect=AssertionError("不得呼叫 subprocess")), \
                    mock.patch.object(subprocess, "Popen", side_effect=AssertionError("不得呼叫 subprocess")):
                code, out = self.go(env=pipe_env(ARCHIVE_FSC_DIR=os.path.join(d, "fsc")))
            self.assertEqual(os.listdir(d), [])                                              # 工作目錄沒有任何新檔
        finally:
            os.chdir(cwd)
        self.assertEqual((code, opened), (0, []))
        self.assertEqual(self.api.requests, [])                                              # 沒有查詢前幾次 run
        self.assertNotIn("判斷：", out)


class TestPipeTestFailures(PipeBase):
    def assert_red(self, code, out, *needles):
        self.assertEqual(code, 1, out)
        self.assertIn("::error title=管道測試失敗", out)
        self.assertNotIn("✅", out)
        for n in needles:
            self.assertIn(n, out)
        self.assert_clean(out)

    def test_http_errors_report_status_code_error_code_and_short_description_only(self):
        for code_, desc in ((400, "Bad Request: chat not found"), (401, "Unauthorized"), (403, "Forbidden: bot was blocked by the user"),
                            (404, "Not Found"), (429, "Too Many Requests: retry after 5")):
            with self.subTest(code=code_):
                body = {"ok": False, "error_code": code_, "description": desc, "parameters": {"leak": "FULLRESPONSELEAK-abcdef"}}
                code, out = self.go(FakeTelegram(error=http_err(code_, body)))
                self.assert_red(code, out, f"HTTP {code_}", f"Telegram error_code={code_}", f"description={desc}")
                self.assertNotIn("FULLRESPONSELEAK", out)                                    # 不印完整回應

    def test_non_json_error_body_still_reports_http_status(self):
        code, out = self.go(FakeTelegram(error=http_err(502, b"<html>Bad Gateway SECRETBODY</html>")))
        self.assert_red(code, out, "HTTP 502")
        self.assertNotIn("SECRETBODY", out)

    def test_description_containing_token_or_chat_id_is_masked_and_truncated(self):
        body = {"ok": False, "error_code": 400, "description": f"Bad Request: chat {CHAT} not found for bot {TOKEN} " + "x" * 400}
        code, out = self.go(FakeTelegram(error=http_err(400, body)))
        self.assert_red(code, out, "HTTP 400")
        line = [ln for ln in out.splitlines() if ln.startswith("::error")][0]
        self.assertLess(len(line), 400)
        self.assertIn("***", line)

    def test_timeout_and_connection_errors(self):
        for exc, name in ((TimeoutError("t"), "TimeoutError"), (urllib.error.URLError("down"), "URLError"), (OSError("x"), "OSError")):
            with self.subTest(exc=name):
                code, out = self.go(FakeTelegram(error=exc))
                self.assert_red(code, out, f"例外類別={name}", "逾時或連線失敗")

    def test_2xx_with_ok_false_is_a_failure(self):
        code, out = self.go(FakeTelegram(resp=TgResp({"ok": False, "error_code": 400, "description": "Bad Request: chat not found"})))
        self.assert_red(code, out, "Telegram error_code=400", "chat not found")

    def test_non_2xx_without_exception_is_a_failure(self):
        code, out = self.go(FakeTelegram(resp=TgResp({"ok": True}, status=500)))
        self.assertEqual(code, 1)
        self.assertNotIn("✅", out)

    def test_missing_or_empty_secrets_fail_without_any_request(self):
        cases = ({"TELEGRAM_BOT_TOKEN": ""}, {"BOT_ALERT_CHAT_ID": ""}, {"TELEGRAM_BOT_TOKEN": "", "BOT_ALERT_CHAT_ID": ""},
                 {"TELEGRAM_BOT_TOKEN": "   "}, {"BOT_ALERT_CHAT_ID": "  "})
        for kw in cases:
            with self.subTest(kw=kw):
                code, out = self.go(env=pipe_env(**kw))
                self.assert_red(code, out, "secrets 缺失", "沒有送出任何訊息")
                self.assertEqual(self.tg.requests, [])
        e = pipe_env()
        del e["TELEGRAM_BOT_TOKEN"]
        code, out = self.go(env=e)
        self.assert_red(code, out, "TELEGRAM_BOT_TOKEN")
        self.assertEqual(self.tg.requests, [])
        self.assertIn("TELEGRAM_CHAT_ID", self.go(env=pipe_env(BOT_ALERT_CHAT_ID=""))[1])      # 錯誤訊息用 secret 名稱，不印值

    def test_unexpected_exception_is_an_error_not_swallowed(self):
        with mock.patch.object(fa, "run_pipe_test", side_effect=RuntimeError("boom")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = fa.main(pipe_env(), telegram_opener=FakeTelegram())
        self.assertEqual(code, 1)
        self.assertIn("::error title=管道測試失敗 程式例外::RuntimeError", out.getvalue())

    def test_sender_raising_is_an_error(self):
        with mock.patch("bot_relay.send_telegram_alert", side_effect=RuntimeError("boom")):
            code, out = self.go()
        self.assert_red(code, out, "RuntimeError")


class TestPipeTestGuards(PipeBase):
    def test_only_workflow_dispatch_can_send(self):
        for ev in ("schedule", "push", "", "workflow_run", "repository_dispatch"):
            with self.subTest(event=ev):
                code, out = self.go(env=pipe_env(ALERT_EVENT_NAME=ev))
                self.assertEqual(code, 1)
                self.assertIn("只能由手動 dispatch 觸發", out)
                self.assertEqual(self.tg.requests, [])

    def test_only_master_can_send(self):
        for ref in ("refs/heads/claude/x", "refs/heads/main", "refs/tags/master", ""):
            with self.subTest(ref=ref):
                code, out = self.go(env=pipe_env(ALERT_REF=ref))
                self.assertEqual(code, 1)
                self.assertIn("只能在 master 分支上執行", out)
                self.assertEqual(self.tg.requests, [])
        e = pipe_env()
        del e["ALERT_REF"]
        code, out = self.go(env=e)
        self.assertEqual((code, self.tg.requests), (1, []))

    def test_dry_run_or_test_alert_combination_never_sends(self):
        for kw in ({"ARCHIVE_DRY_RUN": "1"}, {"ALERT_TEST": "1"}, {"ARCHIVE_DRY_RUN": "1", "ALERT_TEST": "1"}):
            with self.subTest(kw=kw):
                code, out = self.go(env=pipe_env(**kw))
                self.assertEqual(code, 1)
                self.assertIn("不可與 dry_run 或 test_alert 並用", out)
                self.assertEqual(self.tg.requests, [])

    def test_without_send_test_flag_the_normal_path_runs_and_no_test_message_is_sent(self):
        out = io.StringIO()
        tg = FakeTelegram()
        with contextlib.redirect_stdout(out):
            code = fa.main(pipe_env(SEND_TEST="0", ARCHIVE_OUTCOME="success", COMMIT_OUTCOME="success", ARCHIVE_LOG="/nonexistent"),
                           opener=FakeApi([hist("success")]), send=lambda t: True, telegram_opener=tg)
        self.assertEqual(code, 0)
        self.assertEqual(tg.requests, [])
        self.assertNotIn("管道測試", out.getvalue())

    def test_pipe_test_uses_bot_relay_and_the_same_two_secrets(self):
        with mock.patch("bot_relay.send_telegram_alert", return_value=True) as real_send:
            code, out = self.go()
        real_send.assert_called_once()
        text, env = real_send.call_args[0]
        self.assertEqual(set(env), {"TELEGRAM_BOT_TOKEN", "BOT_ALERT_CHAT_ID"})
        self.assertEqual(text, fa.pipe_test_message(pipe_env()))
        src = open(os.path.join(ROOT, "scripts", "fsc_alert.py"), encoding="utf-8").read()
        self.assertNotIn("api.telegram.org", src)                                            # 沒有另寫送訊程式
        self.assertNotIn("sendMessage", src)


class TestHistoryIgnoresPipeTests(unittest.TestCase):
    def env(self):
        return {"GITHUB_REPOSITORY": "o/r", "GITHUB_RUN_ID": "100", "GITHUB_REF_NAME": "master", "GH_API_TOKEN": GH}

    def test_conclusion_of_ignores_pipe_test_runs_whatever_their_result(self):
        for concl in ("success", "failure", "timed_out"):
            self.assertIsNone(fa.conclusion_of(api_run(1, concl, title="留存證期局申報案件彙總表（管道測試）")))
        self.assertEqual(fa.conclusion_of(api_run(1, "failure", title="留存證期局申報案件彙總表")), "failure")

    def test_red_pipe_test_run_does_not_create_a_failure_streak(self):
        runs = [api_run(99, "failure", title="留存證期局申報案件彙總表（管道測試）"), api_run(98, "success")]
        self.assertEqual(fa.fetch_history(self.env(), FakeApi([runs])), ["success"])
        d = fa.decide(True, fa.fetch_history(self.env(), FakeApi([runs])))
        self.assertEqual((d.kind, d.streak), ("first", 1))                                   # 前一次仍是成功 → 連續第 1 次

    def test_green_pipe_test_between_failures_does_not_break_the_streak_or_fake_a_recovery(self):
        runs = [api_run(99, "success", title="留存證期局申報案件彙總表（管道測試）"), api_run(98, "failure"), api_run(97, "failure"), api_run(96, "success")]
        h = fa.fetch_history(self.env(), FakeApi([runs]))
        self.assertEqual(h, ["failure", "failure", "success"])
        self.assertEqual(fa.decide(True, h).streak, 3)
        d = fa.decide(False, h)                                                              # 本次成功：恢復的是真正的失敗（連續 2 次），不是被測試 run「恢復」
        self.assertEqual((d.kind, d.streak), ("recovered", 2))

    def test_pipe_test_runs_do_not_trigger_recovery_after_success(self):
        runs = [api_run(99, "failure", title="留存證期局申報案件彙總表（管道測試）"), api_run(98, "success")]
        d = fa.decide(False, fa.fetch_history(self.env(), FakeApi([runs])))
        self.assertFalse(d.send)                                                             # 不會因為紅的測試 run 而發「已恢復」


if __name__ == "__main__":
    unittest.main()
