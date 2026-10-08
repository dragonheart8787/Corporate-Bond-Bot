"""daily.yml 的兩個關鍵機制：
  - 防重發守門在「抓取之前」：已送過的日報不得再連線 Telegram（main.py check-sent / all）。
  - 推送 .last_sent 前 pull --rebase 並重試；最終失敗要讓 run 變紅並告警（scripts/push_with_retry.sh，用本地 bare repo 實測）。
全部本地進行，不碰網路、不發任何訊息。
"""

import contextlib
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "push_with_retry.sh")


class TestSkipBeforeFetch(unittest.TestCase):
    def setUp(self):
        import main as m
        self.m = m
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.makedirs("reports")

    def tearDown(self):
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mark(self, date):
        with open(self.m.SENT_MARKER, "w", encoding="utf-8") as f:
            f.write(date + "\n")

    def run_check(self, env):
        out_file = os.path.join(self.tmp, "gh_output")
        if os.path.exists(out_file):
            os.remove(out_file)
        full = {"REPORT_DATE": "20261007", "GITHUB_OUTPUT": out_file, **env}
        with mock.patch.dict(os.environ, full), contextlib.redirect_stdout(io.StringIO()):
            self.m.mode_check_sent()
        with open(out_file, encoding="utf-8") as f:
            return f.read().strip()

    def test_already_sent_sets_skip_true_and_records_result(self):
        self.mark("20261007")
        self.assertEqual(self.run_check({"SKIP_IF_ALREADY_SENT": "true"}), "skip=true")
        with open(os.path.join("outputs", "daily", "send_result.txt"), encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "20261007 skipped_already_sent")

    def test_not_sent_yet_or_other_day_sets_skip_false(self):
        self.assertEqual(self.run_check({"SKIP_IF_ALREADY_SENT": "true"}), "skip=false")
        self.mark("20261006")
        self.assertEqual(self.run_check({"SKIP_IF_ALREADY_SENT": "true"}), "skip=false")

    def test_plain_manual_run_is_never_skipped(self):
        self.mark("20261007")
        self.assertEqual(self.run_check({"SKIP_IF_ALREADY_SENT": "false"}), "skip=false")

    def test_inside_actions_without_output_file_fails_closed(self):
        with mock.patch.dict(os.environ, {"REPORT_DATE": "20261007", "GITHUB_ACTIONS": "true", "GITHUB_OUTPUT": ""}), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                self.m.mode_check_sent()
        self.assertEqual(cm.exception.code, 1)

    def run_main(self, mode):
        calls = []
        with mock.patch.object(self.m, "mode_fetch", lambda: calls.append("fetch") or True), \
                mock.patch.object(self.m, "mode_send", lambda: calls.append("send")), \
                mock.patch("sys.argv", ["main.py", mode]), \
                mock.patch.dict(os.environ, {"REPORT_DATE": "20261007", "SKIP_IF_ALREADY_SENT": "true"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.m.main()
        return calls

    def test_all_mode_does_not_fetch_when_already_sent(self):
        self.mark("20261007")
        self.assertEqual(self.run_main("all"), [])          # fetch 會連 Telegram，不得呼叫

    def test_all_mode_fetches_then_sends_when_not_sent(self):
        self.assertEqual(self.run_main("all"), ["fetch", "send"])

    def test_send_step_keeps_its_own_second_check(self):
        self.mark("20261007")
        with mock.patch.dict(os.environ, {"REPORT_DATE": "20261007", "SKIP_IF_ALREADY_SENT": "true"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.m.mode_send()       # 不會走到 load_bot_credentials / 發送
        with open(os.path.join("outputs", "daily", "send_result.txt"), encoding="utf-8") as f:
            self.assertIn("skipped_already_sent", f.read())


def git(cwd, *args, check=True, env=None):
    e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    e.update(env or {})
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True, env=e)


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "需要 git 與 bash")
class TestPushWithRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", "-b", "master", self.remote)
        seed = os.path.join(self.tmp, "seed")
        git(self.tmp, "clone", "-q", self.remote, seed)
        git(seed, "checkout", "-q", "-b", "master")
        os.makedirs(os.path.join(seed, "reports"))
        self.write(seed, "reports/.last_sent", "20261006\n")
        self.write(seed, "reports/complete_report_latest.txt", "old\n")
        git(seed, "add", "-A"); git(seed, "commit", "-q", "-m", "init"); git(seed, "push", "-q", "origin", "master")
        self.work = os.path.join(self.tmp, "work")
        git(self.tmp, "clone", "-q", self.remote, self.work)
        self.alert_log = os.path.join(self.tmp, "alert.log")
        alert = os.path.join(self.tmp, "alert.sh")
        with open(alert, "w") as f:
            f.write(f'#!/bin/sh\necho "$1" >> "{self.alert_log}"\n')
        os.chmod(alert, os.stat(alert).st_mode | stat.S_IEXEC)
        self.alert = alert

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def write(base, rel, text):
        with open(os.path.join(base, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def commit_local(self, marker="20261007\n"):
        self.write(self.work, "reports/.last_sent", marker)
        self.write(self.work, "reports/complete_report_latest.txt", "new\n")
        git(self.work, "add", "reports/"); git(self.work, "commit", "-q", "-m", "update")

    def advance_remote(self, rel, text):
        other = os.path.join(self.tmp, "other")
        git(self.tmp, "clone", "-q", self.remote, other)
        self.write(other, rel, text)
        git(other, "add", "-A"); git(other, "commit", "-q", "-m", "other"); git(other, "push", "-q", "origin", "master")
        shutil.rmtree(other)

    def run_script(self, **env):
        e = {**os.environ, "PUSH_BRANCH": "master", "PUSH_RETRY_SLEEP": "0", "PUSH_ALERT_CMD": self.alert,
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", **env}
        return subprocess.run(["bash", SCRIPT], cwd=self.work, capture_output=True, text=True, env=e)

    def remote_file(self, rel):
        return git(self.tmp, "--git-dir", self.remote, "show", f"master:{rel}").stdout

    def test_plain_push_succeeds_without_alert(self):
        self.commit_local()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.remote_file("reports/.last_sent"), "20261007\n")
        self.assertFalse(os.path.exists(self.alert_log))

    def test_remote_moved_ahead_is_rebased_then_pushed(self):
        self.commit_local()
        self.advance_remote("README.md", "other change\n")     # 讓 push 變 non-fast-forward
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.remote_file("reports/.last_sent"), "20261007\n")
        self.assertEqual(self.remote_file("README.md"), "other change\n")   # 對方的變更沒被蓋掉

    def test_conflicting_marker_keeps_ours_instead_of_losing_the_marker(self):
        self.commit_local()
        self.advance_remote("reports/.last_sent", "20261005\n")  # 同檔衝突
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.remote_file("reports/.last_sent"), "20261007\n")

    def test_final_failure_is_red_with_error_annotation_and_alert(self):
        self.commit_local()
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        with open(hook, "w") as f:
            f.write("#!/bin/sh\necho rejected >&2\nexit 1\n")
        os.chmod(hook, 0o755)
        r = self.run_script(PUSH_ATTEMPTS="3", GITHUB_RUN_ID="77", GITHUB_REPOSITORY="o/r", GITHUB_SERVER_URL="https://github.com")
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error title=git push 最終失敗", r.stdout)
        self.assertEqual(r.stdout.count("push 失敗（"), 3)        # 真的重試了 3 次
        with open(self.alert_log, encoding="utf-8") as f:
            alert = f.read()
        self.assertIn("下一班可能重發", alert)
        self.assertIn("o/r/actions/runs/77", alert)
        self.assertEqual(self.remote_file("reports/.last_sent"), "20261006\n")   # 確實沒推上去

    def test_alert_command_failure_does_not_mask_the_failure(self):
        self.commit_local()
        shutil.rmtree(self.remote)                              # 遠端消失：pull 與 push 都失敗
        r = self.run_script(PUSH_ATTEMPTS="2", PUSH_ALERT_CMD="false")
        self.assertEqual(r.returncode, 1)
        self.assertIn("告警也沒送出去", r.stdout)


if __name__ == "__main__":
    unittest.main()
