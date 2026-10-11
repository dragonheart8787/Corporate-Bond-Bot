"""唯讀探測（scripts/probe_sources.py 與 probe-sources.yml）的測試。全部離線：用假的 opener，不連任何網站。"""

import contextlib
import io
import json
import os
import re
import sys
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import probe_sources as ps  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

SECRET_VALUE = "機密內文ABC123不得出現在輸出"


class FakeHeaders(dict):
    def get(self, k, d=None):
        for key, v in self.items():
            if key.lower() == k.lower():
                return v
        return d


class FakeResp:
    def __init__(self, body=b"", status=200, ctype="application/json"):
        self._body = body
        self.status = status
        self.headers = FakeHeaders({"Content-Type": ctype})

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """依序回傳／拋出 outcomes（每個來源一個）。記錄每次請求。"""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req.full_url, req.get_method(), dict(req.header_items()), timeout))
        item = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(item, Exception):
            raise item
        return item


def http_error(code, location=None, ctype="text/html"):
    h = FakeHeaders({"Content-Type": ctype})
    if location:
        h["Location"] = location
    return urllib.error.HTTPError("https://x", code, "err", h, io.BytesIO(b""))


def jbody(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


TWSE_ROWS = [
    {"發言日期": "1151009", "公司代號": "9999", "主旨": f"本公司辦理送件 {SECRET_VALUE}", "說明": "x"},
    {"發言日期": "1151011", "公司代號": "8888", "主旨": f"公告申報生效 {SECRET_VALUE}", "說明": "y"},
    {"發言日期": "1151008", "公司代號": "7777", "主旨": "其他", "說明": "z"},
]


def run_probe(opener, env=None, sleeps=None):
    out = io.StringIO()
    sl = [] if sleeps is None else sleeps
    e = {"PROBE_REPO": "o/r", **(env or {})}
    with contextlib.redirect_stdout(out):
        code = ps.run(e, opener=opener, sleep=sl.append)
    return code, out.getvalue(), sl


class TestUrlValidation(unittest.TestCase):
    def test_allowed(self):
        for u in (ps.DEFAULT_TWSE_URL, ps.DEFAULT_TPEX_URL, "https://openapi.tpex.org.tw/openapi/v1/x",
                  "https://data.gov.tw/api/v2/rest/dataset/11406", "https://mops.twse.com.tw/x"):
            self.assertEqual(ps.validate_url(u), (True, ""), u)

    def test_rejected(self):
        for u in ("http://openapi.twse.com.tw/x", "https://evil.example.com/x", "https://user:pw@openapi.twse.com.tw/x",
                  "https://openapi.twse.com.tw:8443/x", "ftp://openapi.twse.com.tw/x", "", "//openapi.twse.com.tw/x",
                  "https://openapi.twse.com.tw.evil.com/x"):
            self.assertFalse(ps.validate_url(u)[0], u)

    def test_invalid_config_sends_nothing_and_exits_2(self):
        op = FakeOpener(FakeResp(b"[]"))
        code, out, _ = run_probe(op, {"PROBE_TWSE_URL": "https://evil.example.com/x"})
        self.assertEqual(code, 2)
        self.assertEqual(op.requests, [])
        self.assertIn("::error", out)
        self.assertIn("未送出任何請求", out)

    def test_dataset_ids_must_be_numeric(self):
        op = FakeOpener(FakeResp(b"[]"))
        code, out, _ = run_probe(op, {"PROBE_DATAGOV_IDS": "11406,../etc/passwd"})
        self.assertEqual(code, 2)
        self.assertEqual(op.requests, [])


class TestRequestDiscipline(unittest.TestCase):
    def ok_body(self):
        return FakeResp(jbody(TWSE_ROWS))

    def test_each_source_requested_exactly_once_with_at_least_6s_gaps(self):
        op = FakeOpener(self.ok_body())
        code, out, sleeps = run_probe(op)
        self.assertEqual(code, 0, out)
        urls = [r[0] for r in op.requests]
        self.assertEqual(len(urls), 4)
        self.assertEqual(len(set(urls)), 4)                       # 沒有重複請求同一個網址
        self.assertEqual(len(sleeps), 3)                          # 來源之間才睡
        self.assertTrue(all(s >= 6 for s in sleeps), sleeps)
        self.assertGreaterEqual(ps.MIN_INTERVAL_SEC, 6)

    def test_only_get_and_user_agent_names_the_project(self):
        op = FakeOpener(self.ok_body())
        run_probe(op)
        for url, method, headers, timeout in op.requests:
            self.assertEqual(method, "GET")
            ua = headers.get("User-agent", "")
            self.assertIn("Corporate-Bond-Bot-probe", ua)
            self.assertIn("github.com/o/r", ua)
            self.assertLessEqual(timeout, 30)
            self.assertNotIn("Authorization", headers)
            self.assertNotIn("Cookie", headers)

    def test_failure_is_not_retried(self):
        op = FakeOpener(http_error(503))
        code, out, _ = run_probe(op)
        self.assertEqual(len(op.requests), 4)                     # 4 個來源各一次；503 的那個沒有再打
        self.assertEqual(code, 1)

    def test_dry_run_connects_to_nothing(self):
        op = FakeOpener(self.ok_body())
        code, out, sleeps = run_probe(op, {"PROBE_DRY_RUN": "1"})
        self.assertEqual(code, 0)
        self.assertEqual(op.requests, [])
        self.assertEqual(sleeps, [])
        self.assertIn("DRY-RUN", out)

    def test_redirects_are_not_followed(self):
        # 真正的 opener 不跟隨轉址：確認 handler 回 None，而且轉址會被當成失敗回報
        h = ps._NoRedirect()
        self.assertIsNone(h.redirect_request(None, None, 302, "Found", {}, "https://other.example/x"))
        op = FakeOpener(http_error(302, location="https://other.example.com/login"))
        code, out, _ = run_probe(op)
        self.assertEqual(code, 1)
        self.assertIn("被轉址", out)
        self.assertIn("other.example.com", out)
        self.assertEqual(len(op.requests), 4)


class TestFailuresAreExplicit(unittest.TestCase):
    def one(self, outcome):
        op = FakeOpener(outcome)
        return run_probe(op)

    def test_http_errors_give_reason_and_nonzero(self):
        for code_, word in ((403, "被拒絕"), (404, "找不到"), (429, "頻繁"), (500, "伺服器錯誤")):
            c, out, _ = self.one(http_error(code_))
            self.assertEqual(c, 1)
            self.assertIn(f"HTTP {code_}", out)
            self.assertIn(word, out)
            self.assertIn("::error title=探測失敗", out)

    def test_connection_refused_and_dns_and_ssl(self):
        for err in (urllib.error.URLError(ConnectionRefusedError("refused")),
                    urllib.error.URLError(OSError("Name or service not known"))):
            c, out, _ = self.one(err)
            self.assertEqual(c, 1)
            self.assertIn("連線失敗", out)
            self.assertIn("::error", out)

    def test_timeout(self):
        c, out, _ = self.one(TimeoutError("timed out"))
        self.assertEqual(c, 1)
        self.assertIn("逾時", out)

    def test_html_block_page_is_reported_not_swallowed(self):
        c, out, _ = self.one(FakeResp(b"<!DOCTYPE html><html><body>Access Denied</body></html>", ctype="text/html"))
        self.assertEqual(c, 1)
        self.assertIn("HTML", out)
        self.assertIn("::error", out)
        self.assertNotIn("Access Denied", out)                    # 頁面內容不輸出

    def test_empty_and_garbage_bodies(self):
        for body, ctype in ((b"", "application/json"), (b"\xff\xfe\x00\x01", "application/octet-stream"), (b"{ not json", "application/json")):
            c, out, _ = self.one(FakeResp(body, ctype=ctype))
            self.assertEqual(c, 1, body)
            self.assertIn("::error", out)

    def test_one_failure_does_not_hide_the_others(self):
        op = FakeOpener(http_error(403), FakeResp(jbody(TWSE_ROWS)), FakeResp(jbody(TWSE_ROWS)), FakeResp(jbody(TWSE_ROWS)))
        code, out, _ = run_probe(op)
        self.assertEqual(code, 1)
        self.assertIn("3/4 個來源成功", out)
        self.assertEqual(out.count("::error"), 1)


class TestAnalysisAndOutput(unittest.TestCase):
    def test_json_list_fields_rows_dates_keywords(self):
        a = ps.analyze(jbody(TWSE_ROWS), "application/json")
        self.assertEqual((a.kind, a.rows), ("json", 3))
        self.assertEqual(set(a.fields), {"發言日期", "公司代號", "主旨", "說明"})
        self.assertEqual(a.date_ranges["發言日期"], ("2026-10-08", "2026-10-11"))
        self.assertEqual(a.keyword_counts, {"送件": 1, "申報生效": 1})

    def test_output_never_contains_values(self):
        op = FakeOpener(FakeResp(jbody(TWSE_ROWS)))
        code, out, _ = run_probe(op)
        self.assertEqual(code, 0)
        self.assertNotIn(SECRET_VALUE, out)
        self.assertNotIn("本公司辦理送件", out)
        self.assertNotIn("9999", out)                              # 公司代號之類的值也不輸出
        self.assertIn("欄位名稱（4）", out)
        self.assertIn("2026-10-08 ～ 2026-10-11", out)
        self.assertIn("含「送件」的記錄 1 筆", out)
        self.assertIn("含「申報生效」的記錄 1 筆", out)
        self.assertIn("HTTP 狀態：200", out)
        self.assertIn("回應大小：", out)

    def test_step_summary_has_only_structure(self):
        import tempfile
        d = tempfile.mkdtemp()
        path = os.path.join(d, "summary.md")
        op = FakeOpener(FakeResp(jbody(TWSE_ROWS)))
        run_probe(op, {"GITHUB_STEP_SUMMARY": path})
        text = open(path, encoding="utf-8").read()
        self.assertIn("只有結構，沒有內文", text)
        self.assertNotIn(SECRET_VALUE, text)

    def test_date_formats_in_fields(self):
        cases = {
            "1151009": "2026-10-09", "20261009": "2026-10-09", "115/10/09": "2026-10-09", "2026-10-09": "2026-10-09",
            "2026/10/9": "2026-10-09", "2026-10-09 15:30:00": "2026-10-09", "115年10月9日": "2026-10-09",
        }
        for raw, iso in cases.items():
            self.assertEqual(ps.parse_full_date(raw), iso, raw)
        for bad in ("115/2/30", "abc", "12345", "", None, True, "本公司於115/10/9", "9999999", "20261340"):
            self.assertIsNone(ps.parse_full_date(bad), bad)

    def test_non_date_columns_are_not_reported_as_dates(self):
        rows = [{"公司代號": "1101", "金額": "1234567"}, {"公司代號": "2330", "金額": "7654321"}]
        a = ps.analyze(jbody(rows), "application/json")
        self.assertEqual(a.date_ranges, {})

    def test_nested_container_like_data_gov_metadata(self):
        meta = {"result": {"title": "T", "distribution": [
            {"format": "CSV", "resourceDownloadUrl": "https://x/y.csv", "updated": "2026-10-01"},
            {"format": "JSON", "resourceDownloadUrl": "https://x/y.json", "updated": "2026-10-09"}]}}
        a = ps.analyze(jbody(meta), "application/json")
        self.assertEqual(a.rows, 2)
        self.assertIn("result.distribution", a.rows_source)
        self.assertIn("format", a.fields)
        self.assertEqual(a.date_ranges["updated"], ("2026-10-01", "2026-10-09"))

    def test_csv_with_big5_fallback_and_bom(self):
        csv_text = "發言日期,主旨\n1151009,送件\n1151010,其他\n"
        for enc in ("utf-8-sig", "big5"):
            a = ps.analyze(csv_text.encode(enc), "text/csv")
            self.assertEqual((a.kind, a.rows), ("csv", 2), enc)
            self.assertEqual(a.keyword_counts["送件"], 1)

    def test_empty_list_is_ok_but_reported_as_zero_rows(self):
        a = ps.analyze(b"[]", "application/json")
        self.assertEqual((a.kind, a.rows, a.fields), ("json", 0, []))

    def test_response_over_size_cap_is_truncated_and_flagged(self):
        big = b"[" + b"1," * 10 + b"1]"
        old = ps.MAX_BYTES
        ps.MAX_BYTES = 5
        try:
            r = ps.fetch_once("https://openapi.twse.com.tw/x", "ua", FakeOpener(FakeResp(big)))
        finally:
            ps.MAX_BYTES = old
        self.assertTrue(r.truncated)
        self.assertEqual(r.size, 5)


@unittest.skipIf(yaml is None, "需要 PyYAML")
class TestWorkflowIsReadOnlyAndManualOnly(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(ROOT, ".github", "workflows", "probe-sources.yml")
        self.text = open(self.path, encoding="utf-8").read()
        self.wf = yaml.safe_load(self.text)

    def test_only_workflow_dispatch(self):
        on = self.wf[True]
        self.assertEqual(set(on.keys()), {"workflow_dispatch"})

    def test_permissions_are_contents_read_only(self):
        self.assertEqual(self.wf["permissions"], {"contents": "read"})
        for job in self.wf["jobs"].values():
            self.assertNotIn("permissions", job)                   # 不得在 job 層放寬

    def test_no_secrets_no_telegram_no_dispatch_no_repo_writes(self):
        code = re.sub(r"(?m)^\s*#.*$", "", self.text)             # 註解可以提到這些字
        for forbidden in ("secrets.", "TELEGRAM", "api.telegram.org", "git push", "git commit", "workflow_run",
                          "repository_dispatch", "/dispatches", "gh api", "actions: write", "contents: write", "GITHUB_TOKEN"):
            self.assertNotIn(forbidden, code, forbidden)

    def test_checkout_does_not_persist_credentials(self):
        steps = self.wf["jobs"]["probe"]["steps"]
        co = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")][0]
        self.assertIs(co["with"]["persist-credentials"], False)

    def test_inputs_reach_the_script_only_through_env(self):
        steps = self.wf["jobs"]["probe"]["steps"]
        run_step = [s for s in steps if "probe_sources.py" in s.get("run", "")][0]
        self.assertNotIn("${{", run_step["run"])                   # 不把輸入直接插進 shell
        self.assertEqual(set(run_step["env"]), {"PROBE_TWSE_URL", "PROBE_TPEX_URL", "PROBE_DATAGOV_IDS", "PROBE_DRY_RUN", "PROBE_REPO"})

    def test_has_timeout_and_no_schedule(self):
        self.assertLessEqual(self.wf["jobs"]["probe"]["timeout-minutes"], 15)
        self.assertNotIn("schedule", self.wf[True])

    def test_defaults_match_the_script_and_pass_the_allowlist(self):
        inputs = self.wf[True]["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["twse_url"]["default"], ps.DEFAULT_TWSE_URL)
        self.assertEqual(inputs["tpex_url"]["default"], ps.DEFAULT_TPEX_URL)
        self.assertEqual(inputs["datagov_ids"]["default"], ps.DEFAULT_DATAGOV_IDS)
        self.assertEqual(inputs["dry_run"]["default"], "false")
        for key in ("twse_url", "tpex_url"):
            self.assertTrue(ps.validate_url(inputs[key]["default"])[0])

    def test_script_uses_only_stdlib_and_never_writes_files_other_than_step_summary(self):
        src = open(os.path.join(ROOT, "scripts", "probe_sources.py"), encoding="utf-8").read()
        self.assertNotRegex(src, r"(?m)^\s*(import|from)\s+(requests|yaml|aiohttp|httpx)\b")
        self.assertEqual(len(re.findall(r'open\([^)]*"a"', src)), 1)       # 唯一的寫檔：GITHUB_STEP_SUMMARY
        self.assertNotRegex(src, r'open\([^)]*"w"')


if __name__ == "__main__":
    unittest.main()
