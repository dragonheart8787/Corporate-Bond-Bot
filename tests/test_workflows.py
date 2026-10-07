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
        declared = set(self.on["workflow_dispatch"]["inputs"])
        code = read("bot_listener.py") + read("bot_relay.py")
        sent = set(re.findall(r'"(handoff_[a-z_]+)"', code))
        self.assertEqual(sent, declared, "bot 送出的 input 與 bot.yml 宣告的不一致（GitHub 會回 422）")

    def test_inputs_are_passed_to_the_process(self):
        text = read(".github/workflows/bot.yml")
        for env, inp in (("BOT_PREV_RUN_ID", "handoff_prev_run_id"),
                         ("BOT_MIN_UPDATE_ID", "handoff_min_update_id"),
                         ("BOT_FAIL_COUNT", "handoff_fail_count")):
            self.assertRegex(text, rf"{env}: \$\{{\{{ github\.event\.inputs\.{inp} \}}\}}")

    def test_recovery_step_runs_on_failure_and_does_not_need_pip(self):
        steps = self.wf["jobs"]["bot-listener"]["steps"]
        rec = [s for s in steps if "bot_relay.py recover" in s.get("run", "")]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["if"].replace(" ", ""), "${{failure()}}")
        self.assertEqual(steps[-1], rec[0], "自救步驟必須是最後一步，才能涵蓋前面所有步驟的失敗")

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
