#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram 訊息分段：只在「換行處」切，絕不切在一行中間，也不把同一個案件的兩行拆開。

舊做法是每 4000 個字元硬切，會把一行切成兩半、把案件的第二行（縮排 6 格以上的續行）甩到下一則訊息。
這裡把「一行 + 其後縮排 ≥ 6 格的續行」視為不可分割的一組，再依序裝進不超過上限的訊息。
長度以 UTF-16 code unit 計（Telegram 以此計算上限；emoji 佔 2）。上限預設 4000（Telegram 為 4096，留給「[i/n]」前綴）。

只有「單一不可分割的一組本身就超過上限」時才會再退而求其次：先依行切，單行仍超過才在空白或標點處切，最後才硬切。
"""

from __future__ import annotations

from typing import List

LIMIT = 4000
CONTINUATION_INDENT = " " * 6


def ulen(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _groups(text: str) -> List[str]:
    out: List[List[str]] = []
    for ln in text.split("\n"):
        if out and ln.startswith(CONTINUATION_INDENT):
            out[-1].append(ln)
        else:
            out.append([ln])
    return ["\n".join(g) for g in out]


def _cut_line(line: str, limit: int) -> List[str]:
    """單行超過上限：優先在空白或標點處切，否則硬切（以 Python 字元為單位，不會切壞 surrogate pair）。"""
    pieces: List[str] = []
    while ulen(line) > limit:
        n, used = 0, 0
        for ch in line:
            w = 2 if ord(ch) > 0xFFFF else 1
            if used + w > limit:
                break
            used += w
            n += 1
        cut = n
        window = line[:n]
        for sep in ("\n", " ", "，", "。", "；", "、", "｜", ",", ";"):
            k = window.rfind(sep)
            if k > n // 2:
                cut = k + 1
                break
        pieces.append(line[:cut])
        line = line[cut:]
    if line:
        pieces.append(line)
    return pieces


def split_message(text: str, limit: int = LIMIT) -> List[str]:
    if ulen(text) <= limit:
        return [text]
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0

    def flush() -> None:
        nonlocal cur, cur_len
        body = "\n".join(cur).strip("\n")
        if body.strip():
            chunks.append(body)
        cur, cur_len = [], 0

    for g in _groups(text):
        g_len = ulen(g)
        if g_len > limit:                       # 不可分割的一組本身就放不進一則：依行、再依字切
            flush()
            for ln in g.split("\n"):
                for piece in _cut_line(ln, limit):
                    add = ulen(piece) + (1 if cur else 0)
                    if cur_len + add > limit:
                        flush()
                        add = ulen(piece)
                    cur.append(piece)
                    cur_len += add
            flush()
            continue
        add = g_len + (1 if cur else 0)
        if cur_len + add > limit:
            flush()
            add = g_len
        cur.append(g)
        cur_len += add
    flush()
    return chunks or [text[:limit]]
