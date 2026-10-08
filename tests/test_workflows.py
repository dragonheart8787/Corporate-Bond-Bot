"""程式碼與 workflow YAML 之間的契約檢查。需要 PyYAML；沒有就整組略過（CI 不跑測試，這是本地用的）。

這些是「兩邊各自改都不會報錯、合起來卻壞掉」的地方：
  - bot 送出的 workflow_dispatch input 名稱，必須在目標 workflow 有宣告，否則 GitHub 回 422，接力靜默失效。
  - bot 需要 actions: write 才能觸發 workflow；權限被改小，接力就會 403。
  - cron 必須保留作備援（使用者明確要求）。
"""

import os
import re
import unittest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name):
    with open(os.path.join(ROOT, ".github", "workflows", name), encoding="utf-8") as f:
        return yaml.safe_load(f)


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


@unittest.skipIf(yaml is None, "需要 PyYAML")
class TestBotWorkflow(unittest.TestCase):
    def setUp(self):
        self.wf = load("bot.yml")
        self.on = self.wf[True]   # PyYAML 會把 `on` 解析成布林 True

    def test_permissions_are_minimal_but_sufficient_for_relay(self):
        self.assertEqual(self.wf["permissions"], {"contents": "read", "actions": "write"})

    def test_cron_backup_is_kept(self):
        crons = [c["cron"] for c in self.on["schedule"]]
        self.assertEqual(crons, ["17 */2 * * *"])

    def test_only_one_bot_at_a_time_and_running_bot_is_not_cancelled(self):
        self.assertEqual(self.wf["concurrency"]["group"], "telegram-bot-listener")
        self.assertIs(self.wf["concurrency"]["cancel-in-progress"], False)

    def test_dispatch_inputs_match_what_the_code_sends(self):
        # max_runtime_minutes 是給人手動啟動用的，bot 不會自己送（見下方專屬測試）
        declared = set(self.on["workflow_dispatch"]["inputs"]) - {"max_runtime_minutes"}
        code = read("bot_listener.py") + read("bot_relay.py")
        sent = set(re.findall(r'"(handoff_[a-z_]+)"', code))
        self.assertEqual(sent, declared, "bot 送出的 input 與 bot.yml 宣告的不一致（GitHub 會回 422）")

    def test_inputs_are_passed_to_the_process(self):
        text = read(".github/workflows/bot.yml")
        for env, inp in (("BOT_PREV_RUN_ID", "handoff_prev_run_id"),
                         ("BOT_MIN_UPDATE_ID", "handoff_min_update_id"),
                         ("BOT_FAIL_COUNT", "handoff_fail_count")):
            self.assertRegex(text, rf"{env}: \$\{{\{{ github\.event\.inputs\.{inp} \}}\}}")

    def _steps(self):
        return self.wf["jobs"]["bot-listener"]["steps"]

    def test_recovery_step_covers_failure_cancel_and_timeout_and_is_last(self):
        steps = self._steps()
        rec = [s for s in steps if "bot_relay.py recover" in s.get("run", "")]
        self.assertEqual(len(rec), 1)
        cond = rec[0]["if"].replace(" ", "")
        # always() 才會在被取消時執行；failure() 不會
        self.assertEqual(cond, "${{always()&&steps.bot.outcome!='success'}}")
        self.assertNotEqual(cond, "${{failure()}}")
        self.assertEqual(steps[-1], rec[0], "自救步驟必須是最後一步，才能涵蓋前面所有步驟")

    def test_bot_step_has_id_and_its_own_timeout_below_the_job_timeout(self):
        bot = [s for s in self._steps() if s.get("id") == "bot"]
        self.assertEqual(len(bot), 1)
        self.assertIn("bot_listener.py", bot[0]["run"])
        self.assertLess(bot[0]["timeout-minutes"], self.wf["jobs"]["bot-listener"]["timeout-minutes"])
        self.assertGreater(bot[0]["timeout-minutes"], 300)   # 必須大於預設運行 300 分鐘，否則會在正常結束前被砍

    def test_recovery_step_env_has_status_run_id_and_alert_credentials(self):
        rec = [s for s in self._steps() if "bot_relay.py recover" in s.get("run", "")][0]
        env = rec["env"]
        self.assertEqual(env["BOT_JOB_STATUS"], "${{ job.status }}")
        self.assertEqual(env["GITHUB_RUN_ID"], "${{ github.run_id }}")
        self.assertIn("secrets.TELEGRAM_BOT_TOKEN", env["TELEGRAM_BOT_TOKEN"])
        self.assertIn("secrets.TELEGRAM_CHAT_ID", env["BOT_ALERT_CHAT_ID"])
        self.assertIn("-f bot_relay.py", rec["run"])          # checkout 失敗時有明確錯誤而不是 No such file

    def test_max_runtime_minutes_input_is_optional_and_reaches_the_process(self):
        inp = self.on["workflow_dispatch"]["inputs"]["max_runtime_minutes"]
        self.assertFalse(inp["required"])
        self.assertEqual(inp["default"], "")                   # 留空 = 預設 300 分鐘
        bot = [s for s in self._steps() if s.get("id") == "bot"][0]
        self.assertEqual(bot["env"]["BOT_MAX_RUNTIME_MINUTES"], "${{ github.event.inputs.max_runtime_minutes }}")

    def test_relay_never_forwards_max_runtime_so_successors_use_the_default(self):
        code = read("bot_listener.py") + read("bot_relay.py")
        self.assertNotRegex(code, r'"max_runtime_minutes"\s*:')

    def test_every_dispatch_ref_comes_from_default_branch_not_github_ref(self):
        text = read(".github/workflows/bot.yml")
        refs = re.findall(r"BOT_RELAY_REF: (.*)", text)
        self.assertEqual(len(refs), 2)            # bot 步驟與自救步驟
        for expr in refs:
            self.assertEqual(expr.strip(), "${{ github.event.repository.default_branch || 'master' }}")
        self.assertNotRegex(text, r"BOT_RELAY_REF:.*github\.ref")
        for name in ("bot_listener.py", "bot_relay.py"):
            # 只檢查「讀取」（註解與說明文字可以提到它）
            self.assertNotRegex(read(name), r"""(environ|env)\.get\(\s*["']GITHUB_(REF|HEAD_REF)""", name)
            self.assertNotRegex(read(name), r"""(environ|env)\[\s*["']GITHUB_(REF|HEAD_REF)""", name)

    def test_daily_and_bot_concurrency_groups_are_separate_and_never_cancel_running(self):
        daily = load("daily.yml")["jobs"]["fetch-and-send"]["concurrency"]
        self.assertTrue(daily["group"].startswith("corporate-bond-daily"))
        self.assertNotEqual(daily["group"], self.wf["concurrency"]["group"])
        self.assertIs(daily["cancel-in-progress"], False)

    def test_install_step_uses_retrying_script_and_pip_cache_is_on(self):
        steps = self.wf["jobs"]["bot-listener"]["steps"]
        self.assertTrue(any("pip_install_retry.sh" in s.get("run", "") for s in steps))
        self.assertFalse(any(s.get("run", "").strip().startswith("pip install") for s in steps))
        py = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python")][0]
        self.assertEqual(py["with"]["cache"], "pip")

    def test_job_timeout_leaves_room_after_relay_lead(self):
        # 本棒 5 小時 + 收尾；timeout 必須大於 5 小時，否則接力前就被強殺
        self.assertGreater(self.wf["jobs"]["bot-listener"]["timeout-minutes"], 5 * 60)
        self.assertLessEqual(self.wf["jobs"]["bot-listener"]["timeout-minutes"], 6 * 60)


@unittest.skipIf(yaml is None, "需要 PyYAML")
class TestDailyWorkflow(unittest.TestCase):
    def setUp(self):
        self.wf = load("daily.yml")
        self.on = self.wf[True]

    def test_all_four_cron_backups_are_kept(self):
        crons = sorted(c["cron"] for c in self.on["schedule"])
        self.assertEqual(crons, sorted(["23 10 * * *", "37 11 * * *", "53 12 * * *", "47 14 * * *"]))

    def test_dispatch_inputs_match_what_bot_sends(self):
        declared = self.on["workflow_dispatch"]["inputs"]
        self.assertIn("mode", declared)
        self.assertIn("skip_if_sent", declared)
        code = read("bot_listener.py")
        self.assertIn('{"mode": "all", "skip_if_sent": "true"}', code)
        self.assertIn("true", declared["skip_if_sent"]["options"])
        self.assertIn("all", declared["mode"]["options"])

    def test_skip_if_sent_guard_covers_cron_and_bot_but_not_plain_manual(self):
        send = [s for s in self.wf["jobs"]["fetch-and-send"]["steps"] if s.get("id") == "send"][0]
        expr = send["env"]["SKIP_IF_ALREADY_SENT"]
        self.assertIn("github.event_name == 'schedule'", expr)
        self.assertIn("github.event.inputs.skip_if_sent == 'true'", expr)

    def test_checkout_uses_latest_branch_head_not_the_stale_trigger_sha(self):
        steps = self.wf["jobs"]["fetch-and-send"]["steps"]
        co = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")]
        self.assertEqual(len(co), 1)
        self.assertEqual(co[0]["with"]["ref"], "${{ github.ref }}")
        self.assertIs(steps[0], co[0], "checkout 必須是第一步")

    def test_skip_decision_happens_in_send_step_after_checkout_and_install(self):
        steps = self.wf["jobs"]["fetch-and-send"]["steps"]
        idx = {s.get("id") or s["name"]: i for i, s in enumerate(steps)}
        send = idx["send"]
        checkout = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/checkout")][0]
        pip = [i for i, s in enumerate(steps) if "pip_install_retry" in s.get("run", "")][0]
        self.assertLess(checkout, send)
        self.assertLess(pip, send)
        # 判斷本身在 main.py 的 mode_send 裡（send 步驟之內），不在 workflow 的 if 條件上
        self.assertNotIn("last_sent", str(steps[send].get("if", "")))
        main_src = read("main.py")
        self.assertRegex(main_src, r'_truthy\("SKIP_IF_ALREADY_SENT"\) and already_sent\(target_date\)')

    def _daily_steps(self):
        return self.wf["jobs"]["fetch-and-send"]["steps"]

    def test_guard_runs_before_fetch_and_every_telegram_or_report_step_depends_on_it(self):
        steps = self._daily_steps()
        ids = [s.get("id") for s in steps]
        self.assertLess(ids.index("guard"), ids.index("fetch"))
        self.assertLess(ids.index("guard"), ids.index("send"))
        guard = steps[ids.index("guard")]
        self.assertEqual(guard["run"].strip(), "python main.py check-sent")
        self.assertIn("github.event.inputs.skip_if_sent == 'true'", guard["env"]["SKIP_IF_ALREADY_SENT"])
        self.assertIn("github.event_name == 'schedule'", guard["env"]["SKIP_IF_ALREADY_SENT"])
        self.assertNotIn("continue-on-error", guard)        # 守門失敗不得被吞掉
        for s in steps:
            if s.get("id") in ("fetch", "send") or "儲存報告" in s["name"] or "發送前檢查" in s["name"]:
                self.assertIn("steps.guard.outputs.skip != 'true'", s["if"], s["name"])

    def test_guard_is_after_checkout_and_install_but_before_any_telegram_connection(self):
        steps = self._daily_steps()
        names = [s["name"] for s in steps]
        g = [i for i, s in enumerate(steps) if s.get("id") == "guard"][0]
        self.assertLess([i for i, s in enumerate(steps) if "actions/checkout" in str(s.get("uses"))][0], g)
        self.assertLess([i for i, s in enumerate(steps) if "pip_install_retry" in s.get("run", "")][0], g)
        for i, s in enumerate(steps):
            if "TELEGRAM_SESSION_STRING" in str(s.get("env", {})):
                self.assertGreater(i, g, f"{names[i]} 會用到 Telegram session，必須在守門之後")

    def test_push_uses_retry_script_and_failure_is_not_just_a_warning(self):
        steps = self._daily_steps()
        commit = [s for s in steps if "Commit 報告" in s["name"]][0]
        self.assertIn("bash scripts/push_with_retry.sh", commit["run"])
        self.assertNotRegex(commit["run"], r"(?m)^\s*git push\b")
        self.assertNotIn("continue-on-error", commit)
        self.assertIn("secrets.TELEGRAM_BOT_TOKEN", commit["env"]["TELEGRAM_BOT_TOKEN"])
        self.assertIn("secrets.TELEGRAM_CHAT_ID", commit["env"]["BOT_ALERT_CHAT_ID"])
        script = read("scripts/push_with_retry.sh")
        self.assertIn("pull --rebase", script)
        self.assertIn("::error", script)
        self.assertRegex(script, r"(?m)^exit 1$")
        self.assertNotIn("origin master", script)      # 不寫死分支

    def test_summary_step_always_runs(self):
        steps = self.wf["jobs"]["fetch-and-send"]["steps"]
        summ = [s for s in steps if "send_result.txt" in s.get("run", "")]
        self.assertEqual(len(summ), 1)
        self.assertIn("always()", summ[0]["if"])

    def test_daily_does_not_gain_extra_permissions(self):
        self.assertEqual(self.wf["permissions"], {"contents": "write"})

    def test_install_step_uses_retrying_script(self):
        steps = self.wf["jobs"]["fetch-and-send"]["steps"]
        self.assertTrue(any("pip_install_retry.sh" in s.get("run", "") for s in steps))


class TestDependencies(unittest.TestCase):
    def test_unused_heavy_deps_are_gone(self):
        reqs = read("requirements.txt").lower()
        self.assertNotIn("pandas", reqs)
        self.assertNotIn("openpyxl", reqs)

    def test_nothing_imports_them(self):
        for dirpath, _, files in os.walk(ROOT):
            if any(part in dirpath for part in (".git", "tests", "__pycache__")):
                continue
            for name in files:
                if name.endswith(".py"):
                    src = read(os.path.relpath(os.path.join(dirpath, name), ROOT))
                    self.assertNotRegex(src, r"^\s*(import|from)\s+(pandas|openpyxl|numpy)\b", name)

    def test_relay_module_uses_only_stdlib_so_recovery_works_when_pip_failed(self):
        src = read("bot_relay.py")
        self.assertNotRegex(src, r"^\s*(import|from)\s+(requests|telethon|yaml)\b")


if __name__ == "__main__":
    unittest.main()
