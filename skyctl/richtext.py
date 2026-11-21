# Copyright 2026 The skyctl Authors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
skyctl.richtext
===============
Pure text helpers: grapheme counting, length validation, facet detection
(links, mentions, hashtags) and thread splitting. Nothing here touches
the network.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_GRAPHEMES = 300


class PostTooLongError(ValueError):
    """Raised when post text exceeds the Bluesky grapheme limit."""


# ----------------------------------------------------------------------
# Graphemes
# ----------------------------------------------------------------------

_ZWJ = "‍"


def _extends_cluster(ch: str) -> bool:
    cp = ord(ch)
    return (
        unicodedata.category(ch) in ("Mn", "Me", "Mc")
        or 0xFE00 <= cp <= 0xFE0F  # variation selectors
        or 0x1F3FB <= cp <= 0x1F3FF  # emoji skin tones
        or 0xE0020 <= cp <= 0xE007F  # tag characters (flag sequences)
        or ch == _ZWJ
    )


def _fallback_graphemes(text: str) -> list[str]:
    """Approximate grapheme clustering for Pythons without iter_graphemes."""
    clusters: list[str] = []
    for ch in text:
        if clusters:
            prev = clusters[-1]
            is_ri = 0x1F1E6 <= ord(ch) <= 0x1F1FF
            prev_single_ri = len(prev) == 1 and 0x1F1E6 <= ord(prev) <= 0x1F1FF
            if (
                _extends_cluster(ch)
                or prev.endswith(_ZWJ)
                or (is_ri and prev_single_ri)
                or (prev == "\r" and ch == "\n")
            ):
                clusters[-1] = prev + ch
                continue
        clusters.append(ch)
    return clusters


def graphemes(text: str) -> list[str]:
    """Split text into user-perceived characters."""
    iter_graphemes = getattr(unicodedata, "iter_graphemes", None)
    if iter_graphemes is not None:
        return [str(g) for g in iter_graphemes(text)]
    return _fallback_graphemes(text)


def grapheme_len(text: str) -> int:
    return len(graphemes(text))


def validate_length(text: str, limit: int = MAX_GRAPHEMES) -> None:
    """Raise PostTooLongError if text is over the limit."""
    n = grapheme_len(text)
    if n > limit:
        raise PostTooLongError(f"Post is {n} graphemes; the limit is {limit}. Shorten it or send it as a thread.")


# ----------------------------------------------------------------------
# Facets
# ----------------------------------------------------------------------


@dataclass
class Facet:
    """A detected link, mention or hashtag with UTF-8 byte offsets."""

    kind: str  # "link" | "mention" | "tag"
    value: str  # URL, handle (no @) or tag (no #)
    text: str  # The matched text as written
    byte_start: int
    byte_end: int


_BOUNDARY = r"(?:^|(?<=[\s(]))"
_URL_RE = re.compile(_BOUNDARY + r"https?://[^\s]+")
_MENTION_RE = re.compile(
    _BOUNDARY + r"@((?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"[a-zA-Z](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
)
_TAG_RE = re.compile(r"(?:^|(?<=\s))#(\S+)")
_TRAILING_PUNCT = ".,;:!?'\"”’"


def _byte_offset(text: str, index: int) -> int:
    return len(text[:index].encode("utf-8"))


def _trim_url(url: str) -> str:
    while url:
        if url[-1] in _TRAILING_PUNCT or (url[-1] == ")" and url.count("(") < url.count(")")):
            url = url[:-1]
        else:
            break
    return url


def detect_facets(text: str) -> list[Facet]:
    """Find links, mentions and hashtags. Returned in order of appearance."""
    found: list[tuple[int, int, str, str]] = []  # (start, end, kind, value)

    for m in _URL_RE.finditer(text):
        url = _trim_url(m.group(0))
        if len(url) > len("https://"):
            found.append((m.start(), m.start() + len(url), "link", url))

    for m in _MENTION_RE.finditer(text):
        found.append((m.start(), m.end(), "mention", m.group(1)))

    for m in _TAG_RE.finditer(text):
        tag = m.group(1).rstrip(_TRAILING_PUNCT + ")")
        if not tag or tag.isdigit() or len(tag) > 64:
            continue
        found.append((m.start(), m.start() + 1 + len(tag), "tag", tag))

    found.sort()
    facets: list[Facet] = []
    last_end = -1
    for start, end, kind, value in found:
        if start < last_end:  # overlaps an earlier facet (e.g. # inside a URL)
            continue
        facets.append(
            Facet(
                kind=kind,
                value=value,
                text=text[start:end],
                byte_start=_byte_offset(text, start),
                byte_end=_byte_offset(text, end),
            )
        )
        last_end = end
    return facets


# ----------------------------------------------------------------------
# Threads
# ----------------------------------------------------------------------


def split_thread(text: str, limit: int = MAX_GRAPHEMES) -> list[str]:
    """
    Split text into parts of at most `limit` graphemes, breaking on
    whitespace where possible. Words longer than the limit are hard-split.
    """
    parts: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if grapheme_len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current = ""
        chunk = graphemes(word)
        while len(chunk) > limit:
            parts.append("".join(chunk[:limit]))
            chunk = chunk[limit:]
        current = "".join(chunk)
    if current:
        parts.append(current)
    return parts
