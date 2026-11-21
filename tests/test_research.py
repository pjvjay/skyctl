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

"""Research: news search adapters, SSRF-safe fetching, thread reading, and citation grounding."""

import json

import httpx
import pytest

from skyctl.research import (
    BraveSearch,
    GdeltSearch,
    NoSearch,
    ResearchError,
    SafeFetcher,
    SearchHit,
    StaticFetcher,
    StaticSearch,
    TavilySearch,
    UnsafeURLError,
    check_url,
    html_to_text,
    search_for,
)

PUBLIC = lambda host: ["93.184.216.34"]


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ----------------------------------------------------------------------
# Search adapters (documented response shapes; no live network here)
# ----------------------------------------------------------------------


def test_gdelt_parses_articles_and_sends_timespan():
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "articles": [
                    {
                        "url": "https://a.example/1",
                        "title": "Story",
                        "seendate": "20260301T120000Z",
                        "domain": "a.example",
                    }
                ]
            },
        )

    hits = GdeltSearch(client=mock_client(handler)).search("transit strike", limit=3, recency_days=2)
    assert hits == [SearchHit("Story", "https://a.example/1", "", "a.example", "2026-03-01T12:00:00+00:00")]
    assert seen["timespan"] == "2d" and seen["mode"] == "artlist" and "sourcelang:english" in seen["query"]


def test_gdelt_plain_text_error_is_reported():
    bad = GdeltSearch(client=mock_client(lambda r: httpx.Response(200, text="The specified phrase is too short.")))
    with pytest.raises(ResearchError, match="too short"):
        bad.search("x")


def test_brave_and_tavily(monkeypatch):
    def brave(request):
        assert request.headers["X-Subscription-Token"] == "bk" and request.url.params["freshness"] == "pw"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "B",
                        "url": "https://b.example/x",
                        "description": "<strong>Hi</strong> &amp; bye",
                        "page_age": "2026-03-01",
                        "meta_url": {"hostname": "b.example"},
                    }
                ]
            },
        )

    [b] = BraveSearch(api_key="bk", client=mock_client(brave)).search("q", recency_days=5)
    assert (b.snippet, b.source, b.published_at) == ("Hi & bye", "b.example", "2026-03-01")

    def tavily(request):
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer tk" and body["topic"] == "news" and body["days"] == 3
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "T", "url": "https://www.t.example/y", "content": "c", "published_date": "2026-03-02"}
                ]
            },
        )

    [t] = TavilySearch(api_key="tk", client=mock_client(tavily)).search("q", recency_days=3)
    assert t.source == "t.example"
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    with pytest.raises(ResearchError, match="BRAVE_API_KEY"):
        BraveSearch()
    with pytest.raises(ResearchError, match="HTTP 500"):
        TavilySearch(api_key="k", client=mock_client(lambda r: httpx.Response(500))).search("q")


def test_search_factory_and_static(monkeypatch):
    assert isinstance(search_for("gdelt://"), GdeltSearch)
    assert isinstance(search_for(""), GdeltSearch)  # default: free, no key
    with pytest.raises(ResearchError):
        NoSearch().search("x")
    assert isinstance(search_for("none"), NoSearch)
    with pytest.raises(ResearchError, match="Unknown search provider"):
        search_for("bing://")
    s = StaticSearch(
        [SearchHit("Strike enters day four", "https://n.example/a"), SearchHit("Weather", "https://n.example/b")]
    )
    assert [h.url for h in s.search("strike day")] == ["https://n.example/a"]


# ----------------------------------------------------------------------
# SSRF protection
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "resolves"),
    [
        ("http://169.254.169.254/latest/", ["93.184.216.34"]),  # IP literal: never trust a resolver
        ("http://[::1]:8080/", ["93.184.216.34"]),
        ("http://169.254.169.254/latest/meta-data/", ["169.254.169.254"]),  # AWS/Azure/GCP metadata (link-local)
        ("http://metadata.google.internal/computeMetadata/v1/", ["169.254.169.254"]),
        ("http://localhost:8080/healthz", ["127.0.0.1"]),
        ("http://intranet.example/", ["10.0.0.5"]),
        ("http://v6.example/", ["::1"]),
        ("http://mapped.example/", ["::ffff:127.0.0.1"]),
        ("http://rebind.example/", ["93.184.216.34", "192.168.1.1"]),  # any private answer is refused
        ("file:///etc/passwd", ["127.0.0.1"]),
        ("gopher://x.example/", ["93.184.216.34"]),
    ],
)
def test_unsafe_urls_are_refused(url, resolves):
    with pytest.raises(UnsafeURLError):
        check_url(url, resolve=lambda h: resolves)


def test_redirect_to_internal_address_is_refused():
    def handler(request):
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/iam/"})
        return httpx.Response(200, text="SECRET")

    resolve = lambda h: ["169.254.169.254"] if h.startswith("169.") else ["93.184.216.34"]
    with pytest.raises(UnsafeURLError):
        SafeFetcher(client=mock_client(handler), resolve=resolve).fetch("http://public.example/story")


def test_safe_fetch_reads_text_and_limits():
    page = (
        "<html><head><title>Forum &amp; more</title><script>evil()</script></head><body><nav>menu</nav>"
        "<p>The forum goes ahead.</p><p>" + "x" * 50 + "</p></body></html>"
    )
    fetcher = SafeFetcher(
        client=mock_client(lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})),
        resolve=PUBLIC,
        max_chars=40,
    )
    out = fetcher.fetch("https://news.example/a")
    assert out["title"] == "Forum & more" and out["text"].startswith("The forum goes ahead.")
    assert "evil" not in out["text"] and "menu" not in out["text"] and out["truncated"] is True
    pdf = SafeFetcher(
        client=mock_client(lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})),
        resolve=PUBLIC,
    )
    with pytest.raises(ResearchError, match="not a readable page"):
        pdf.fetch("https://news.example/a.pdf")


def test_safe_fetch_stops_reading_at_the_byte_cap():
    served = []

    def endless():
        for _ in range(10_000):  # would be 10 MB if read to the end
            served.append(1)
            yield b"<p>" + b"y" * 1020 + b"</p>"

    fetcher = SafeFetcher(
        client=mock_client(lambda r: httpx.Response(200, content=endless(), headers={"content-type": "text/html"})),
        resolve=PUBLIC,
        max_bytes=4096,
    )
    out = fetcher.fetch("https://news.example/huge")
    assert len(served) < 10 and out["text"].startswith("y")


def test_html_to_text():
    title, text = html_to_text("<title> T </title><p>a<br>b</p><style>x{}</style>")
    assert title == "T" and text.splitlines()[0].strip() == "a" and "x{}" not in text


def test_link_card_fetch_is_ssrf_safe(monkeypatch):
    from skyctl.client import fetch_link_metadata

    called = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: called.append(a) or httpx.Response(200, text="SECRET"))
    meta = fetch_link_metadata("http://169.254.169.254/latest/meta-data/")
    assert called == [] and meta["title"] == "http://169.254.169.254/latest/meta-data/"


# ----------------------------------------------------------------------
# Tools and citation grounding
# ----------------------------------------------------------------------


@pytest.fixture
def world_ctx():
    from skyctl import BskyManager
    from skyctl.models import AccountConfig
    from skyctl.testing.world import World
    from skyctl.tools import ToolContext, ToolPolicy

    world = World()
    world.add_post("city.test", "Strike update", key="topic")
    world.reply("topic", "a.test", "Buses are running normally!", key="false")
    world.reply("false", "b.test", "No they're not.", key="nested")
    world.add_article("https://news.example/strike", "Strike day four", "No buses are running.")
    m = BskyManager({"work": AccountConfig("work", "desk.test", "pw")}, atproto_factory=world.client_factory)
    ctx = ToolContext(m, policy=ToolPolicy(allow_writes=True)).use_research(*world.research())
    return world, ctx


def test_thread_search_and_fetch_tools(world_ctx):
    from skyctl.tools import registry

    world, ctx = world_ctx
    thread = registry.call("get_post_thread", {"account": "work", "uri": world.uri("topic")}, ctx).result
    assert thread["note"].startswith("Content fetched")
    replies = thread["data"]["replies"]
    assert [(r["author"], r["depth"], r["reply_to"]) for r in replies] == [
        ("a.test", 1, world.uri("topic")),
        ("b.test", 2, world.uri("false")),
    ]
    assert world.uri("false") in ctx.seen_uris
    found = registry.call("web_search", {"query": "strike buses"}, ctx).result["data"]
    assert found["provider"] == "static" and found["results"][0]["url"] == "https://news.example/strike"
    assert "https://news.example/strike" in ctx.seen_urls
    page = registry.call("fetch_page", {"url": "https://news.example/strike"}, ctx)
    assert page.ok and page.result["data"]["text"] == "No buses are running."
    blocked = registry.call("fetch_page", {"url": "http://169.254.169.254/"}, ctx)
    assert not blocked.ok and "non-public" in blocked.error
    missing = registry.call(
        "get_post_thread", {"account": "work", "uri": "at://did:plc:x/app.bsky.feed.post/nope"}, ctx
    )
    assert not missing.ok


def test_citation_grounding(world_ctx):
    from skyctl.planning import Plan, check_plan
    from skyctl.tools import registry

    world, ctx = world_ctx
    registry.call("get_post_thread", {"account": "work", "uri": world.uri("topic")}, ctx)
    ctx.link_grounding, ctx.require_citations = "error", True

    def plan(text):
        return Plan.model_validate(
            {
                "goal": "g",
                "steps": [
                    {
                        "id": "r",
                        "tool": "reply",
                        "args": {"account": "work", "parent_uri": world.uri("false"), "text": text},
                    }
                ],
            }
        )

    uncited = check_plan(plan("That's wrong."), ctx, registry)
    assert any("cite a source" in f.message for f in uncited.errors)
    invented = check_plan(plan("Wrong, see https://made-up.example/x"), ctx, registry)
    assert any("was not retrieved" in f.message for f in invented.errors)
    registry.call("web_search", {"query": "strike"}, ctx)
    assert check_plan(plan("No buses are running: https://news.example/strike"), ctx, registry).ok
    ctx.trusted_urls = {"https://user-supplied.example/page"}  # links from the user's own request
    ctx.require_citations = False
    assert check_plan(plan("See https://user-supplied.example/page."), ctx, registry).ok
    # Not re-applied by the executor: citations are a property of the proposal.
    assert check_plan(plan("Wrong, see https://made-up.example/x"), ctx, registry, proposal=False).ok


def test_world_static_fetcher_keeps_the_url_policy():
    with pytest.raises(UnsafeURLError):
        StaticFetcher({}).fetch("file:///etc/passwd")
    with pytest.raises(ResearchError, match="404"):
        StaticFetcher({}).fetch("https://news.example/missing")
