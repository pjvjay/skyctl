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
skyctl.research
===============
Finding out what is true *now*: web/news search and safe page fetching, so
an agent can ground what it publishes in recent, citable sources.

    SKYCTL_SEARCH_URL   gdelt:// (default; free, no key) | brave:// (BRAVE_API_KEY)
                        | tavily:// (TAVILY_API_KEY) | none

Page fetching is SSRF-safe: only http(s), and every hop of every redirect
must resolve to a public address, so a prompt-injected "read
http://169.254.169.254/…" cannot reach cloud metadata or internal services.
"""

from __future__ import annotations

import html as html_lib
import ipaddress
import os
import re
import socket
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urljoin, urlparse


def _user_agent() -> str:
    from . import __version__

    return f"skyctl/{__version__} (research; +https://pypi.org/project/skyctl/)"


USER_AGENT = _user_agent()
MAX_PAGE_BYTES = 2_000_000
MAX_TEXT_CHARS = 12_000
MAX_REDIRECTS = 5


class ResearchError(RuntimeError):
    """A search or fetch could not be completed."""


class UnsafeURLError(ResearchError):
    """The URL points somewhere an agent must not reach (SSRF protection)."""


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    source: str = ""  # domain
    published_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class WebSearch(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 5, recency_days: int | None = 7) -> list[SearchHit]: ...


class PageFetcher(Protocol):
    def fetch(self, url: str) -> dict: ...


# ----------------------------------------------------------------------
# Search adapters
# ----------------------------------------------------------------------


def _http(client):
    if client is not None:
        return client
    import httpx

    return httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


class GdeltSearch:
    """GDELT DOC 2.0 article search: global news, updated every 15 minutes, no API key."""

    name = "gdelt"
    ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

    def __init__(self, client=None, language: str = "english"):
        self._client, self.language = client, language

    def search(self, query: str, *, limit: int = 5, recency_days: int | None = 7) -> list[SearchHit]:
        q = f"{query} sourcelang:{self.language}" if self.language else query
        params = {
            "query": q,
            "mode": "artlist",
            "format": "json",
            "maxrecords": str(max(1, min(limit, 50))),
            "sort": "datedesc",
        }
        if recency_days:
            params["timespan"] = f"{recency_days}d"
        resp = _http(self._client).get(self.ENDPOINT, params=params)
        if resp.status_code >= 400:
            raise ResearchError(f"GDELT returned HTTP {resp.status_code}")
        try:
            articles = resp.json().get("articles", [])
        except ValueError:  # GDELT answers plain text for malformed queries
            raise ResearchError(f"GDELT could not run the query: {resp.text[:200]}") from None
        hits = []
        for a in articles[:limit]:
            seen = a.get("seendate")
            published = None
            if seen:
                try:
                    published = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
                except ValueError:
                    published = None
            hits.append(SearchHit(a.get("title", ""), a["url"], "", a.get("domain") or _domain(a["url"]), published))
        return hits


class BraveSearch:
    """Brave Search News API (BRAVE_API_KEY)."""

    name = "brave"
    ENDPOINT = "https://api.search.brave.com/res/v1/news/search"
    FRESHNESS = ((1, "pd"), (7, "pw"), (31, "pm"), (365, "py"))

    def __init__(self, api_key: str | None = None, client=None):
        self.api_key = api_key or os.getenv("BRAVE_API_KEY", "")
        if not self.api_key:
            raise ResearchError("BRAVE_API_KEY is not set (needed for brave:// search).")
        self._client = client

    def search(self, query: str, *, limit: int = 5, recency_days: int | None = 7) -> list[SearchHit]:
        params = {"q": query, "count": str(max(1, min(limit, 20)))}
        if recency_days:
            params["freshness"] = next((code for days, code in self.FRESHNESS if recency_days <= days), "py")
        resp = _http(self._client).get(
            self.ENDPOINT, params=params, headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"}
        )
        if resp.status_code >= 400:
            raise ResearchError(f"Brave returned HTTP {resp.status_code}")
        return [
            SearchHit(
                r.get("title", ""),
                r["url"],
                html_lib.unescape(re.sub(r"<[^>]+>", "", r.get("description", ""))),
                (r.get("meta_url") or {}).get("hostname") or _domain(r["url"]),
                r.get("page_age"),
            )
            for r in resp.json().get("results", [])[:limit]
        ]


class TavilySearch:
    """Tavily search API, news topic (TAVILY_API_KEY)."""

    name = "tavily"
    ENDPOINT = "https://api.tavily.com/search"

    def __init__(self, api_key: str | None = None, client=None):
        self.api_key = api_key or os.getenv("TAVILY_API_KEY", "")
        if not self.api_key:
            raise ResearchError("TAVILY_API_KEY is not set (needed for tavily:// search).")
        self._client = client

    def search(self, query: str, *, limit: int = 5, recency_days: int | None = 7) -> list[SearchHit]:
        body: dict[str, Any] = {"query": query, "topic": "news", "max_results": max(1, min(limit, 20))}
        if recency_days:
            body["days"] = recency_days
        resp = _http(self._client).post(self.ENDPOINT, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        if resp.status_code >= 400:
            raise ResearchError(f"Tavily returned HTTP {resp.status_code}")
        return [
            SearchHit(
                r.get("title", ""), r["url"], r.get("content", "")[:500], _domain(r["url"]), r.get("published_date")
            )
            for r in resp.json().get("results", [])[:limit]
        ]


class StaticSearch:
    """Fixed results (tests, evaluations, air-gapped demos)."""

    name = "static"

    def __init__(self, hits: list[SearchHit]):
        self.hits = hits

    def search(self, query: str, *, limit: int = 5, recency_days: int | None = 7) -> list[SearchHit]:
        words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
        scored = [(sum(w in f"{h.title} {h.snippet}".lower() for w in words), h) for h in self.hits]
        return [h for score, h in sorted(scored, key=lambda x: -x[0]) if score > 0][:limit]


class NoSearch:
    name = "none"

    def search(self, query: str, *, limit: int = 5, recency_days: int | None = 7) -> list[SearchHit]:
        raise ResearchError("Web search is disabled (SKYCTL_SEARCH_URL=none).")


def search_for(url: str | None = None) -> WebSearch:
    url = (url if url is not None else os.getenv("SKYCTL_SEARCH_URL", "")).strip() or "gdelt://"
    scheme = url.split("://", 1)[0]
    factories: dict[str, Callable[[], WebSearch]] = {
        "gdelt": GdeltSearch,
        "brave": BraveSearch,
        "tavily": TavilySearch,
        "none": NoSearch,
    }
    if scheme not in factories:
        raise ResearchError(f"Unknown search provider {scheme!r}; use gdelt://, brave://, tavily:// or none")
    return factories[scheme]()


# ----------------------------------------------------------------------
# SSRF-safe page fetching
# ----------------------------------------------------------------------


def _public_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def check_url(url: str, resolve: Callable[[str], list[str]] | None = None) -> None:
    """Raise UnsafeURLError unless url is http(s) on a host that resolves only to public addresses."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"only http(s) URLs can be fetched, not {parsed.scheme or 'relative'}:")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    if host.lower() in ("localhost", "metadata.google.internal", "metadata") or host.lower().endswith(
        (".internal", ".local")
    ):
        raise UnsafeURLError(f"{host} is an internal host")
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None:  # an IP literal is checked as-is, never "resolved"
        if not _public_ip(str(literal)):
            raise UnsafeURLError(f"{host} is a non-public address")
        return
    resolve = resolve or (lambda h: sorted({str(info[4][0]) for info in socket.getaddrinfo(h, None)}))
    try:
        addresses = resolve(host)
    except (OSError, UnicodeError) as e:
        raise ResearchError(f"cannot resolve {host}: {e}") from None
    if not addresses:
        raise ResearchError(f"{host} did not resolve")
    bad = [a for a in addresses if not _public_ip(a)]
    if bad:
        raise UnsafeURLError(f"{host} resolves to a non-public address ({bad[0]})")


def html_to_text(page: str) -> tuple[str, str]:
    """(title, readable text) from HTML, without a parser dependency."""
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    title = html_lib.unescape(m.group(1)).strip() if m else ""
    body = re.sub(r"(?is)<head[^>]*>.*?</head>|<title[^>]*>.*?</title>", " ", page)  # not part of the article
    body = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header|form)[^>]*>.*?</\1>", " ", body)
    body = re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n", body)
    text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", body))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return title, text


class SafeFetcher:
    def __init__(
        self,
        client=None,
        resolve: Callable[[str], list[str]] | None = None,
        max_bytes: int = MAX_PAGE_BYTES,
        max_chars: int = MAX_TEXT_CHARS,
    ):
        self._client, self._resolve = client, resolve
        self.max_bytes, self.max_chars = max_bytes, max_chars

    def _client_or_default(self):
        import httpx

        return self._client or httpx.Client(timeout=20, headers={"User-Agent": USER_AGENT}, follow_redirects=False)

    def fetch(self, url: str) -> dict:
        client = self._client_or_default()
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current, self._resolve)  # every hop, not only the first
            request = client.build_request("GET", current)
            resp = client.send(request, stream=True, follow_redirects=False)
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    current = urljoin(current, resp.headers["location"])
                    continue
                return self._read(url, current, resp)
            finally:
                resp.close()
        raise ResearchError("too many redirects")

    def _read(self, url: str, final_url: str, resp) -> dict:
        if resp.status_code >= 400:
            raise ResearchError(f"{final_url} returned HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "text/html").split(";")[0].strip().lower()
        if ctype not in ("text/html", "text/plain", "application/xhtml+xml"):
            raise ResearchError(f"{final_url} is {ctype}, not a readable page")
        body = bytearray()
        for chunk in resp.iter_bytes():  # stop reading at the cap: a huge page cannot exhaust memory
            body.extend(chunk)
            if len(body) >= self.max_bytes:
                break
        raw = bytes(body[: self.max_bytes]).decode(resp.encoding or "utf-8", errors="replace")
        title, text = html_to_text(raw) if ctype != "text/plain" else ("", raw)
        truncated = len(text) > self.max_chars
        return {
            "url": url,
            "final_url": final_url,
            "title": title,
            "text": text[: self.max_chars],
            "truncated": truncated,
        }


class StaticFetcher:
    """Pages from a dict (tests, evaluations)."""

    def __init__(self, pages: dict[str, dict]):
        self.pages = pages

    def fetch(self, url: str) -> dict:
        check_url(url, resolve=lambda h: ["93.184.216.34"])  # same URL policy, no DNS
        if url not in self.pages:
            raise ResearchError(f"{url} returned HTTP 404")
        page = self.pages[url]
        return {
            "url": url,
            "final_url": url,
            "title": page.get("title", ""),
            "text": page.get("text", ""),
            "truncated": False,
        }
