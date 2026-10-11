#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
可轉債「發行進度」追蹤（只用標準函式庫）。

每檔可轉債各自累積 6 個流程節點的日期：
  board     董事會決議日
  filing    送件日（向金管會申報）
  effective 申報生效（核准）日
  auction   競價拍賣日（或公開承銷日）
  issue     發行日
  listing   上櫃／掛牌日

【資料來源的限制——務必先讀】
本專案抓到的 Telegram 公告只有「標題」（實測 27 天、2825 則不重複公告，最長 153 字，沒有任何
「說明：」內文）。標題幾乎不含節點日期，所以多數節點只能判定「有這個節點的公告」而拿不到日期。
本模組嚴格 fail-closed：日期只從公告文字抽取；抽不到就是「未偵測到」，不用公告日、今天或任何推估補值。
「有公告但沒日期」另外記成 evidence（證據），不會變成日期，也不影響「目前階段」。

【標的鍵】  "<公司代號>:<範圍><期別>"，例如  "6806:國內第1次"、"2303:海外第7次"、"2911:國內私募第1次"
  - 公司代號：公告開頭 `#代號 #名稱` 的代號（不用名稱：名稱會更名、會帶 -KY／*／-創 等尾碼）。
  - 期別：標題的「第N次」（支援國字與阿拉伯數字；「第四次及第五次」「第一次暨第二次」會各自展開成兩檔）。
    標題沒有「第N次」時，退而用債券代碼（代碼:30165 = 公司 3016 + 第 5 次；前四碼必須等於公司代號才採用）。
    兩者都沒有 → 無法指到特定一檔 → 不納入追蹤（絕不猜是哪一期）。
  - 範圍：海外／國外 → 海外；私募 → 國內私募（私募另有自己的期別編號）；其餘 → 國內。標題沒標範圍時
    預設國內（台灣慣例海外一定會標示），這是推定，會在紀錄裡標 scope_inferred。
  - 不併入鍵：有擔保／無擔保（同公司期別不分擔保與否連號）、債券簡稱。
  - 不追蹤：代子公司／他公司發的公告（公司代號是母公司，期別卻是別人的）。

【日期規則】（全部有測試）
  - 格式：西元 2026/10/09、2026-10-09、2026.10.09、2026年10月9日、20261009；
          民國 115/10/09、115-10-9、115.10.9、115年10月9日、1151009。全形數字先 NFKC 正規化。
  - 民國年只接受 100~199（= 2011~2110）；西元只接受 2000~2100；月日必須真的存在（2/30 之類丟棄並記錄）。
  - 沒有年份（10月9日）或沒有日（115年10月）→ 不採用。年份不能從公告日推。
  - 「語意最近」：每個日期歸給「字元距離最近」的關鍵詞（節點詞，或排除詞如贖回／到期／終止…）。
    距離相同、超過 30 字、或中間隔了句號／分號 → 不採用（模糊就不填）。歸給排除詞的日期丟棄。
  - 引用日期（補充115/8/11公告、更正…之公告）是「被引用公告的日期」，不是節點日期 → 不採用，另存 ref_dates 供人工參考。
  - 預估日期（預計／預定／擬／將於／約／暫定 + 日期）→ 不採用。「訂於」視為已排定，採用。
  - 同一節點在同一則公告出現兩個不同日期且不是區間 → 模糊，不採用；區間（115/10/9至115/10/12）取起日。
  - 公告發佈日與今天都不會被當成預設值。

【衝突與更正】同一節點多個不同日期：以「發佈時間較新的公告」為準，其餘保留在 history（含來源公告 id）。
  結果由 history 重新推導，不依賴處理順序，所以重跑、亂序都得到相同結果（idempotent）。

【階段】由「有日期的最後一個節點（照流程順序，不是日期大小）」推導。前面有節點沒日期 → 標「資料不全」，不補值。

時間一律用 Asia/Taipei，不依賴 runner 本地時區。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

TW = ZoneInfo("Asia/Taipei")

STORE_VERSION = 1
DEFAULT_STORE_PATH = os.path.join("reports", "issuance_progress.json")

# 流程順序（也是推導「目前階段」的順序）
NODE_ORDER = ["board", "filing", "effective", "auction", "issue", "listing"]
# 資訊節點：會顯示、會累積日期與證據，但「不參與階段推導，也不算資料不全」。
#   pricing 完成訂價：它在流程中的先後（競拍前或後）沒有資料可驗證，先不排進階段順序。
#   paid    收足債款：只代表款項收足的公告，不等於發行日（原本誤歸入 issue，已拆出）。
INFO_NODES = ["pricing", "paid"]
ALL_NODES = NODE_ORDER + INFO_NODES
NODE_LABEL = {
    "board": "董事會決議日",
    "filing": "送件日",
    "effective": "申報生效日",
    "auction": "競價拍賣日",
    "issue": "發行日",
    "pricing": "完成訂價日",
    "paid": "收足債款日",
    "listing": "上櫃日",
}
NODE_SHORT = {
    "board": "董事會",
    "filing": "送件",
    "effective": "生效",
    "auction": "競拍",
    "issue": "發行",
    "pricing": "訂價",
    "paid": "收足債款",
    "listing": "上櫃",
}
NOT_DETECTED = "未偵測到"

MAX_KEYWORD_DISTANCE = 30     # 日期與關鍵詞最遠相隔字數
EVIDENCE_KEEP = 10            # 每個節點最多保留幾則證據公告

PUBLISH_TIME_RE = re.compile(r"發佈時間：\s*(\d{8})\s+(\d{2}):(\d{2}):(\d{2})")
COMPANY_RE = re.compile(r"#(\d+)\s*#([^\s#]+)")


# ─────────────────────────────────────────
# 文字正規化
# ─────────────────────────────────────────
def _is_cjk(ch: str) -> bool:
    return bool(ch) and ("　" <= ch <= "〿" or "㐀" <= ch <= "䶿" or "一" <= ch <= "鿿")


def normalize_text(text: str) -> str:
    """
    NFKC（全形→半形）後，移除「兩側有中文」的空白。來源訊息是折行的，標題常被插入空白
    （例如「到期還本暨 終止上櫃買賣」「115 年」），不先拿掉會讓關鍵詞與日期都比對不到。
    """
    s = unicodedata.normalize("NFKC", text or "")
    out: List[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i].isspace():
            j = i
            while j < n and s[j].isspace():
                j += 1
            prev = out[-1] if out else ""
            nxt = s[j] if j < n else ""
            if not (_is_cjk(prev) or _is_cjk(nxt)) and prev and nxt:
                out.append(" ")
            i = j
        else:
            out.append(s[i])
            i += 1
    return "".join(out).strip()


_HEADER_RE = re.compile(r"^\s*#\d+\s*#[^\s#]+\s*發佈時間：\s*\d{8}\s+\d{2}:\d{2}:\d{2}\s*")
_TAG_RE = re.compile(r"(?:^|\s)#[^\s#]+")


def extract_title(text: str) -> str:
    """去掉 `#代號 #名稱 發佈時間：…` 抬頭與結尾的 #標籤，回傳正規化後的標題／內文。"""
    s = _HEADER_RE.sub("", text or "", count=1)
    s = _TAG_RE.sub(" ", s)
    return normalize_text(s)


# ─────────────────────────────────────────
# 日期解析
# ─────────────────────────────────────────
@dataclass
class DateHit:
    start: int
    end: int
    raw: str
    iso: Optional[str]            # 合法才有值
    reason: str = ""              # 不合法的原因
    ref: bool = False             # 引用日期（補充…公告）
    tentative: bool = False       # 預估日期


# 順序很重要：先找帶「年月日」或分隔符的，最後才找純數字
_DATE_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("ymd_cjk", re.compile(r"(?<![0-9A-Za-z])(\d{3,4})年(\d{1,2})月(\d{1,2})日?")),
    ("ymd_sep", re.compile(r"(?<![0-9A-Za-z.])(\d{3,4})([/\-.])(\d{1,2})\2(\d{1,2})(?![0-9])")),
    ("bare8", re.compile(r"(?<![0-9A-Za-z.,])(\d{8})(?![0-9A-Za-z])")),
    ("bare7", re.compile(r"(?<![0-9A-Za-z.,])(\d{7})(?![0-9A-Za-z])")),
]


def _to_gregorian(year: int) -> Tuple[Optional[int], str]:
    if 100 <= year <= 199:
        return year + 1911, ""
    if 2000 <= year <= 2100:
        return year, ""
    return None, f"年份 {year} 不在民國 100~199／西元 2000~2100"


def _build_iso(y: int, m: int, d: int) -> Tuple[Optional[str], str]:
    year, why = _to_gregorian(y)
    if year is None:
        return None, why
    try:
        return date(year, m, d).isoformat(), ""
    except ValueError:
        return None, f"{year}-{m:02d}-{d:02d} 不是存在的日期"


def find_dates(norm: str) -> List[DateHit]:
    """在正規化後的文字裡找出所有完整日期（含不合法的，標 reason），位置不重疊。"""
    hits: List[DateHit] = []
    taken: List[Tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(a < e and s < b for s, e in taken)

    for style, pat in _DATE_PATTERNS:
        for m in pat.finditer(norm):
            if overlaps(m.start(), m.end()):
                continue
            if style == "ymd_cjk":
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            elif style == "ymd_sep":
                y, mo, d = int(m.group(1)), int(m.group(3)), int(m.group(4))
            elif style == "bare8":
                s = m.group(1)
                y, mo, d = int(s[:4]), int(s[4:6]), int(s[6:])
            else:  # bare7：民國 yyyMMdd
                s = m.group(1)
                y, mo, d = int(s[:3]), int(s[3:5]), int(s[5:])
            iso, why = _build_iso(y, mo, d)
            hits.append(DateHit(m.start(), m.end(), m.group(0), iso, why))
            taken.append((m.start(), m.end()))
    hits.sort(key=lambda h: h.start)

    for h in hits:
        before = norm[max(0, h.start - 8):h.start]
        after = norm[h.end:h.end + 8]
        # 引用日期：補充115/8/11公告、更正…之公告、依115/8/11所發布…
        if re.search(r"(?:補充|更正|補正|修正|依|據|如)\s*$", before) and re.match(r"(?:日)?(?:之|所|的)?(?:公告|重大訊息|發布|發佈|函)", after):
            h.ref = True
        elif re.search(r"(?:補充|更正|補正)(?:原)?(?:公告)?\s*$", before):
            h.ref = True
        if re.search(r"(?:預計|預定|預期|擬|將|大約|約|暫定)(?:於|在|為)?\s*$", norm[max(0, h.start - 5):h.start]):
            h.tentative = True
    return hits


_PARTIAL_DATE_RES = [
    re.compile(r"(?<![0-9])\d{1,2}月\d{1,2}日"),            # 沒年份
    re.compile(r"(?<![0-9])\d{3,4}年\d{1,2}月(?!\d{1,2}日)"),  # 沒日
]


def find_partial_dates(norm: str) -> List[str]:
    """不完整的日期（沒年份／沒日）：不採用，只回傳給 log／統計用。"""
    full = find_dates(norm)
    out = []
    for pat in _PARTIAL_DATE_RES:
        for m in pat.finditer(norm):
            if not any(h.start <= m.start() < h.end for h in full):
                out.append(m.group(0))
    return out


# ─────────────────────────────────────────
# 節點關鍵詞
# ─────────────────────────────────────────
_AGENCY = r"(?:金管會|金融監督管理委員會|證期局|證券期貨局|行政院金融監督管理委員會)"
_SUFFIX = r"(?:日期|日)?[::]?(?:為|於)?"   # 連標籤後的冒號／「為」一起算進關鍵詞：「董事會決議日:115/10/9」

_NODE_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("board", re.compile(r"董事會[^,。;\n]{0,10}?(?:決議|通過)" + _SUFFIX)),
    ("filing", re.compile(r"送件" + _SUFFIX)),
    ("filing", re.compile(r"(?:向|函報)" + _AGENCY + r"[^,。;\n]{0,14}?(?:申報|申請|送件|函報)(?!生效)" + _SUFFIX)),
    ("filing", re.compile(r"申報(?:募集|發行)|申請(?:募集|發行)")),
    ("filing", re.compile(r"(?:申報|申請)日期|(?:申報|申請)日")),
    ("effective", re.compile(r"申報生效" + _SUFFIX)),
    ("effective", re.compile(r"生效日期|生效日")),
    ("effective", re.compile(r"(?:" + _AGENCY + r"[^,。;\n]{0,6}?)?(?:核准|核可|同意)(?:生效|申報|募集|發行|日期|日)")),
    ("auction", re.compile(r"競價拍賣|公開競價|競拍|公開承銷|詢價圈購|競標|標售|拍賣日|投標日|開標日")),
    ("issue", re.compile(r"發行日期|發行日|債券發行日")),
    ("issue", re.compile(r"發行完成")),
    ("paid", re.compile(r"(?:收足|募足)(?:應募)?(?:債款|價款|款項)(?:日期|日)?[::]?(?:為|於)?|完成募集|募集完成")),
    ("pricing", re.compile(r"完成訂價|完成定價|訂價完成|定價完成|(?:訂價|定價)(?:日期|日)[::]?(?:為|於)?")),
    ("listing", re.compile(r"上櫃(?:買賣)?(?:開始)?日期|上櫃(?:買賣)?(?:開始)?日|開始(?:上櫃|櫃檯買賣|櫃買)|掛牌(?:上櫃|日期|日)|核准上櫃|上櫃掛牌|櫃檯買賣開始日")),
]

# 其他語意的日期（贖回日、到期日、終止日…）：離這些詞比較近的日期，不是我們要的節點日期
_SINK_RE = re.compile(
    r"贖回|賣回|回售|到期|屆滿|期限|截止|基準日|終止|停止|債權清償|還本|付息|公告期間|過戶|行使|催告|"
    r"股東會|股東常會|臨時會|除權|除息|買回|庫藏股|減資|繳款|扣款|注意交易|轉換價|轉換期間"
)

# 節點關鍵詞附近出現這些字 → 這不是「發行流程」的節點（延長募集期間、修正辦法…）
_EXCLUDE_NEAR = {
    "filing": re.compile(r"延長|展延|延期|修正|更正|iXBRL|財務報告|財報"),
    "effective": re.compile(r"延長|展延|延期|修正|辦法|條款|調整|變更|撤銷|終止|解除|辭任"),
    "listing": re.compile(r"終止"),
    "issue": re.compile(r"終止"),
    "pricing": re.compile(r"終止|取消"),
    "paid": re.compile(r"終止|取消"),
    "auction": re.compile(r"終止|取消"),
    "board": re.compile(r"$^"),
}
# 變更發行額度／條件／價格…：是在修訂先前已決議的案子（例如「(補充115/8/11公告)…董事會決議發行…(變更發行額度)」）
_BOARD_AMEND_RE = re.compile(
    r"(?:變更|調整|修正|修訂|增加|減少|追加|提高|降低)[^,。;]{0,30}?(?:額度|總額|金額|條件|辦法|張數|規模)"
)
# 否定／取消：這則公告在說「沒有發生」，不能當節點證據
_NEGATE_RE = re.compile(r"取消|撤銷|撤回|廢止|不再|否決|未通過|未獲|不予|駁回|終止發行|終止辦理")

_CB_RE = re.compile(r"轉換公司債|可轉債|轉債")
# 代他公司公告：#代號 是母公司，期別卻是別人的
_OTHER_ISSUER_RE = re.compile(r"代(?:重要)?(?:子|孫|母)公司|代[^,。;\s]{1,16}?(?:股份有限公司|有限公司)公告")
_LEGAL_NAME_RE = re.compile(r"([一-鿿A-Za-z0-9*\-]{2,24})股份有限公司")


@dataclass
class NodeEvent:
    kind: str
    date: Optional[str] = None           # YYYY-MM-DD；None = 有這個節點的公告但沒有可採用的日期
    flags: List[str] = field(default_factory=list)
    keyword: str = ""


@dataclass
class Extraction:
    events: List[NodeEvent] = field(default_factory=list)
    negated: List[str] = field(default_factory=list)       # 被否定的節點 kind
    ref_dates: List[str] = field(default_factory=list)     # 引用日期（不採用）
    notes: List[str] = field(default_factory=list)         # 丟棄原因，供 log／統計
    skipped: str = ""                                      # 整則不處理的原因


_MASK = "◆"


def _kw_hits(masked: str) -> List[Tuple[str, int, int]]:
    hits: List[Tuple[str, int, int]] = []
    for kind, pat in _NODE_PATTERNS:
        for m in pat.finditer(masked):
            hits.append((kind, m.start(), m.end()))
    # 同一位置被多個 pattern 命中時，保留較長的（例如「申報生效」不要同時算成 filing）
    hits.sort(key=lambda h: (h[1], -(h[2] - h[1])))
    kept: List[Tuple[str, int, int]] = []
    for h in hits:
        if any(h[1] < k[2] and k[1] < h[2] for k in kept):
            continue
        kept.append(h)
    return kept


def extract_events(text: str) -> Extraction:
    """從一則公告（原文或標題皆可）抽出節點事件。這是唯一決定「日期歸誰」的地方。"""
    ex = Extraction()
    norm = extract_title(text)
    if not norm:
        ex.skipped = "空白內容"
        return ex
    if not _CB_RE.search(norm) and "可轉債" not in (text or ""):
        ex.skipped = "不是可轉債公告"
        return ex
    if _OTHER_ISSUER_RE.search(norm):
        ex.skipped = "代子公司／他公司公告，不納入追蹤"
        return ex

    dhits = find_dates(norm)
    for h in dhits:
        if h.iso is None:
            ex.notes.append(f"日期 {h.raw!r} 無效：{h.reason}")
    for p in find_partial_dates(norm):
        ex.notes.append(f"不完整日期 {p!r}（缺年或缺日）未採用")

    # 只有合法日期才參與配對；其餘維持原字串（不遮罩）
    usable = [h for h in dhits if h.iso is not None]
    pieces: List[str] = []
    pos = 0
    mpos: Dict[int, DateHit] = {}
    for h in usable:
        pieces.append(norm[pos:h.start])
        mpos[sum(len(p) for p in pieces)] = h
        pieces.append(_MASK)
        pos = h.end
    pieces.append(norm[pos:])
    masked = "".join(pieces)

    kws = _kw_hits(masked)
    sinks = [(m.start(), m.end()) for m in _SINK_RE.finditer(masked)]

    def clause_break(a: int, b: int) -> bool:
        lo, hi = (a, b) if a <= b else (b, a)
        return any(c in masked[lo:hi] for c in "。;\n")

    def gap(p: int, s: int, e: int) -> int:
        if s <= p < e:
            return 0
        return (s - p - 1) if p < s else (p - e)

    # 每個節點關鍵詞的「有效性」：是否被否定、是否在排除語境
    kw_state: List[Dict[str, Any]] = []
    for kind, s, e in kws:
        window = masked[max(0, s - 6):e + 10]
        excl = _EXCLUDE_NEAR[kind].search(masked[max(0, s - 6):e + 14]) if kind != "board" else None
        neg = bool(_NEGATE_RE.search(window))
        kw_state.append({"kind": kind, "s": s, "e": e, "neg": neg, "excl": bool(excl), "dates": []})

    for p, h in sorted(mpos.items()):
        if h.ref:
            ex.ref_dates.append(h.iso or "")
            ex.notes.append(f"{h.raw} 是引用的舊公告日期，不當節點日期")
            continue
        cands: List[Tuple[int, str, int]] = []   # (gap, 'kw'|'sink', index)
        for i, st in enumerate(kw_state):
            if clause_break(p, st["s"]):
                continue
            cands.append((gap(p, st["s"], st["e"]), "kw", i))
        for j, (s, e) in enumerate(sinks):
            if clause_break(p, s):
                continue
            cands.append((gap(p, s, e), "sink", j))
        if not cands:
            ex.notes.append(f"{h.raw} 附近沒有任何節點關鍵詞")
            continue
        cands.sort(key=lambda c: c[0])
        best = cands[0]
        if best[0] > MAX_KEYWORD_DISTANCE:
            ex.notes.append(f"{h.raw} 離最近的關鍵詞超過 {MAX_KEYWORD_DISTANCE} 字，不採用")
            continue
        ties = [c for c in cands if c[0] == best[0]]
        kinds_tied = {("sink" if c[1] == "sink" else kw_state[c[2]]["kind"]) for c in ties}
        if len(kinds_tied) > 1:
            ex.notes.append(f"{h.raw} 與多個不同語意的關鍵詞距離相同（{'/'.join(sorted(kinds_tied))}），語意模糊不採用")
            continue
        if best[1] == "sink":
            ex.notes.append(f"{h.raw} 最靠近的是其他語意（贖回／到期／終止…），不是節點日期")
            continue
        if h.tentative:
            ex.notes.append(f"{h.raw} 是預估日期（預計／擬…），不採用")
            continue
        kw_state[best[2]]["dates"].append(h)

    by_kind: Dict[str, Dict[str, Any]] = {}
    for st in kw_state:
        if st["excl"]:
            ex.notes.append(f"{st['kind']} 關鍵詞出現在排除語境（延長／修正／終止…），不算節點")
            continue
        if st["neg"]:
            ex.negated.append(st["kind"])
            continue
        slot = by_kind.setdefault(st["kind"], {"dates": [], "kw": masked[st["s"]:st["e"]]})
        slot["dates"].extend(st["dates"])

    # 董事會決議的「補充／變更發行額度」類標題：是對先前決議的修訂，不是一次新的董事會決議節點
    if "board" in by_kind and _BOARD_AMEND_RE.search(norm):
        ex.notes.append("董事會決議字樣出自「變更發行額度／條件」類公告，是修訂先前決議，不計為董事會決議節點")
        del by_kind["board"]

    for kind in ALL_NODES:
        slot = by_kind.get(kind)
        if not slot:
            continue
        isos = sorted({d.iso for d in slot["dates"] if d.iso})
        flags: List[str] = []
        chosen: Optional[str] = None
        if len(isos) == 1:
            chosen = isos[0]
        elif len(isos) > 1:
            ds = sorted(slot["dates"], key=lambda d: d.start)
            between = norm[ds[0].end:ds[-1].start] if len(ds) == 2 else ""
            if len(isos) == 2 and re.fullmatch(r"\s*(?:至|~|-|—|–|到)\s*", between or ""):
                chosen = isos[0]
                flags.append("range_start")
            else:
                ex.notes.append(f"{kind} 同則公告出現多個日期 {isos}，語意模糊不採用")
                flags.append("ambiguous_dates")
        if re.search(r"補充|更正|補正", norm):
            flags.append("correction")
        ex.events.append(NodeEvent(kind, chosen, flags, slot["kw"]))
    return ex


# ─────────────────────────────────────────
# 標的鍵
# ─────────────────────────────────────────
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_to_int(s: str) -> Optional[int]:
    """國字或阿拉伯數字 → 整數（1~99）。不認得就回 None。"""
    s = (s or "").strip()
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= 99 else None
    if not s or any(ch not in _CN_DIGIT and ch != "十" for ch in s):
        return None
    if "十" not in s:
        return _CN_DIGIT[s] if len(s) == 1 and _CN_DIGIT[s] > 0 else None
    left, _, right = s.partition("十")
    if "十" in right:
        return None
    tens = 1 if left == "" else _CN_DIGIT.get(left) if len(left) == 1 else None
    ones = 0 if right == "" else _CN_DIGIT.get(right) if len(right) == 1 else None
    if tens is None or ones is None or tens == 0:
        return None
    return tens * 10 + ones


_NUM = r"(?:\d{1,2}|[零〇一二兩三四五六七八九十]{1,3})"
_SERIES_CLUSTER_RE = re.compile(rf"第{_NUM}次(?:(?:及|暨|和|、|與)第?{_NUM}次)*")
_SERIES_NUM_RE = re.compile(rf"第({_NUM})次|(?:及|暨|和|、|與)第?({_NUM})次")
_BOND_CODE_RE = re.compile(r"(?:代碼|代號)[::]\s*([0-9A-Za-z]{5,6})")


@dataclass
class TargetRef:
    key: str
    code: str
    scope: str            # 國內 / 海外
    private: bool
    series: int
    scope_inferred: bool
    basis: str            # title | bond_code


@dataclass
class KeyResult:
    refs: List[TargetRef] = field(default_factory=list)
    reason: str = ""      # refs 為空時的原因


def _scope_of(norm: str) -> Tuple[str, bool, bool]:
    private = "私募" in norm
    if re.search(r"海外|國外", norm):
        return "海外", private, False
    if re.search(r"國內|境內", norm):
        return "國內", private, False
    return "國內", private, True


def make_key(code: str, scope: str, private: bool, series: int) -> str:
    return f"{code}:{scope}{'私募' if private else ''}第{series}次"


def resolve_targets(code: str, name: str, text: str) -> KeyResult:
    """這則公告對應哪幾檔可轉債。指不到特定一檔就回空（附原因），絕不猜。"""
    norm = extract_title(text)
    res = KeyResult()
    if not code:
        res.reason = "沒有公司代號"
        return res

    # 他公司：標題裡的「XXX股份有限公司」與 #名稱 完全對不上
    core = re.sub(r"(?:-KY|-創|-DR|\*)+$", "", name or "")
    legal = " ".join(_LEGAL_NAME_RE.findall(norm))
    if legal and core and core[:2] not in legal:
        res.reason = f"標題的公司（{legal}）與 #{name} 不符，疑似他公司公告"
        return res

    scope, private, inferred = _scope_of(norm)
    series: List[int] = []
    for cluster in _SERIES_CLUSTER_RE.finditer(norm):
        tail = norm[cluster.end():cluster.end() + 18]
        if not re.match(r"(?:(?!修正|變更|修訂|調整|更正)[^,。;]){0,8}?(?:轉換公司債|可轉債|公司債)", tail):
            continue   # 不是「第N次…公司債」（例如「第二次修正…公司債發行辦法」）
        for m in _SERIES_NUM_RE.finditer(cluster.group(0)):
            n = cn_to_int(m.group(1) or m.group(2))
            if n and n not in series:
                series.append(n)
    if series:
        basis = "title"
        cm = _BOND_CODE_RE.search(norm)
        if cm:
            bc = cm.group(1)
            if len(bc) == 5 and bc[:4] == code and bc[4].isdigit() and int(bc[4]) not in series and len(series) == 1:
                res.reason = f"標題期別 {series} 與債券代碼 {bc} 不一致，不納入追蹤"
                return res
    else:
        cm = _BOND_CODE_RE.search(norm)
        bc = cm.group(1) if cm else ""
        if len(bc) == 5 and bc[:4] == code and bc[4].isdigit() and bc[4] != "0":
            series = [int(bc[4])]
            basis = "bond_code"
            inferred = True if not re.search(r"國內|境內|海外|國外", norm) else inferred
        else:
            res.reason = "標題沒有「第N次」且無可用的債券代碼，無法指到特定一檔"
            return res

    for n in series:
        res.refs.append(TargetRef(make_key(code, scope, private, n), code, scope, private, n, inferred, basis))
    return res


# ─────────────────────────────────────────
# 公告識別與時間
# ─────────────────────────────────────────
def published_iso(text: str, fallback_cell: str = "") -> Optional[str]:
    """公告發佈時間 → 台北時區 ISO。找不到再用 CSV 的 date 欄；都沒有回 None。"""
    m = PUBLISH_TIME_RE.search(text or "")
    if m:
        d, hh, mm, ss = m.groups()
        try:
            dt = datetime(int(d[:4]), int(d[4:6]), int(d[6:]), int(hh), int(mm), int(ss), tzinfo=TW)
            return dt.isoformat()
        except ValueError:
            return None
    cell = (fallback_cell or "").strip()
    m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", cell)
    if m2:
        try:
            y, mo, d, hh, mm, ss = (int(x) for x in m2.groups())
            return datetime(y, mo, d, hh, mm, ss, tzinfo=TW).isoformat()
        except ValueError:
            return None
    return None


def announcement_id(code: str, text: str, pub: Optional[str]) -> str:
    stamp = re.sub(r"\D", "", (pub or ""))[:14] or "nots"
    digest = hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()[:6]
    return f"{code}-{stamp}-{digest}"


# ─────────────────────────────────────────
# 儲存與累積
# ─────────────────────────────────────────
class ProgressError(Exception):
    pass


def _new_target(ref: TargetRef, name: str) -> Dict[str, Any]:
    return {
        "code": ref.code,
        "name": name,
        "scope": ref.scope,
        "private": ref.private,
        "series": ref.series,
        "scope_inferred": ref.scope_inferred,
        "key_basis": ref.basis,
        "nodes": {},
        "flags": {},
    }


def _entry_sort_key(e: Dict[str, Any]) -> Tuple[str, str]:
    # 發佈時間較新的優先；同時間用公告 id 讓結果固定（不依賴處理順序）
    return (e.get("published") or "", e.get("ann") or "")


def derive_node(node: Dict[str, Any]) -> Dict[str, Any]:
    """由 history 推導目前採用的日期。回傳 {date, source, published, conflict, superseded}。"""
    dated = [h for h in node.get("history", []) if h.get("date")]
    if not dated:
        return {"date": None, "source": None, "published": None, "conflict": False, "superseded": []}
    best = max(dated, key=_entry_sort_key)
    others = sorted({(h["date"], h["ann"]) for h in dated if h["date"] != best["date"]})
    return {
        "date": best["date"],
        "source": best["ann"],
        "published": best.get("published"),
        "conflict": bool(others),
        "superseded": [{"date": d, "ann": a} for d, a in others],
    }


@dataclass
class Summary:
    key: str
    name: str
    stage_kind: Optional[str]
    nodes: Dict[str, Dict[str, Any]]      # kind -> derive_node 結果 + evidence 數
    missing_before_stage: List[str]
    undated_evidence: List[str]           # 有公告但沒日期的節點
    cancel_notice: bool
    scope_inferred: bool


def summarize_target(key: str, target: Dict[str, Any]) -> Summary:
    nodes: Dict[str, Dict[str, Any]] = {}
    for kind in ALL_NODES:
        raw = target.get("nodes", {}).get(kind, {"history": [], "evidence": []})
        d = derive_node(raw)
        d["evidence_n"] = len(raw.get("evidence", []))
        nodes[kind] = d
    dated_kinds = [k for k in NODE_ORDER if nodes[k]["date"]]
    stage = dated_kinds[-1] if dated_kinds else None
    missing: List[str] = []
    if stage:
        missing = [k for k in NODE_ORDER[:NODE_ORDER.index(stage)] if not nodes[k]["date"]]
    undated = [k for k in ALL_NODES if not nodes[k]["date"] and nodes[k]["evidence_n"] > 0]
    return Summary(
        key=key,
        name=target.get("name", ""),
        stage_kind=stage,
        nodes=nodes,
        missing_before_stage=missing,
        undated_evidence=undated,
        cancel_notice=bool(target.get("flags", {}).get("cancel_notice")),
        scope_inferred=bool(target.get("scope_inferred")),
    )


class ProgressStore:
    """reports/issuance_progress.json 的記憶體副本。load → apply → save。"""

    def __init__(self, path: str = DEFAULT_STORE_PATH):
        self.path = path
        self.data: Dict[str, Any] = {"version": STORE_VERSION, "updated_at": None, "processed_dates": [], "targets": {}}
        self.logs: List[str] = []
        self.stats: Dict[str, int] = {}
        self.load_error: str = ""

    # ── 讀寫 ──
    def _log(self, msg: str) -> None:
        self.logs.append(msg)

    def _bump(self, name: str, n: int = 1) -> None:
        self.stats[name] = self.stats.get(name, 0) + n

    @classmethod
    def load(cls, path: Optional[str] = None) -> "ProgressStore":
        store = cls(path or os.environ.get("ISSUANCE_PROGRESS_PATH") or DEFAULT_STORE_PATH)
        if not os.path.exists(store.path):
            store._log(f"ℹ️ 尚無進度紀錄檔 {store.path}，從空白開始")
            return store
        try:
            with open(store.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
                raise ValueError("結構不符（缺 targets）")
            if data.get("version") != STORE_VERSION:
                raise ValueError(f"版本 {data.get('version')!r} 不是 {STORE_VERSION}")
            data.setdefault("processed_dates", [])
            store.data = data
        except (OSError, ValueError) as e:
            # 絕不在這裡直接覆蓋壞檔：先備份，之後 save 才不會把唯一的資料蓋掉
            backup = store.path + ".corrupt"
            try:
                shutil.copyfile(store.path, backup)
                note = f"已備份為 {backup}"
            except OSError as be:
                note = f"備份也失敗（{be}）"
            store.load_error = f"進度紀錄檔無法讀取（{e}），{note}；本次從空白開始"
            store._log(f"::error title=進度紀錄檔損毀::{store.load_error}")
        return store

    def save(self) -> None:
        """原子寫入（先寫暫存檔再 replace）。失敗拋 ProgressError，由呼叫端決定如何呈現。"""
        self.data["updated_at"] = datetime.now(TW).isoformat(timespec="seconds")
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1, sort_keys=True)
                f.write("\n")
            os.replace(tmp, self.path)
        except OSError as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            raise ProgressError(f"寫入 {self.path} 失敗：{e}") from e

    # ── 累積 ──
    @property
    def targets(self) -> Dict[str, Any]:
        return self.data["targets"]

    def apply_row(self, row: Dict[str, Any]) -> None:
        text = str(row.get("text", "") or "")
        cm = COMPANY_RE.search(text)
        if not cm:
            return
        code, name = cm.group(1), cm.group(2)
        ex = extract_events(text)
        if ex.skipped:
            if ex.skipped.startswith("代子公司"):
                self._bump("skipped_other_issuer")
                self._log(f"↪ {code} {name}：{ex.skipped}")
            return
        if not ex.events and not ex.negated:
            return   # 可轉債公告，但不是發行流程的節點（贖回、注意交易…）

        pub = published_iso(text, str(row.get("date", "") or ""))
        ann = announcement_id(code, text, pub)
        kr = resolve_targets(code, name, text)
        if not kr.refs:
            self._bump("unassigned")
            self._log(f"⚠️ {code} {name}：有節點公告但無法指到特定一檔（{kr.reason}）→ 未納入追蹤 [{ann}]")
            return

        for note in ex.notes:
            self._log(f"ℹ️ {code} {name} [{ann}]：{note}")
        for ref in kr.refs:
            tgt = self.targets.get(ref.key)
            if tgt is None:
                if not ex.events:
                    # 只有「取消」類字樣、而且我們從沒追蹤過它：不替它建紀錄
                    self._log(f"⚠️ {ref.key}：偵測到取消／撤回字樣（{','.join(ex.negated)}），但尚無進度紀錄 [{ann}]")
                    continue
                tgt = _new_target(ref, name)
                self.targets[ref.key] = tgt
                self._bump("targets_created")
            tgt["name"] = name or tgt.get("name", "")
            if ex.negated:
                flags = tgt.setdefault("flags", {})
                flags["cancel_notice"] = {"ann": ann, "published": pub, "nodes": sorted(set(ex.negated))}
                self._log(f"⚠️ {ref.key}：偵測到取消／撤回字樣（{','.join(ex.negated)}），未計入節點，請人工確認 [{ann}]")
            for ev in ex.events:
                self._apply_event(ref.key, tgt, ev, ann, pub, ex)

    def _apply_event(self, key: str, tgt: Dict[str, Any], ev: NodeEvent, ann: str, pub: Optional[str], ex: Extraction) -> None:
        node = tgt["nodes"].setdefault(ev.kind, {"history": [], "evidence": []})
        before = derive_node(node)

        if ev.date:
            if not any(h["ann"] == ann for h in node["history"]):
                entry: Dict[str, Any] = {"date": ev.date, "ann": ann, "published": pub}
                if ev.flags:
                    entry["flags"] = sorted(set(ev.flags))
                if ex.ref_dates:
                    entry["ref_dates"] = sorted(set(ex.ref_dates))
                node["history"].append(entry)
                node["history"].sort(key=_entry_sort_key)   # 固定順序：處理順序不同也得到同樣的檔案內容
        if not any(e["ann"] == ann for e in node["evidence"]):
            node["evidence"].append({"ann": ann, "published": pub, "has_date": bool(ev.date), "ref_dates": sorted(set(ex.ref_dates))})
            node["evidence"] = sorted(node["evidence"], key=_entry_sort_key)[-EVIDENCE_KEEP:]

        after = derive_node(node)
        if after["date"] != before["date"]:
            if before["date"] is None:
                self._bump("dates_recorded")
                self._log(f"✅ {key} {NODE_LABEL[ev.kind]}：{after['date']}（來源 {after['source']}）")
            else:
                self._bump("dates_changed")
                self._log(
                    f"⚠️ 節點日期衝突 {key} {NODE_LABEL[ev.kind]}：舊 {before['date']}（{before['source']}）"
                    f" → 以較新公告為準 {after['date']}（{after['source']}），舊值保留於 history"
                )
        elif ev.date and ev.date != after["date"]:
            self._bump("dates_ignored_older")
            self._log(
                f"⚠️ 節點日期衝突 {key} {NODE_LABEL[ev.kind]}：較舊公告 {ev.date}（{ann}）"
                f"不採用，維持 {after['date']}（{after['source']}）"
            )

    def apply_rows(self, rows: Iterable[Dict[str, Any]]) -> None:
        for row in rows:
            self.apply_row(row)

    # ── 涵蓋範圍（資料可能不全的判斷）──
    PROCESSED_KEEP = 120

    def mark_processed(self, report_date: str, row_count: int) -> bool:
        """
        記下「這個報告日期的公告已納入」。0 則公告不記：頻道當天不可能完全沒有公告，0 則多半是抓取失敗，
        記下去會把缺口藏起來（fail-closed：寧可顯示缺口）。
        """
        if row_count <= 0 or not re.fullmatch(r"\d{8}", report_date or ""):
            return False
        days = set(self.data.get("processed_dates", []))
        days.add(report_date)
        self.data["processed_dates"] = sorted(days)[-self.PROCESSED_KEEP:]
        return True

    def coverage_line(self, report_date: str, title_only: bool) -> str:
        """
        資料可能不全時回傳「一行」警示，否則回空字串。原因（可並列）：
          - 來源只有標題：這批公告沒有任何「說明：」內文，節點日期多半抓不到。
          - 進度紀錄有缺日：上次處理的日期到 report_date 之間有天數沒納入（當天 daily 未執行、抓取失敗或沒存檔）。
        """
        reasons: List[str] = []
        if title_only:
            reasons.append("來源只有公告標題（無內文），節點日期多半無法取得")
        days = sorted(set(self.data.get("processed_dates", [])))
        prior = [d for d in days if d < report_date]
        missing: List[str] = []
        if prior:
            try:
                cur = datetime.strptime(prior[-1], "%Y%m%d").date()
                end = datetime.strptime(report_date, "%Y%m%d").date()
                while True:
                    cur = cur.fromordinal(cur.toordinal() + 1)
                    if cur >= end:
                        break
                    if cur.strftime("%Y%m%d") not in days:
                        missing.append(cur.strftime("%m/%d"))
            except ValueError:
                pass
        if missing:
            shown = "、".join(missing[:5]) + (f" 等 {len(missing)} 天" if len(missing) > 5 else f" 共 {len(missing)} 天")
            reasons.append(f"進度紀錄缺 {shown}（當天 daily 未執行或沒存檔），期間公告未納入")
        if not reasons:
            return ""
        since = days[0] if days else report_date
        reasons.append(f"紀錄自 {since[:4]}-{since[4:6]}-{since[6:]} 起累積，更早的公告未納入")
        return "⚠️ 資料可能不全：" + "；".join(reasons)

    # ── 查詢 ──
    def summaries_for(self, row_text: str) -> Tuple[List[Summary], str]:
        """這則公告對應的標的進度。回傳 (summaries, 原因)；沒有時 summaries 為空、原因說明為何。"""
        cm = COMPANY_RE.search(row_text or "")
        if not cm:
            return [], "沒有公司代號"
        code, name = cm.group(1), cm.group(2)
        ex = extract_events(row_text)
        if ex.skipped.startswith("代子公司"):
            return [], ex.skipped
        kr = resolve_targets(code, name, row_text)
        if not kr.refs:
            return [], kr.reason
        out = [summarize_target(r.key, self.targets[r.key]) for r in kr.refs if r.key in self.targets]
        if not out:
            return [], "尚無此檔的進度紀錄"
        return out, ""

    def summaries_for_company(self, code: str) -> List[Summary]:
        return [summarize_target(k, t) for k, t in sorted(self.targets.items()) if t.get("code") == code]


# ─────────────────────────────────────────
# 顯示（格式沿用「🔎 公司情況研判」：emoji 標題＋縮排兩格的欄位）
# ─────────────────────────────────────────
def _series_label(key: str) -> str:
    return key.split(":", 1)[1] if ":" in key else key


def format_summary_lines(s: Summary) -> List[str]:
    stage = f"{NODE_LABEL[s.stage_kind]}" if s.stage_kind else NOT_DETECTED
    if s.stage_kind and s.missing_before_stage:
        miss = "、".join(NODE_LABEL[k] for k in s.missing_before_stage)
        stage += f"（⚠️ 資料不全：缺 {miss}）"
    lines = [f"📈 發行進度（{_series_label(s.key)}）："]
    lines.append(f"  目前階段：{stage}")
    lines.append(f"  送件日：{s.nodes['filing']['date'] or NOT_DETECTED}")
    others = [f"{NODE_SHORT[k]} {s.nodes[k]['date'] or NOT_DETECTED}" for k in NODE_ORDER if k != "filing"]
    lines.append("  節點：" + "｜".join(others))
    info = [f"{NODE_SHORT[k]} {s.nodes[k]['date'] or NOT_DETECTED}" for k in INFO_NODES]
    lines.append("  其他節點（不影響階段）：" + "｜".join(info))
    conflicts = [NODE_LABEL[k] for k in ALL_NODES if s.nodes[k]["conflict"]]
    if conflicts:
        lines.append("  ⚠️ 日期曾被更正／衝突（已採較新公告）：" + "、".join(conflicts))
    if s.undated_evidence:
        lines.append("  ℹ 已有公告但日期未揭露：" + "、".join(NODE_LABEL[k] for k in s.undated_evidence))
    if s.cancel_notice:
        lines.append("  ⚠️ 偵測到取消／撤回字樣，請人工確認")
    if s.scope_inferred:
        lines.append("  ℹ 標題未標示國內／海外，依慣例視為國內")
    return lines


def format_progress_block(store: Optional[ProgressStore], row_text: str, error: str = "") -> List[str]:
    """放進單則公告的顯示區塊。沒有進度時也要明講（fail-closed），不留白。"""
    if error:
        return ["📈 發行進度：⚠️ 追蹤功能異常，本次未顯示（見 Actions log）"]
    if store is None:
        return []
    summaries, why = store.summaries_for(row_text)
    if not summaries:
        return [f"📈 發行進度：{NOT_DETECTED}（{why}）"]
    lines: List[str] = []
    for s in summaries:
        lines.extend(format_summary_lines(s))
    return lines
