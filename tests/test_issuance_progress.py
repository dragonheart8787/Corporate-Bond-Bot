"""發行進度追蹤（issuance_progress.py）與其接進日報的整合測試。

注意：真實頻道公告只有標題、幾乎沒有節點日期，所以「帶日期」的樣本是依常見公告寫法自行編寫的合成資料
（以 SYN 標示）；「沒有日期」的樣本則直接取自真實公告標題。全部離線，不碰網路、不發任何訊息。
"""

import contextlib
import io
import itertools
import json
import os
import random
import shutil
import tempfile
import time
import unittest
from datetime import date as datetime_date
from datetime import timedelta as datetime_td
from unittest import mock

import issuance_progress as ip
from issuance_progress import ProgressStore, extract_events, resolve_targets


def row(code, name, body, stamp="20261009 10:00:00", tag=" #可轉債"):
    """組出和真實頻道一樣的訊息：#代號 #名稱 發佈時間：… 標題"""
    cell = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:]}"
    return {"date": cell, "text": f"#{code} #{name} 發佈時間：{stamp} {body}{tag}"}


def dates_of(body):
    ex = extract_events(row("9999", "測試", body)["text"])
    return {e.kind: e.date for e in ex.events}, ex


class TestNormalizeAndDates(unittest.TestCase):
    def d(self, s):
        return [h.iso for h in ip.find_dates(ip.normalize_text(s)) if h.iso]

    def test_ad_formats(self):
        for s in ("2026/10/09", "2026-10-09", "2026.10.09", "2026年10月9日", "2026年10月09日", "20261009"):
            self.assertEqual(self.d(f"於{s}決議"), ["2026-10-09"], s)

    def test_roc_formats(self):
        for s in ("115/10/09", "115/10/9", "115-10-9", "115.10.9", "115年10月9日", "115年10月09日", "1151009"):
            self.assertEqual(self.d(f"於{s}決議"), ["2026-10-09"], s)

    def test_fullwidth_digits_and_inserted_spaces(self):
        self.assertEqual(self.d("於１１５年１０月９日決議"), ["2026-10-09"])
        self.assertEqual(self.d("於115 年 10 月 9 日 決議"), ["2026-10-09"])      # 來源折行常在中文間插空白

    def test_fullwidth_separators_and_labels(self):
        # 全形斜線、冒號、逗號：沒做 NFKC 正規化就整個比對不到
        self.assertEqual(self.d("於115／10／9決議"), ["2026-10-09"])
        ev, _ = dates_of("國內第二次無擔保轉換公司債，董事會決議日：１１５／１０／９，贖回基準日１１５／１２／１")
        self.assertEqual(ev["board"], "2026-10-09")

    def test_roc_year_conversion_boundaries(self):
        self.assertEqual(self.d("100/1/1"), ["2011-01-01"])
        self.assertEqual(self.d("115/12/31"), ["2026-12-31"])
        self.assertEqual(self.d("199/1/1"), ["2110-01-01"])

    def test_nonexistent_dates_are_rejected_and_reported(self):
        for s in ("115/2/30", "115年13月1日", "2026/02/29", "115/4/31", "20261340", "1151332"):
            hits = ip.find_dates(ip.normalize_text(s))
            self.assertTrue(hits, s)
            self.assertTrue(all(h.iso is None and h.reason for h in hits), s)
        ev, ex = dates_of("本公司董事會於115年2月30日決議發行國內第一次無擔保轉換公司債")
        self.assertIsNone(ev["board"])
        self.assertTrue(any("無效" in n for n in ex.notes))

    def test_leap_day_is_valid(self):
        self.assertEqual(self.d("2028/02/29"), ["2028-02-29"])

    def test_out_of_range_years_rejected(self):
        for s in ("99/10/9", "1850/10/9", "2200/10/9"):
            self.assertEqual(self.d(s), [], s)

    def test_partial_dates_are_not_used(self):
        ev, ex = dates_of("本公司董事會於10月9日決議發行國內第一次無擔保轉換公司債")
        self.assertIsNone(ev["board"])
        self.assertTrue(any("不完整日期" in n for n in ex.notes))
        ev, _ = dates_of("本公司董事會於115年10月決議發行國內第一次無擔保轉換公司債")
        self.assertIsNone(ev["board"])

    def test_codes_and_ids_are_not_dates(self):
        for s in ("簡稱:森崴能源一，代碼:68061", "ISIN：XS3170882131", "HCB303", "金額1,151,009元", "電話02-1234-5678"):
            self.assertEqual(self.d(s), [], s)

    def test_publish_header_is_never_a_date_candidate(self):
        ev, ex = dates_of("本公司董事會決議發行國內第一次無擔保轉換公司債")
        self.assertEqual(ev, {"board": None})            # 發佈時間 20261009 不能變成決議日

    def test_normalize_keeps_latin_spaces_and_drops_cjk_spaces(self):
        self.assertEqual(ip.normalize_text("到期還本暨 終止上櫃買賣"), "到期還本暨終止上櫃買賣")
        self.assertEqual(ip.normalize_text("Biologics Limited 公告"), "Biologics Limited公告")
        self.assertEqual(ip.normalize_text("ABC   DEF"), "ABC DEF")


class TestNodeDetection(unittest.TestCase):
    # ── 6 個節點各自的肯定樣本（SYN = 合成，依常見公告寫法） ──
    def test_board_with_date_syn(self):
        ev, _ = dates_of("公告本公司董事會於115年10月9日決議發行國內第二次無擔保轉換公司債")
        self.assertEqual(ev, {"board": "2026-10-09"})

    def test_board_label_form_syn(self):
        ev, _ = dates_of("本公司國內第二次無擔保轉換公司債，董事會決議日期:115/10/09")
        self.assertEqual(ev["board"], "2026-10-09")

    def test_filing_syn(self):
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債，於115年10月15日向金管會申報募集發行")
        self.assertEqual(ev, {"filing": "2026-10-15"})
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債送件日:115/10/15")
        self.assertEqual(ev, {"filing": "2026-10-15"})

    def test_effective_syn(self):
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債，金管會於115年10月20日核准申報生效")
        self.assertEqual(ev, {"effective": "2026-10-20"})
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債，申報生效日期:115/10/20")
        self.assertEqual(ev, {"effective": "2026-10-20"})

    def test_auction_syn(self):
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債訂於115年10月25日辦理競價拍賣")
        self.assertEqual(ev, {"auction": "2026-10-25"})
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債公開承銷日:115/10/25")
        self.assertEqual(ev, {"auction": "2026-10-25"})

    def test_issue_syn(self):
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債，發行日:115年10月30日")
        self.assertEqual(ev, {"issue": "2026-10-30"})

    def test_listing_syn(self):
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債洽定上櫃買賣開始日為115年11月3日")
        self.assertEqual(ev, {"listing": "2026-11-03"})

    # ── 真實標題：有節點字樣、沒有日期 ──
    def test_real_titles_detect_node_without_date(self):
        real = {
            "board": "本公司董事會決議發行國內第五次無擔保轉換公司債",
            "paid": "公告本公司國內第六次無擔保轉換公司債收足債款",
            "pricing": "公告本公司國內第二次無擔保轉換公司債完成訂價",
        }
        for kind, body in real.items():
            ev, _ = dates_of(body)
            self.assertEqual(ev, {kind: None}, body)

    def test_no_node_wording_at_all(self):
        ev, ex = dates_of("公告本公司國內第四次無擔保轉換公司債轉換價格調整")
        self.assertEqual(ev, {})
        self.assertEqual(ex.skipped, "")

    def test_not_a_convertible_bond_is_skipped(self):
        ex = extract_events("#2317 #鴻海 發佈時間：20261011 16:59:13 董事會決議115年10月9日辦理現金增資")
        self.assertEqual(ex.skipped, "不是可轉債公告")
        self.assertEqual(ex.events, [])

    # ── 容易誤判的情境 ──
    def test_extension_of_offering_period_is_not_filing_or_effective(self):
        for body in (
            "公告本公司發行國內第五次無擔保轉換公司債案向金管會申請延長募集期間，業經金管會准予備查",
            "公告本公司申請展延115年國內第二次無擔保轉換公司債募集期間，業經金管會准予備查",
            "公告本公司申請延長現金增資暨國內第二次無擔保轉換公司債募集期間經金管會核准",
        ):
            ev, _ = dates_of(body)
            self.assertEqual(ev, {}, body)

    def test_amending_terms_approval_is_not_effective(self):
        ev, _ = dates_of("公告本公司修正「國內第二次無擔保轉換公司債發行及轉換辦法」業經金管會核准")
        self.assertEqual(ev, {})

    def test_terminating_otc_trading_is_not_listing(self):
        ev, _ = dates_of("公告本公司國內第四次有擔保轉換公司債到期還本暨終止上櫃買賣事宜")
        self.assertEqual(ev, {})
        ev, _ = dates_of("公告國內第三次無擔保轉換公司債行使贖回權暨訂於115年11月18日終止櫃檯買賣")
        self.assertEqual(ev, {})

    def test_negated_node_is_not_recorded_but_reported(self):
        for body in ("公告本公司董事會決議取消發行國內第二次無擔保轉換公司債",
                     "公告本公司董事會決議撤回國內第二次無擔保轉換公司債案",
                     "公告本公司董事會未通過發行國內第二次無擔保轉換公司債"):
            ev, ex = dates_of(body)
            self.assertEqual(ev, {}, body)
            self.assertIn("board", ex.negated, body)

    def test_other_company_announcements_are_skipped(self):
        ex = extract_events("#2330 #台積電 發佈時間：20261009 10:00:00 代子公司ABC公司公告董事會決議發行國內第一次無擔保轉換公司債 #可轉債")
        self.assertIn("他公司", ex.skipped)
        s = ProgressStore(os.path.join(tempfile.mkdtemp(), "p.json"))
        s.apply_row(row("2330", "台積電", "代子公司ABC公司公告董事會決議發行國內第一次無擔保轉換公司債"))
        self.assertEqual(s.targets, {})

    def test_legal_name_mismatch_is_treated_as_other_company(self):
        kr = resolve_targets("1111", "甲公司", "公告乙公司股份有限公司國內第二次無擔保轉換公司債董事會決議")
        self.assertEqual(kr.refs, [])
        self.assertIn("不符", kr.reason)

    def test_legal_name_that_matches_is_fine(self):
        kr = resolve_targets("6547", "高端疫苗", "公告高端疫苗股份有限公司第二次有擔保轉換公司債(簡稱：高端疫苗二，代碼：65472)")
        self.assertEqual([r.key for r in kr.refs], ["6547:國內第2次"])

    def test_historical_narrative_in_title_does_not_become_a_new_node(self):
        # 「補充115/8/11公告」只是引用舊公告，日期不是這次的節點日期
        ev, ex = dates_of("（補充115/8/11公告）本公司董事會決議發行國內第一次暨第二次無擔保轉換公司債(變更發行額度)")
        self.assertNotIn("board", ev)                      # 變更發行額度類：不計為董事會決議節點（見 TestAdjustments）
        self.assertEqual(ex.ref_dates, ["2026-08-11"])
        ev2, ex2 = dates_of("更正115年8月11日之公告，本公司董事會決議發行國內第一次無擔保轉換公司債")
        self.assertIsNone(ev2["board"])
        self.assertEqual(ex2.ref_dates, ["2026-08-11"])
        # 「依／據／如 … 所發布之公告」同樣是引用
        for lead in ("依", "據", "如"):
            ev3, ex3 = dates_of(f"{lead}115年8月11日所發布之公告，本公司董事會決議發行國內第一次無擔保轉換公司債")
            self.assertIsNone(ev3["board"], lead)
            self.assertEqual(ex3.ref_dates, ["2026-08-11"], lead)


class TestAdjustments(unittest.TestCase):
    """2026-10 調整：收足債款不再算發行；新增完成訂價；變更發行額度不算董事會決議；資料可能不全的警示。"""

    def store(self):
        return ProgressStore(os.path.join(tempfile.mkdtemp(), "p.json"))

    def target(self, s, key="9999:國內第2次"):
        return ip.summarize_target(key, s.targets[key])

    # ── a. 收足債款 ──
    def test_paid_up_is_not_the_issue_node(self):
        for body in ("公告本公司國內第二次無擔保轉換公司債收足債款", "公告本公司國內第二次無擔保轉換公司債收足應募價款",
                     "公告本公司國內第二次有擔保轉換公司債收足應募債款"):
            ev, _ = dates_of(body)
            self.assertEqual(ev, {"paid": None}, body)
            self.assertNotIn("issue", ev, body)

    def test_paid_up_never_moves_the_stage_or_creates_missing_nodes(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"))
        s.apply_row(row("9999", "測試", "國內第二次無擔保轉換公司債收足債款日:115年10月30日", stamp="20261030 10:00:00"))
        m = self.target(s)
        self.assertEqual(m.nodes["paid"]["date"], "2026-10-30")      # 日期照樣累積
        self.assertEqual(m.stage_kind, "board")                      # 但階段仍停在董事會
        self.assertEqual(m.missing_before_stage, [])
        self.assertIsNone(m.nodes["issue"]["date"])
        text = "\n".join(ip.format_summary_lines(m))
        self.assertIn("目前階段：董事會決議日", text)
        self.assertNotIn("資料不全", text)

    def test_issue_date_label_still_means_issue(self):
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債，發行日:115年10月30日")
        self.assertEqual(ev, {"issue": "2026-10-30"})

    # ── b. 完成訂價 ──
    def test_pricing_node_detected_with_and_without_date(self):
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債完成訂價")
        self.assertEqual(ev, {"pricing": None})
        ev, _ = dates_of("公告本公司國內第二次無擔保轉換公司債，訂價日:115年10月12日")
        self.assertEqual(ev, {"pricing": "2026-10-12"})
        ev, _ = dates_of("公告本公司發行國內第三次無擔保轉換公司債完成訂價及代收價款及存儲專戶行庫")
        self.assertEqual(ev, {"pricing": None})          # 代收價款行庫不是節點

    def test_pricing_is_shown_but_does_not_affect_stage(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "國內第二次無擔保轉換公司債，完成訂價日:115年10月12日", stamp="20261012 10:00:00"))
        m = self.target(s)
        self.assertEqual(m.nodes["pricing"]["date"], "2026-10-12")
        self.assertIsNone(m.stage_kind)
        text = "\n".join(ip.format_summary_lines(m))
        self.assertIn("訂價 2026-10-12", text)
        self.assertIn("目前階段：未偵測到", text)

    def test_pricing_undated_is_listed_as_announced_without_date(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "公告本公司國內第二次無擔保轉換公司債完成訂價"))
        text = "\n".join(ip.format_summary_lines(self.target(s)))
        self.assertIn("訂價 未偵測到", text)
        self.assertIn("已有公告但日期未揭露：完成訂價日", text)

    def test_info_nodes_line_always_present_and_fail_closed(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債"))
        text = "\n".join(ip.format_summary_lines(self.target(s)))
        self.assertIn("其他節點（不影響階段）：訂價 未偵測到｜收足債款 未偵測到", text)

    def test_negated_or_terminated_pricing_and_paid_are_not_nodes(self):
        for body in ("公告本公司國內第二次無擔保轉換公司債取消訂價", "公告本公司國內第二次無擔保轉換公司債終止收足債款"):
            ev, _ = dates_of(body)
            self.assertEqual(ev, {}, body)

    # ── c. (補充)…變更發行額度 ──
    def test_supplement_changing_issue_amount_is_not_a_board_resolution(self):
        real = "（補充115/8/11公告）本公司董事會決議發行國內第一次暨第二次無擔保轉換公司債(變更發行額度)"
        ev, ex = dates_of(real)
        self.assertEqual(ev, {})
        self.assertEqual(ex.ref_dates, ["2026-08-11"])
        self.assertTrue(any("修訂先前決議" in n for n in ex.notes))
        s = self.store()
        s.apply_row(row("3706", "神達", "（補充115/8/11公告）本公司董事會決議發行國內第一次暨第二次無擔保轉換公司債(變更發行額度)"))
        self.assertEqual(s.targets, {})                   # 連 evidence 也不建

    def test_other_amendment_wordings_and_dated_variant(self):
        for body in ("本公司董事會決議變更國內第二次無擔保轉換公司債發行額度",
                     "（更正）本公司董事會決議調整國內第二次無擔保轉換公司債發行條件",
                     "本公司董事會於115年10月9日決議修正國內第二次無擔保轉換公司債發行辦法",
                     "本公司董事會決議國內第二次無擔保轉換公司債增加發行金額"):
            ev, _ = dates_of(body)
            self.assertNotIn("board", ev, body)

    def test_plain_supplement_or_correction_of_a_real_resolution_still_counts(self):
        for body in ("(補充公告)本公司董事會決議發行國內第一次無擔保轉換公司債",
                     "(更正)公告本公司董事會決議發行國內第二次無擔保轉換公司債案"):
            ev, _ = dates_of(body)
            self.assertIn("board", ev, body)

    def test_amendment_does_not_overwrite_an_existing_board_date(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"))
        s.apply_row(row("9999", "測試", "(補充115/10/9公告)董事會於115年10月20日決議變更國內第二次無擔保轉換公司債發行額度", stamp="20261020 10:00:00"))
        m = self.target(s)
        self.assertEqual(m.nodes["board"]["date"], "2026-10-09")
        self.assertFalse(m.nodes["board"]["conflict"])

    # ── d. 資料可能不全 ──
    def test_coverage_title_only_source(self):
        s = self.store()
        s.mark_processed("20261011", 8)
        line = s.coverage_line("20261011", title_only=True)
        self.assertTrue(line.startswith("⚠️ 資料可能不全："))
        self.assertIn("來源只有公告標題", line)
        self.assertIn("紀錄自 2026-10-11 起累積", line)
        self.assertEqual(line.count("\n"), 0)            # 一行

    def test_coverage_no_warning_when_source_has_body_and_no_gap(self):
        s = self.store()
        s.mark_processed("20261010", 5)
        s.mark_processed("20261011", 5)
        self.assertEqual(s.coverage_line("20261011", title_only=False), "")

    def test_coverage_detects_days_the_daily_did_not_run(self):
        s = self.store()
        s.mark_processed("20261006", 5)
        s.mark_processed("20261011", 5)                  # 10/07~10/10 沒處理
        line = s.coverage_line("20261011", title_only=False)
        self.assertIn("缺 10/07、10/08、10/09、10/10 共 4 天", line)
        self.assertIn("當天 daily 未執行", line)

    def test_coverage_gap_listing_is_truncated(self):
        s = self.store()
        s.mark_processed("20261001", 5)
        line = s.coverage_line("20261011", title_only=False)
        self.assertIn("等 9 天", line)

    def test_coverage_consecutive_days_have_no_gap(self):
        s = self.store()
        for d in ("20261008", "20261009", "20261010"):
            s.mark_processed(d, 3)
        self.assertEqual(s.coverage_line("20261011", title_only=False), "")

    def test_zero_row_day_is_not_marked_so_the_gap_stays_visible(self):
        s = self.store()
        s.mark_processed("20261009", 5)
        self.assertFalse(s.mark_processed("20261010", 0))
        s.mark_processed("20261011", 5)
        self.assertIn("缺 10/10 共 1 天", s.coverage_line("20261011", title_only=False))

    def test_mark_processed_rejects_bad_dates_and_is_idempotent(self):
        s = self.store()
        self.assertFalse(s.mark_processed("2026-10-11", 5))
        self.assertTrue(s.mark_processed("20261011", 5))
        s.mark_processed("20261011", 5)
        self.assertEqual(s.data["processed_dates"], ["20261011"])

    def test_processed_dates_survive_restart_and_are_capped(self):
        s = self.store()
        base = datetime_date(2026, 1, 1)
        for i in range(150):
            s.mark_processed((base + datetime_td(days=i)).strftime("%Y%m%d"), 1)
        self.assertEqual(len(s.data["processed_dates"]), ProgressStore.PROCESSED_KEEP)
        s.save()
        self.assertEqual(ProgressStore.load(s.path).data["processed_dates"], s.data["processed_dates"])

    def test_report_date_not_wall_clock_decides_the_gap(self):
        s = self.store()
        s.mark_processed("20261009", 5)
        # 台北凌晨回退時 report_date 是前一天：10/10 在報告日期之後，不算缺
        self.assertEqual(s.coverage_line("20261010", title_only=False), "")


class TestMultipleDatesInOneAnnouncement(unittest.TestCase):
    def test_each_date_goes_to_its_nearest_node(self):
        ev, _ = dates_of("本公司董事會於115年10月9日決議發行國內第二次無擔保轉換公司債，並於115年10月15日向金管會申報")
        self.assertEqual(ev, {"board": "2026-10-09", "filing": "2026-10-15"})

    def test_date_before_or_after_keyword_both_work(self):
        a, _ = dates_of("115年10月9日董事會決議發行國內第二次無擔保轉換公司債")
        b, _ = dates_of("國內第二次無擔保轉換公司債，董事會決議日:115年10月9日")
        self.assertEqual(a["board"], "2026-10-09")
        self.assertEqual(b["board"], "2026-10-09")

    def test_nearer_semantic_wins_over_other_dates(self):
        # 贖回日／到期日不是節點日期，即使同一句還有節點關鍵詞
        ev, ex = dates_of("國內第二次無擔保轉換公司債，董事會決議日:115/10/9，贖回基準日115/12/1")
        self.assertEqual(ev["board"], "2026-10-09")
        self.assertTrue(any("其他語意" in n for n in ex.notes))

    def test_date_that_only_belongs_to_a_sink_is_not_assigned_to_any_node(self):
        ev, ex = dates_of("公告本公司董事會決議發行國內第二次無擔保轉換公司債，國內第一次債券於115年12月1日到期還本")
        self.assertEqual(ev, {"board": None})

    def test_equidistant_between_two_nodes_is_ambiguous_and_dropped(self):
        ev, ex = dates_of("國內第二次無擔保轉換公司債董事會決議115/10/9向金管會申報")
        self.assertEqual(ev.get("board"), None)
        self.assertEqual(ev.get("filing"), None)
        self.assertTrue(any("語意模糊" in n for n in ex.notes))

    def test_too_far_from_any_keyword_is_dropped(self):
        far = "，" + "說明內容" * 12 + "，"
        ev, ex = dates_of(f"國內第二次無擔保轉換公司債董事會決議{far}115/10/9")
        self.assertEqual(ev["board"], None)
        self.assertTrue(any("超過" in n for n in ex.notes))

    def test_clause_break_separates_meaning(self):
        ev, _ = dates_of("國內第二次無擔保轉換公司債董事會決議。另 115/10/9 公布其他事項")
        self.assertEqual(ev["board"], None)

    def test_tentative_dates_are_not_used_but_scheduled_ones_are(self):
        ev, ex = dates_of("國內第二次無擔保轉換公司債，預計於115年11月5日向金管會申報")
        self.assertEqual(ev["filing"], None)
        self.assertTrue(any("預估" in n for n in ex.notes))
        for w in ("擬於", "預定", "將於", "約", "暫定"):
            ev, _ = dates_of(f"國內第二次無擔保轉換公司債，{w}115年11月5日向金管會申報")
            self.assertIsNone(ev["filing"], w)
        ev, _ = dates_of("國內第二次無擔保轉換公司債，訂於115年11月5日辦理競價拍賣")
        self.assertEqual(ev["auction"], "2026-11-05")

    def test_two_dates_for_same_node_conflict_is_ambiguous(self):
        ev, ex = dates_of("國內第二次無擔保轉換公司債董事會決議日:115/10/9及115/10/12")
        self.assertIsNone(ev["board"])
        self.assertTrue(any("多個日期" in n for n in ex.notes))

    def test_date_range_takes_start(self):
        ev, _ = dates_of("國內第二次無擔保轉換公司債競價拍賣日期115/10/25至115/10/27")
        self.assertEqual(ev["auction"], "2026-10-25")

    def test_never_defaults_to_announcement_date_or_today(self):
        s = ProgressStore(os.path.join(tempfile.mkdtemp(), "p.json"))
        s.apply_row(row("9999", "測試", "公告本公司董事會決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"))
        summ = s.summaries_for_company("9999")[0]
        self.assertIsNone(summ.nodes["board"]["date"])
        self.assertNotIn("2026-10-09", "".join(ip.format_summary_lines(summ)))


class TestCnNumbers(unittest.TestCase):
    def test_table(self):
        cases = {"一": 1, "二": 2, "兩": 2, "九": 9, "十": 10, "十一": 11, "十九": 19, "二十": 20, "二十三": 23, "九十九": 99,
                 "1": 1, "09": 9, "12": 12}
        for k, v in cases.items():
            self.assertEqual(ip.cn_to_int(k), v, k)
        for bad in ("", "零", "0", "百", "一百", "二十十", "abc", "100"):
            self.assertIsNone(ip.cn_to_int(bad), bad)


class TestTargetKey(unittest.TestCase):
    def keys(self, body, code="9999", name="測試"):
        return [r.key for r in resolve_targets(code, name, f"#{code} #{name} 發佈時間：20261009 10:00:00 {body}").refs]

    def test_basic_forms(self):
        self.assertEqual(self.keys("公告本公司國內第二次無擔保轉換公司債"), ["9999:國內第2次"])
        self.assertEqual(self.keys("公告本公司國內第2次有擔保轉換公司債"), ["9999:國內第2次"])
        self.assertEqual(self.keys("公告本公司發行中華民國境內第四次無擔保轉換公司債"), ["9999:國內第4次"])
        self.assertEqual(self.keys("公告本公司國內第十二次無擔保轉換公司債"), ["9999:國內第12次"])

    def test_title_order_variants_give_the_same_key(self):
        a = self.keys("公告本公司第七次海外無擔保轉換公司債完成訂價")
        b = self.keys("公告本公司海外第七次無擔保轉換公司債完成訂價")
        self.assertEqual(a, b)
        self.assertEqual(a, ["9999:海外第7次"])

    def test_secured_or_not_does_not_split_a_series(self):
        self.assertEqual(self.keys("國內第一次有擔保轉換公司債"), self.keys("國內第一次無擔保轉換公司債"))

    def test_same_company_different_series_are_different_targets(self):
        self.assertNotEqual(self.keys("國內第一次無擔保轉換公司債"), self.keys("國內第二次無擔保轉換公司債"))

    def test_domestic_overseas_and_private_are_separate(self):
        ks = {self.keys("國內第一次無擔保轉換公司債")[0], self.keys("海外第一次無擔保轉換公司債")[0],
              self.keys("國內第一次私募無擔保轉換公司債")[0]}
        self.assertEqual(len(ks), 3)
        self.assertIn("9999:國內私募第1次", ks)

    def test_multi_series_fan_out(self):
        for body in ("國內第五次及第六次無擔保轉換公司債", "國內第一次暨第二次無擔保轉換公司債", "國內第四次及第五次無擔保轉換公司債"):
            self.assertEqual(len(self.keys(body)), 2, body)
        self.assertEqual(self.keys("國內第五次及第六次無擔保轉換公司債"), ["9999:國內第5次", "9999:國內第6次"])

    def test_bond_code_fallback(self):
        self.assertEqual(self.keys("本公司可轉換公司債測試五(代號:99995)近期達公布注意交易資訊標準"), ["9999:國內第5次"])

    def test_bond_code_of_other_company_is_not_used(self):
        kr = resolve_targets("9999", "測試", "本公司可轉換公司債某某五(代號:12345)")
        self.assertEqual(kr.refs, [])

    def test_title_and_bond_code_disagree_is_not_tracked(self):
        kr = resolve_targets("9999", "測試", "公告本公司國內第三次無擔保轉換公司債(簡稱:測試五，代碼:99995)")
        self.assertEqual(kr.refs, [])
        self.assertIn("不一致", kr.reason)

    def test_no_series_and_no_code_cannot_be_pinned(self):
        for body in ("公告本公司可轉換公司債到期還款計劃說明", "本公司董事會決議發行無擔保轉換公司債"):
            kr = resolve_targets("9999", "測試", f"#9999 #測試 發佈時間：20261009 10:00:00 {body}")
            self.assertEqual(kr.refs, [], body)
            self.assertIn("無法指到特定一檔", kr.reason)

    def test_second_amendment_is_not_a_series(self):
        self.assertEqual(self.keys("公告本公司第二次修正國內無擔保轉換公司債發行辦法"), [])

    def test_company_name_changes_or_suffix_do_not_change_key(self):
        a = resolve_targets("6957", "裕慶-KY", "#6957 #裕慶-KY 發佈時間：20261009 10:00:00 國內第一次無擔保轉換公司債").refs[0].key
        b = resolve_targets("6957", "新名稱", "#6957 #新名稱 發佈時間：20261009 10:00:00 國內第一次無擔保轉換公司債").refs[0].key
        self.assertEqual(a, b)

    def test_scope_inferred_flag(self):
        r = resolve_targets("9999", "測試", "公告本公司第二次有擔保轉換公司債").refs[0]
        self.assertTrue(r.scope_inferred)
        r = resolve_targets("9999", "測試", "公告本公司國內第二次有擔保轉換公司債").refs[0]
        self.assertFalse(r.scope_inferred)

    def test_halfwidth_fullwidth_and_spacing_noise(self):
        self.assertEqual(self.keys("國內第 二 次無擔保轉 換公司債"), ["9999:國內第2次"])


class TestAccumulationAndConflicts(unittest.TestCase):
    def store(self):
        return ProgressStore(os.path.join(tempfile.mkdtemp(), "p.json"))

    def summ(self, s, key="9999:國內第2次"):
        return ip.summarize_target(key, s.targets[key])

    def test_accumulates_across_days_without_overwriting_other_nodes(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"))
        s.apply_row(row("9999", "測試", "國內第二次無擔保轉換公司債於115年10月15日向金管會申報", stamp="20261015 10:00:00"))
        s.apply_row(row("9999", "測試", "國內第二次無擔保轉換公司債金管會於115年10月20日核准申報生效", stamp="20261020 10:00:00"))
        m = self.summ(s)
        self.assertEqual(m.nodes["board"]["date"], "2026-10-09")
        self.assertEqual(m.nodes["filing"]["date"], "2026-10-15")
        self.assertEqual(m.nodes["effective"]["date"], "2026-10-20")
        self.assertEqual(m.stage_kind, "effective")
        self.assertEqual(m.missing_before_stage, [])

    def test_unrelated_announcements_do_not_touch_progress(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債"))
        before = json.dumps(s.data, sort_keys=True)
        s.apply_row(row("9999", "測試", "公告本公司國內第二次無擔保轉換公司債轉換價格調整", stamp="20261012 10:00:00"))
        s.apply_row(row("1111", "別家", "公告本公司國內第二次無擔保轉換公司債贖回", stamp="20261012 10:00:00"))
        self.assertEqual(before, json.dumps(s.data, sort_keys=True))

    def test_newer_announcement_wins_and_keeps_old_value_with_source(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"))
        s.apply_row(row("9999", "測試", "(更正)董事會於115年10月8日決議發行國內第二次無擔保轉換公司債", stamp="20261010 09:00:00"))
        m = self.summ(s)
        self.assertEqual(m.nodes["board"]["date"], "2026-10-08")
        self.assertTrue(m.nodes["board"]["conflict"])
        sup = m.nodes["board"]["superseded"]
        self.assertEqual(sup[0]["date"], "2026-10-09")
        self.assertTrue(sup[0]["ann"].startswith("9999-20261009"))
        self.assertTrue(any("節點日期衝突" in l and "2026-10-09" in l and "2026-10-08" in l for l in s.logs))
        # history 兩筆都在
        hist = s.targets["9999:國內第2次"]["nodes"]["board"]["history"]
        self.assertEqual({h["date"] for h in hist}, {"2026-10-08", "2026-10-09"})

    def test_postponement_notice_replaces_schedule(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "國內第二次無擔保轉換公司債訂於115年10月25日辦理競價拍賣", stamp="20261015 10:00:00"))
        s.apply_row(row("9999", "測試", "國內第二次無擔保轉換公司債競價拍賣延後，改訂於115年11月1日辦理競價拍賣", stamp="20261020 10:00:00"))
        self.assertEqual(self.summ(s).nodes["auction"]["date"], "2026-11-01")

    def test_older_announcement_arriving_later_does_not_override(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月8日決議發行國內第二次無擔保轉換公司債", stamp="20261010 09:00:00"))
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"))
        self.assertEqual(self.summ(s).nodes["board"]["date"], "2026-10-08")
        self.assertTrue(any("較舊公告" in l and "不採用" in l for l in s.logs))

    def test_same_date_reannounced_is_not_a_conflict(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"))
        s.apply_row(row("9999", "測試", "(補充公告)董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261010 10:00:00"))
        m = self.summ(s)
        self.assertEqual(m.nodes["board"]["date"], "2026-10-09")
        self.assertFalse(m.nodes["board"]["conflict"])

    def test_idempotent_and_order_independent(self):
        rows = [
            row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"),
            row("9999", "測試", "(更正)董事會於115年10月8日決議發行國內第二次無擔保轉換公司債", stamp="20261010 09:00:00"),
            row("9999", "測試", "國內第二次無擔保轉換公司債於115年10月15日向金管會申報", stamp="20261015 10:00:00"),
            row("9999", "測試", "公告本公司國內第二次無擔保轉換公司債收足債款", stamp="20261030 10:00:00"),
        ]

        def snapshot(order, twice=False):
            s = self.store()
            for r in order:
                s.apply_row(r)
            if twice:
                for r in order:
                    s.apply_row(r)
            m = self.summ(s)
            # 連儲存的內容（history／evidence 的順序與筆數）都要一致，否則 git 會出現無意義的差異、重跑會重複累積
            raw = json.dumps(s.data["targets"], sort_keys=True, ensure_ascii=False)
            return {k: (v["date"], v["source"]) for k, v in m.nodes.items()}, m.stage_kind, raw

        base = snapshot(rows)
        self.assertEqual(snapshot(rows, twice=True), base)
        for perm in itertools.islice(itertools.permutations(rows), 24):
            self.assertEqual(snapshot(list(perm)), base)

    def test_same_publish_time_conflict_is_deterministic(self):
        a = row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00")
        b = row("9999", "測試", "董事會於115年10月8日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00")
        outs = set()
        for order in ([a, b], [b, a]):
            s = self.store()
            s.apply_rows(order)
            outs.add(self.summ(s).nodes["board"]["date"])
        self.assertEqual(len(outs), 1)

    def test_multi_series_announcement_updates_both_targets(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第一次暨第二次無擔保轉換公司債"))
        self.assertEqual(self.summ(s, "9999:國內第1次").nodes["board"]["date"], "2026-10-09")
        self.assertEqual(self.summ(s, "9999:國內第2次").nodes["board"]["date"], "2026-10-09")

    def test_same_company_two_series_progress_independently(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第一次無擔保轉換公司債", stamp="20261009 10:00:00"))
        s.apply_row(row("9999", "測試", "董事會於115年10月12日決議發行國內第二次無擔保轉換公司債", stamp="20261012 10:00:00"))
        s.apply_row(row("9999", "測試", "國內第一次無擔保轉換公司債於115年10月15日向金管會申報", stamp="20261015 10:00:00"))
        self.assertEqual(self.summ(s, "9999:國內第1次").nodes["filing"]["date"], "2026-10-15")
        self.assertIsNone(self.summ(s, "9999:國內第2次").nodes["filing"]["date"])
        self.assertEqual(self.summ(s, "9999:國內第2次").nodes["board"]["date"], "2026-10-12")

    def test_cancel_notice_flagged_not_applied(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"))
        s.apply_row(row("9999", "測試", "董事會決議取消發行國內第二次無擔保轉換公司債", stamp="20261012 10:00:00"))
        m = self.summ(s)
        self.assertEqual(m.nodes["board"]["date"], "2026-10-09")       # 不自行抹掉
        self.assertTrue(m.cancel_notice)
        self.assertIn("取消", "".join(ip.format_summary_lines(m)))

    def test_unresolvable_node_announcement_is_logged_not_stored(self):
        s = self.store()
        s.apply_row(row("9999", "測試", "本公司董事會決議發行無擔保轉換公司債"))
        self.assertEqual(s.targets, {})
        self.assertEqual(s.stats.get("unassigned"), 1)
        self.assertTrue(any("無法指到特定一檔" in l for l in s.logs))


class TestStageAndDisplay(unittest.TestCase):
    def target_with(self, **dates):
        t = {"code": "9999", "name": "測試", "scope": "國內", "private": False, "series": 2, "nodes": {}, "flags": {}}
        for kind, d in dates.items():
            t["nodes"][kind] = {"history": [{"date": d, "ann": f"a-{kind}", "published": "2026-10-09T10:00:00+08:00"}] if d else [],
                                "evidence": [{"ann": f"a-{kind}", "published": "2026-10-09T10:00:00+08:00", "has_date": bool(d)}]}
        return t

    def test_stage_is_last_dated_node_in_flow_order_not_by_date_value(self):
        # 日期大小故意顛倒：流程順序為準
        m = ip.summarize_target("9999:國內第2次", self.target_with(board="2026-10-30", filing="2026-10-01", effective="2026-09-01"))
        self.assertEqual(m.stage_kind, "effective")

    def test_skipped_node_is_marked_incomplete_and_not_filled(self):
        m = ip.summarize_target("9999:國內第2次", self.target_with(effective="2026-10-20"))
        self.assertEqual(m.stage_kind, "effective")
        self.assertEqual(m.missing_before_stage, ["board", "filing"])
        text = "\n".join(ip.format_summary_lines(m))
        self.assertIn("資料不全：缺 董事會決議日、送件日", text)
        self.assertIn("送件日：未偵測到", text)
        self.assertIsNone(m.nodes["filing"]["date"])

    def test_effective_without_filing_exactly_as_spec_example(self):
        m = ip.summarize_target("9999:國內第2次", self.target_with(board="2026-10-09", effective="2026-10-20"))
        self.assertEqual(m.missing_before_stage, ["filing"])
        self.assertIn("資料不全：缺 送件日", "\n".join(ip.format_summary_lines(m)))

    def test_no_dates_at_all(self):
        m = ip.summarize_target("9999:國內第2次", self.target_with(board=None, issue=None))
        self.assertIsNone(m.stage_kind)
        text = "\n".join(ip.format_summary_lines(m))
        self.assertIn("目前階段：未偵測到", text)
        self.assertIn("送件日：未偵測到", text)
        self.assertIn("已有公告但日期未揭露：董事會決議日、發行日", text)
        self.assertNotIn("資料不全", text)

    def test_every_missing_node_shows_not_detected_never_a_value(self):
        m = ip.summarize_target("9999:國內第2次", self.target_with(board="2026-10-09"))
        text = "\n".join(ip.format_summary_lines(m))
        for short in ("生效", "競拍", "發行", "上櫃"):
            self.assertIn(f"{short} 未偵測到", text)
        self.assertIn("送件日：未偵測到", text)
        self.assertIn("目前階段：董事會決議日", text)

    def test_format_has_stage_and_filing_date_first(self):
        m = ip.summarize_target("9999:國內第2次", self.target_with(board="2026-10-09", filing="2026-10-15"))
        lines = ip.format_summary_lines(m)
        self.assertTrue(lines[0].startswith("📈 發行進度（國內第2次）"))
        self.assertEqual(lines[1], "  目前階段：送件日")
        self.assertEqual(lines[2], "  送件日：2026-10-15")

    def test_block_for_unpinnable_announcement_says_not_detected_with_reason(self):
        s = ProgressStore(os.path.join(tempfile.mkdtemp(), "p.json"))
        block = ip.format_progress_block(s, "#9999 #測試 發佈時間：20261009 10:00:00 公告本公司可轉換公司債到期還款計劃說明 #可轉債")
        self.assertEqual(len(block), 1)
        self.assertIn("未偵測到", block[0])
        self.assertIn("無法指到特定一檔", block[0])

    def test_block_for_untracked_series_says_no_record(self):
        s = ProgressStore(os.path.join(tempfile.mkdtemp(), "p.json"))
        block = ip.format_progress_block(s, "#9999 #測試 發佈時間：20261009 10:00:00 公告本公司國內第二次無擔保轉換公司債轉換價格調整 #可轉債")
        self.assertIn("尚無此檔的進度紀錄", block[0])

    def test_error_block_is_explicit(self):
        self.assertIn("追蹤功能異常", ip.format_progress_block(None, "x", error="KeyError")[0])

    def test_block_never_contains_report_section_markers(self):
        # main.filter_cb_only 以這些字串判斷 CB 區段結尾；進度區塊含到任何一個就會被截斷
        markers = ["📢 澄清媒體報導", "💰 財務資訊", "👥 人事異動", "⚠️ 注意交易", "📋 重大訊息", "📄 其他", "── 其他公告"]
        t = self.target_with(effective="2026-10-20")
        t["flags"]["cancel_notice"] = {"ann": "x"}
        t["scope_inferred"] = True
        m = ip.summarize_target("9999:國內第2次", t)
        text = "\n".join(ip.format_summary_lines(m) + ip.format_progress_block(None, "x", error="E"))
        for mk in markers:
            self.assertNotIn(mk, text)


class TestStorePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "reports", "issuance_progress.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_survives_restart(self):
        s = ProgressStore(self.path)
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債"))
        s.save()
        s2 = ProgressStore.load(self.path)
        self.assertEqual(s2.targets, s.targets)
        self.assertEqual(ip.summarize_target("9999:國內第2次", s2.targets["9999:國內第2次"]).nodes["board"]["date"], "2026-10-09")
        self.assertFalse(os.path.exists(self.path + ".tmp"))        # 原子寫入不留暫存檔

    def test_accumulates_across_two_separate_runs(self):
        s = ProgressStore(self.path)
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債", stamp="20261009 10:00:00"))
        s.save()
        s = ProgressStore.load(self.path)               # 模擬隔天 / bot 重啟後的新 run
        s.apply_row(row("9999", "測試", "國內第二次無擔保轉換公司債於115年10月15日向金管會申報", stamp="20261015 10:00:00"))
        s.save()
        m = ip.summarize_target("9999:國內第2次", ProgressStore.load(self.path).targets["9999:國內第2次"])
        self.assertEqual((m.nodes["board"]["date"], m.nodes["filing"]["date"]), ("2026-10-09", "2026-10-15"))

    def test_load_missing_file_starts_empty_with_log(self):
        s = ProgressStore.load(self.path)
        self.assertEqual(s.targets, {})
        self.assertEqual(s.load_error, "")

    def test_corrupt_file_is_backed_up_and_reported_not_silently_overwritten(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        s = ProgressStore.load(self.path)
        self.assertTrue(s.load_error)
        self.assertTrue(any("::error" in l for l in s.logs))
        with open(self.path + ".corrupt", encoding="utf-8") as f:
            self.assertEqual(f.read(), "{ not json")

    def test_wrong_version_or_shape_is_treated_as_corrupt(self):
        os.makedirs(os.path.dirname(self.path))
        for payload in ('{"version": 99, "targets": {}}', '{"version": 1}', "[]"):
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(payload)
            self.assertTrue(ProgressStore.load(self.path).load_error, payload)

    def test_save_failure_raises_explicit_error_and_leaves_old_file_intact(self):
        s = ProgressStore(self.path)
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債"))
        s.save()
        good = open(self.path, encoding="utf-8").read()
        with mock.patch("issuance_progress.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(ip.ProgressError) as cm:
                s.save()
        self.assertIn("寫入", str(cm.exception))
        self.assertEqual(open(self.path, encoding="utf-8").read(), good)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_save_to_unwritable_location_raises(self):
        blocker = os.path.join(self.tmp, "blocker")
        open(blocker, "w").close()
        s = ProgressStore(os.path.join(blocker, "x.json"))
        with self.assertRaises(ip.ProgressError):
            s.save()

    def test_file_content_has_no_secrets_or_chat_ids(self):
        s = ProgressStore(self.path)
        s.apply_row(row("9999", "測試", "董事會於115年10月9日決議發行國內第二次無擔保轉換公司債"))
        s.save()
        raw = open(self.path, encoding="utf-8").read()
        self.assertNotRegex(raw, r"bot\d{5,}:|ghp_|chat_id|token")


class TestTaipeiTime(unittest.TestCase):
    def test_published_time_is_taipei_regardless_of_runner_timezone(self):
        old = os.environ.get("TZ")
        try:
            outs = set()
            for tz in ("UTC", "America/Los_Angeles", "Asia/Taipei"):
                os.environ["TZ"] = tz
                time.tzset()
                outs.add(ip.published_iso("#1 #a 發佈時間：20261009 23:30:00 x"))
            self.assertEqual(outs, {"2026-10-09T23:30:00+08:00"})
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()

    def test_fallback_to_csv_date_cell_and_none_when_absent(self):
        self.assertEqual(ip.published_iso("no header", "2026-10-09 08:00:00"), "2026-10-09T08:00:00+08:00")
        self.assertIsNone(ip.published_iso("no header", ""))

    def test_updated_at_is_taipei(self):
        tmp = tempfile.mkdtemp()
        s = ProgressStore(os.path.join(tmp, "p.json"))
        s.save()
        self.assertTrue(json.load(open(s.path))["updated_at"].endswith("+08:00"))
        shutil.rmtree(tmp)


class TestFormatterIntegration(unittest.TestCase):
    """接進 complete_formatter：/cb、/all、日報都經過這份報告。"""

    def setUp(self):
        import complete_formatter as cf
        self.cf = cf
        self.tmp = tempfile.mkdtemp()
        self.rows = [
            row("9999", "測試", "公告本公司董事會於115年10月9日決議發行國內第二次無擔保轉換公司債，並於115年10月15日向金管會申報"),
            row("1234", "某某", "公告本公司可轉換公司債到期還款計劃說明"),
            {"date": "2026-10-09 09:00:00", "text": "#2317 #鴻海 發佈時間：20261009 09:00:00 關於美國國際貿易委員會337調查之說明"},
        ]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def report(self, progress=None, error="", note=""):
        f = self.cf.CompleteFormatter()
        f.progress, f.progress_error, f.progress_note = progress, error, note
        return f.format_with_convertible_bond_priority(self.rows)

    def test_block_only_in_cb_announcements(self):
        s = ProgressStore(os.path.join(self.tmp, "p.json"))
        s.apply_rows(self.rows)
        rep = self.report(s)
        self.assertEqual(rep.count("📈 發行進度"), 2)           # 兩則 CB 公告；鴻海沒有
        self.assertIn("目前階段：送件日", rep)
        self.assertIn("送件日：2026-10-15", rep)
        self.assertIn("無法指到特定一檔", rep)
        other_part = rep.split("📋 其他公告\n", 1)[1]
        self.assertNotIn("發行進度", other_part)

    def test_existing_layout_is_unchanged_when_progress_is_off(self):
        self.assertNotIn("發行進度", self.report(None))

    def test_progress_block_survives_filter_cb_only(self):
        import main as m
        s = ProgressStore(os.path.join(self.tmp, "p.json"))
        s.apply_rows(self.rows)
        with mock.patch.dict(os.environ, {"REPORT_DATE": "20261009"}):
            out = m.filter_cb_only(self.report(s))
        self.assertIn("目前階段：送件日", out)
        self.assertIn("送件日：2026-10-15", out)
        self.assertIn("共 2 則", out)

    def test_progress_block_survives_bot_filter_cb_only(self):
        import bot_listener as bl
        s = ProgressStore(os.path.join(self.tmp, "p.json"))
        s.apply_rows(self.rows)
        with mock.patch.dict(os.environ, {"REPORT_DATE": "20261009"}):
            out = bl.filter_cb_only(self.report(s))
        self.assertIn("送件日：2026-10-15", out)

    def test_warning_line_sits_under_cb_header_once_and_survives_filters(self):
        import main as m
        import bot_listener as bl
        s = ProgressStore(os.path.join(self.tmp, "p.json"))
        s.apply_rows(self.rows)
        f = self.cf.CompleteFormatter()
        f.progress = s
        f.progress_warning = "⚠️ 資料可能不全：來源只有公告標題（無內文），節點日期多半無法取得"
        rep = f.format_with_convertible_bond_priority(self.rows)
        self.assertEqual(rep.count("資料可能不全"), 1)
        head, _, tail = rep.partition("🔥 轉換公司債相關公告（優先顯示）\n" + "=" * 60 + "\n")
        self.assertTrue(tail.startswith("⚠️ 資料可能不全"))
        with mock.patch.dict(os.environ, {"REPORT_DATE": "20261009"}):
            self.assertIn("資料可能不全", m.filter_cb_only(rep))
            self.assertIn("資料可能不全", bl.filter_cb_only(rep))

    def test_no_warning_line_when_not_needed(self):
        rep = self.report(ProgressStore(os.path.join(self.tmp, "p.json")))
        self.assertNotIn("資料可能不全", rep)

    def test_end_to_end_titles_only_source_prints_warning_and_marks_day(self):
        rep, log = self._run_main(self.rows)
        self.assertIn("資料可能不全：來源只有公告標題", rep)
        data = json.load(open(os.path.join(self.tmp, "reports", "issuance_progress.json")))
        self.assertEqual(data["processed_dates"], ["20261009"])

    def test_end_to_end_detects_missing_daily(self):
        os.makedirs(os.path.join(self.tmp, "reports"))
        with open(os.path.join(self.tmp, "reports", "issuance_progress.json"), "w") as f:
            json.dump({"version": 1, "updated_at": None, "processed_dates": ["20261005"], "targets": {}}, f)
        rep, _ = self._run_main(self.rows)
        self.assertIn("缺 10/06、10/07、10/08 共 3 天", rep)

    def test_end_to_end_body_text_source_has_no_titles_only_warning(self):
        rows = [dict(r, text=r["text"] + " 說明：1.事實發生日:115/10/9") for r in self.rows]
        rep, _ = self._run_main(rows)
        self.assertNotIn("來源只有公告標題", rep)

    def test_save_note_is_shown(self):
        s = ProgressStore(os.path.join(self.tmp, "p.json"))
        s.apply_rows(self.rows)
        self.assertIn("進度紀錄寫入失敗", self.report(s, note="disk full"))

    def test_tracking_error_is_shown_not_silent(self):
        rep = self.report(None, error="KeyError")
        self.assertEqual(rep.count("追蹤功能異常"), 2)

    def _run_main(self, rows, env=None, patch=None):
        import csv
        old = os.getcwd()
        os.chdir(self.tmp)
        try:
            os.makedirs("outputs/daily", exist_ok=True)
            with open("outputs/daily/telegram_messages_20261009.csv", "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["date", "from", "text", "links"])
                w.writeheader()
                for r in rows:
                    w.writerow({"date": r["date"], "from": "x", "text": r["text"], "links": ""})
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"REPORT_DATE": "20261009", **(env or {})}), contextlib.redirect_stdout(out):
                if patch:
                    with patch():
                        self.cf.main()
                else:
                    self.cf.main()
            with open("outputs/daily/complete_report_20261009.txt", encoding="utf-8") as f:
                rep = f.read()
            return rep, out.getvalue()
        finally:
            os.chdir(old)

    def test_end_to_end_writes_store_in_reports_and_report_includes_progress(self):
        rep, log = self._run_main(self.rows)
        path = os.path.join(self.tmp, "reports", "issuance_progress.json")
        self.assertTrue(os.path.exists(path))
        self.assertIn("送件日：2026-10-15", rep)
        self.assertIn("✅ 9999:國內第2次 董事會決議日：2026-10-09", log)
        self.assertNotIn("::error", log)
        rep2, log2 = self._run_main(self.rows)                      # 同一天重跑：結果相同、不重複計
        self.assertEqual(json.load(open(path))["targets"].keys(), {"9999:國內第2次"})
        self.assertIn("送件日：2026-10-15", rep2)

    def test_end_to_end_store_write_failure_is_loud_but_report_still_produced(self):
        reports = os.path.join(self.tmp, "reports")
        open(reports, "w").close()                                   # reports 變成一般檔案 → 無法建立目錄
        rep, log = self._run_main(self.rows)
        self.assertIn("::error title=發行進度紀錄寫入失敗", log)
        self.assertIn("進度紀錄寫入失敗", rep)
        self.assertIn("送件日：2026-10-15", rep)                     # 本次結果仍顯示

    def test_end_to_end_parser_crash_does_not_lose_the_daily_report(self):
        def boom():
            return mock.patch("issuance_progress.extract_events", side_effect=RuntimeError("bug"))
        rep, log = self._run_main(self.rows, patch=boom)
        self.assertIn("::error title=發行進度追蹤失敗", log)
        self.assertIn("追蹤功能異常", rep)
        self.assertIn("公告本公司董事會於115年10月9日決議", rep)       # 原本的公告內容還在

    def test_end_to_end_corrupt_store_is_reported(self):
        os.makedirs(os.path.join(self.tmp, "reports"))
        with open(os.path.join(self.tmp, "reports", "issuance_progress.json"), "w") as f:
            f.write("garbage")
        rep, log = self._run_main(self.rows)
        self.assertIn("::error title=發行進度紀錄檔損毀", log)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "reports", "issuance_progress.json.corrupt")))

    def test_company_insights_file_lists_progress(self):
        self._run_main([self.rows[0]])
        with open(os.path.join(self.tmp, "outputs/daily/company_insights_20261009.txt"), encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("目前階段：送件日", txt)


class TestDailyWorkflowCommitsTheStore(unittest.TestCase):
    def test_store_lives_under_reports_which_daily_commit_step_adds(self):
        self.assertTrue(ip.DEFAULT_STORE_PATH.startswith("reports" + os.sep) or ip.DEFAULT_STORE_PATH.startswith("reports/"))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        wf = open(os.path.join(root, ".github", "workflows", "daily.yml"), encoding="utf-8").read()
        self.assertIn("git add reports/", wf)
        gi = open(os.path.join(root, ".gitignore"), encoding="utf-8").read()
        self.assertNotRegex(gi, r"(?m)^reports/?\s*$")
        self.assertNotIn("issuance_progress", gi)


if __name__ == "__main__":
    unittest.main()
