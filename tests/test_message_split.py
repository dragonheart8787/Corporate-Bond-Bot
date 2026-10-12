"""Telegram 訊息分段（message_split.py）以及 bot_listener.send_message／main.send_telegram 實際送出的分段。
全部離線：requests.post 與 time.sleep 都被換掉，沒有任何真的連線、也不會送出任何訊息。"""

import contextlib
import io
import os
import shutil
import socket
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import bot_listener as bl  # noqa: E402
import complete_formatter as cf  # noqa: E402
import fsc_progress as fp  # noqa: E402
import main as main_mod  # noqa: E402
import message_split as ms  # noqa: E402
from test_fsc_progress import DataDir, days_ago, prow  # noqa: E402

_real_socket = socket.socket


def setUpModule():
    def blocked(*a, **k):
        raise AssertionError("測試不得建立真的網路連線")
    socket.socket = blocked  # type: ignore[assignment]


def tearDownModule():
    socket.socket = _real_socket  # type: ignore[assignment]


def case_text(n):
    """n 個「兩行一組」的案件（第二行縮排 6 格），模擬審理中清單。"""
    lines = ["⏳ 審理中 %d 筆（依收文日期新到舊）" % n]
    for i in range(n):
        lines.append(f"  • {1000 + i} 測試公司{i}｜無擔保｜{i % 50 + 1}.00億 台幣")
        lines.append(f"      收文日期（送件）2026-10-{i % 28 + 1:02d}｜自動補正日期 0 次｜生效日期（預定）2026-10-23｜十二日制")
    return "\n".join(lines)


def nonblank(text):
    return [ln for ln in text.split("\n") if ln.strip()]


class TestSplitMessage(unittest.TestCase):
    def check(self, text, limit=ms.LIMIT):
        chunks = ms.split_message(text, limit)
        for c in chunks:
            self.assertLessEqual(ms.ulen(c), limit)
            self.assertTrue(c.strip())
        return chunks

    def test_short_text_is_one_chunk_unchanged(self):
        self.assertEqual(ms.split_message("abc\n  • x\n      y"), ["abc\n  • x\n      y"])

    def test_limit_boundary(self):
        at = "a" * 3999 + "\n" + "b"                        # 剛好 4001？→ 3999 + 1 + 1 = 4001 > 4000
        self.assertEqual(len(self.check("x" * 4000)), 1)
        self.assertEqual(len(self.check(at)), 2)
        self.assertEqual(len(self.check("a" * 1999 + "\n" + "b" * 2000)), 1)                    # 1999+1+2000 = 4000
        self.assertEqual(len(self.check("a" * 1999 + "\n" + "b" * 2001)), 2)

    def test_packing_fills_a_message_up_to_exactly_the_limit(self):
        text = "x" * 1999 + "\n" + "y" * 2000 + "\n" + "z"             # 前兩行合起來剛好 4000
        chunks = self.check(text)
        self.assertEqual(chunks, ["x" * 1999 + "\n" + "y" * 2000, "z"])
        self.assertEqual(self.check("x" * 1999 + "\n" + "y" * 2001 + "\nz"), ["x" * 1999, "y" * 2001 + "\nz"])

    def test_chunks_never_start_or_end_with_a_blank_line(self):
        a = self.check("a" * 3999 + "\n\n" + "b" * 10)
        self.assertEqual(a, ["a" * 3999, "b" * 10])
        b = self.check("a" * 4000 + "\n\n" + "b")
        self.assertEqual(b, ["a" * 4000, "b"])

    def test_long_text_is_cut_only_at_newlines_and_loses_nothing(self):
        text = case_text(300)
        chunks = self.check(text)
        self.assertGreater(len(chunks), 1)
        original_lines = set(text.split("\n"))
        for c in chunks:
            for ln in c.split("\n"):
                self.assertIn(ln, original_lines)                                   # 每一行都是原文的完整一行
        self.assertEqual(nonblank("\n".join(chunks)), nonblank(text))               # 順序、內容完全一致

    def test_the_two_lines_of_one_case_are_never_separated(self):
        for n in (40, 100, 333):
            chunks = self.check(case_text(n))
            for c in chunks:
                lines = c.split("\n")
                self.assertFalse(lines[0].startswith(ms.CONTINUATION_INDENT), "分段後的第一行不能是續行")
                for i, ln in enumerate(lines):
                    if ln.startswith("  • "):
                        self.assertTrue(i + 1 < len(lines) and lines[i + 1].startswith(ms.CONTINUATION_INDENT), "案件的第一行後面必須接著它的續行")

    def test_emoji_count_as_two_units(self):
        text = "\n".join("🔥" * 100 for _ in range(30))                              # 每行 200 單位 → 一則最多 19 行（19*200+18=3818）
        chunks = self.check(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(ms.ulen(c) <= 4000 for c in chunks))
        self.assertEqual(nonblank("\n".join(chunks)), nonblank(text))

    def test_single_oversized_line_is_cut_at_a_separator_then_hard(self):
        line = "，".join(f"項目{i}" for i in range(3000))
        chunks = self.check(line)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), line)
        for c in chunks[:-1]:
            self.assertTrue(c.endswith("，"))
        hard = "字" * 9000
        chunks = self.check(hard)
        self.assertEqual("".join(chunks), hard)
        self.assertEqual(len(chunks), 3)

    def test_surrogate_pairs_are_never_split(self):
        text = "😀" * 5000
        chunks = self.check(text)
        self.assertEqual("".join(chunks), text)
        for c in chunks:
            c.encode("utf-16-le")                                                   # 切壞 surrogate 會在這裡丟例外

    def test_oversized_group_is_split_by_lines_but_never_mid_line(self):
        group = ["  • 1001 公司"] + ["      " + "續行" * 600 for _ in range(4)]       # 續行很長：整組超過上限
        chunks = self.check("\n".join(group))
        self.assertEqual(nonblank("\n".join(chunks)), nonblank("\n".join(group)))

    def test_blank_lines_at_boundaries_do_not_make_empty_chunks(self):
        text = "\n\n".join(case_text(30) for _ in range(6))
        chunks = self.check(text)
        self.assertTrue(all(c == c.strip("\n") for c in chunks))
        self.assertEqual(nonblank("\n".join(chunks)), nonblank(text))


class _Resp:
    ok = True
    status_code = 200

    def json(self):
        return {}


class TestActualSenders(unittest.TestCase):
    def capture(self, fn, *args):
        posted = []

        def fake_post(url, json=None, timeout=None, **kw):
            posted.append(json["text"])
            return _Resp()
        out = io.StringIO()
        with mock.patch("requests.post", fake_post), mock.patch("time.sleep", lambda s: None), contextlib.redirect_stdout(out):
            fn(*args)
        return posted

    def assert_clean(self, posted, text):
        self.assertGreater(len(posted), 1)
        total = len(posted)
        bodies = []
        for i, m in enumerate(posted, 1):
            head, body = m.split("\n", 1)
            self.assertEqual(head, f"[{i}/{total}]")
            self.assertLessEqual(ms.ulen(m), 4096)                                  # Telegram 的上限
            lines = body.split("\n")
            self.assertFalse(lines[0].startswith(ms.CONTINUATION_INDENT))
            for j, ln in enumerate(lines):
                if ln.startswith("  • "):
                    self.assertTrue(j + 1 < len(lines) and lines[j + 1].startswith(ms.CONTINUATION_INDENT))
            bodies.append(body)
        self.assertEqual(nonblank("\n".join(bodies)), nonblank(text))

    def test_bot_send_message_splits_at_newlines(self):
        text = case_text(120)
        self.assert_clean(self.capture(bl.send_message, "T", "1", text), text)

    def test_daily_send_telegram_splits_at_newlines(self):
        text = case_text(120)
        self.assert_clean(self.capture(main_mod.send_telegram, "T", "1", text), text)

    def test_short_message_has_no_header_and_is_unchanged(self):
        self.assertEqual(self.capture(bl.send_message, "T", "1", "hello\n  • x"), ["hello\n  • x"])
        self.assertEqual(self.capture(main_mod.send_telegram, "T", "1", "hello"), ["hello"])


class TestRealisticReports(unittest.TestCase):
    def test_current_real_data_full_report_length_and_chunks(self):
        block = fp.build_progress_block(datetime(2026, 10, 12, 12, 0, tzinfo=fp.TW))
        report = cf.CompleteFormatter().format_with_convertible_bond_priority([])
        self.assertIn(fp.TITLE, report)
        chunks = ms.split_message(block)
        # 不假設內容（真實資料每天會變）：只確認分段規則成立
        for c in chunks:
            self.assertLessEqual(ms.ulen(c), 4000)
        self.assertEqual(nonblank("\n".join(chunks)), nonblank(block))
        for c in chunks:
            self.assertFalse(c.split("\n")[0].startswith(ms.CONTINUATION_INDENT))

    def test_100_reviewing_cases_real_block_is_capped_at_30_and_splits_cleanly(self):
        d = DataDir()
        self.addCleanup(d.cleanup)
        rows = [prow(f"{1000 + i}", receipt=days_ago(i % 25 + 1), amount=str((i % 30 + 1) * 100000000), fix=",".join(["1150901"] * (i % 4)) or "") for i in range(100)]
        d.year("115", rows)
        d.year("114", [])
        d.check({"115": "2026-10-12", "114": "2026-10-12"})
        rep = fp.build_report(datetime(2026, 10, 12, 12, 0, tzinfo=fp.TW), d.dir)
        self.assertEqual(rep.counts["reviewing"], 100)
        self.assertEqual(sum(1 for ln in rep.text.splitlines() if ln.startswith("  • ")), 30)
        self.assertIn("…另有 70 筆未列出", rep.text)
        # 與一份很長的報告一起走過兩個真正的送出函式
        long_report = "\n".join(["前面的公告"] * 400) + "\n" + rep.text
        posted = []
        with mock.patch("requests.post", lambda url, json=None, timeout=None, **k: posted.append(json["text"]) or _Resp()), \
                mock.patch("time.sleep", lambda s: None), contextlib.redirect_stdout(io.StringIO()):
            bl.send_message("T", "1", long_report)
        self.assertGreater(len(posted), 1)
        joined = "\n".join(m.split("\n", 1)[1] for m in posted)
        self.assertEqual(nonblank(joined), nonblank(long_report))

    def test_unsplit_text_under_limit_still_one_message(self):
        d = DataDir()
        self.addCleanup(d.cleanup)
        d.year("115", [prow("1001")])
        d.year("114", [])
        d.check({"115": "2026-10-12", "114": "2026-10-12"})
        text = fp.build_progress_block(datetime(2026, 10, 12, 12, 0, tzinfo=fp.TW), d.dir)
        self.assertEqual(ms.split_message(text), [text])


if __name__ == "__main__":
    unittest.main()
