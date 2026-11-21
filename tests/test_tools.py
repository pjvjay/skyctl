"""Tool registry, policy and the MCP server built from them."""

import asyncio
import json

import pytest

from skyctl.adapters import MemoryObjectStore, ObjectJobStore
from skyctl.scheduler import BskyScheduler
from skyctl.tools import ToolContext, ToolDenied, ToolPolicy, ToolRegistry, registry


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------

def test_catalogue_marks_mutating_and_destructive_tools():
    names = set(registry.names())
    assert {"list_accounts", "post", "reply", "delete_post", "schedule_post", "preview_post"} <= names
    assert not registry.get("get_timeline").mutating
    assert registry.get("post").mutating and not registry.get("post").destructive
    assert registry.get("delete_post").destructive and registry.get("delete_post").mutating
    assert registry.get("preview_post").account_param is None


def test_schemas_are_consistent_across_formats():
    anth = {t["name"]: t for t in registry.to_anthropic()}
    oai = {t["function"]["name"]: t["function"] for t in registry.to_openai()}
    assert anth.keys() == oai.keys()
    assert anth["post"]["input_schema"] == oai["post"]["parameters"]
    schema = anth["post"]["input_schema"]
    assert schema["required"] == ["account", "text"]
    assert schema["properties"]["account"]["description"].startswith("Account alias")
    assert "title" not in schema
    assert anth["get_timeline"]["input_schema"]["properties"]["limit"]["maximum"] == 100


def test_custom_registry_derives_schema_from_signature():
    reg = ToolRegistry()

    @reg.tool(mutating=True, account_param=None)
    def shout(ctx, text: str, times: int = 1) -> dict:
        """Shout text."""
        return {"out": (text.upper() + " ") * times}

    [spec] = reg.to_anthropic()
    assert spec["description"] == "Shout text."
    assert spec["input_schema"]["properties"]["times"]["default"] == 1
    record = reg.call("shout", {"text": "hi", "times": 2}, ctx=object())
    assert record.ok and record.result == {"out": "HI HI "}
    bad = reg.call("shout", {"text": "hi", "times": "many"}, ctx=object())
    assert not bad.ok and "Invalid arguments" in bad.error
    unknown = reg.call("nope", {}, ctx=object())
    assert not unknown.ok and "Unknown tool" in unknown.error
    with pytest.raises(ValueError):
        reg.register(reg.get("shout"))


# ----------------------------------------------------------------------
# Tools against the fake PDS
# ----------------------------------------------------------------------

def test_read_tools_wrap_network_content_as_untrusted(manager):
    ctx = ToolContext(manager)
    accounts = registry.call("list_accounts", {}, ctx).result
    assert [a["account"] for a in accounts] == ["personal", "work"]
    search = registry.call("search_posts", {"account": "personal", "query": "x"}, ctx)
    assert search.ok and search.result["note"].startswith("Content fetched from the network")
    assert search.result["data"][0]["text"] == "result for x"
    timeline = registry.call("get_timeline", {"account": "personal", "limit": 5}, ctx).result
    assert timeline["data"][0]["author"] == "friend.test"
    notes = registry.call("get_notifications", {"account": "personal"}, ctx).result
    assert notes["data"][0] == {
        "reason": "mention", "author": "fan.test", "text": "hey @me.test, is there a student discount?",
        "uri": "at://did:plc:fan/app.bsky.feed.post/n1", "cid": "cid-n1", "reason_subject": None,
        "indexed_at": "2026-01-01T00:00:00Z", "is_read": False,
    }
    preview = registry.call("preview_post", {"text": "hi @bsky.app #tag"}, ctx).result
    assert preview["fits"] and [f["type"] for f in preview["facets"]] == ["mention", "tag"]
    missing = registry.call("get_timeline", {"account": "nope"}, ctx)
    assert not missing.ok and "not found" in missing.error


def test_write_tools_return_post_results(manager, fake_atproto):
    ctx = ToolContext(manager)
    r = registry.call("post", {"account": "work", "text": "hello"}, ctx)
    assert r.ok and r.result["success"] and r.result["uri"].endswith("/sent1")
    t = registry.call("thread", {"account": "work", "text": "one two"}, ctx)
    assert t.ok and t.result["success"] and len(t.result["posts"]) == 1
    too_long = registry.call("post", {"account": "work", "text": "x" * 301}, ctx)
    assert too_long.ok and too_long.result["success"] is False and "301 graphemes" in too_long.result["error"]


def test_scheduling_tools_use_the_context_scheduler(manager):
    scheduler = BskyScheduler(manager, job_store=ObjectJobStore(MemoryObjectStore()))
    ctx = ToolContext(manager, scheduler=scheduler)
    one = registry.call("schedule_post", {"account": "work", "text": "later", "run_at": "2030-01-01T09:00:00Z"}, ctx)
    assert one.ok and one.result["status"] == "pending"
    cron = registry.call("schedule_cron", {"account": "work", "text": "daily", "cron": "0 9 * * mon-fri"}, ctx)
    assert cron.ok and cron.result["status"] == "recurring"
    bad = registry.call("schedule_cron", {"account": "work", "text": "daily", "cron": "not a cron"}, ctx)
    assert not bad.ok
    bad_time = registry.call("schedule_post", {"account": "work", "text": "x", "run_at": "tomorrow"}, ctx)
    assert not bad_time.ok and "ISO 8601" in bad_time.error
    jobs = registry.call("list_jobs", {}, ctx).result
    assert {j["id"] for j in jobs} == {one.result["job_id"], cron.result["job_id"]}
    cancelled = registry.call("cancel_job", {"job_id": one.result["job_id"]}, ctx).result
    assert cancelled["cancelled"] is True


# ----------------------------------------------------------------------
# Policy
# ----------------------------------------------------------------------

def test_policy_hides_and_denies(manager):
    policy = ToolPolicy(accounts=["work"], allow_writes=False, deny_tools=["search_posts"])
    visible = {s.name for s in registry.select(policy=policy)}
    assert "delete_post" not in visible and "cancel_job" not in visible and "search_posts" not in visible
    assert "post" in visible

    ctx = ToolContext(manager, policy=policy)
    assert registry.call("list_accounts", {}, ctx).result == [
        {"account": "work", "handle": "work.test", "pds_url": "https://bsky.social"}
    ]
    denied = registry.call("get_timeline", {"account": "personal"}, ctx)
    assert denied.denied and "outside this run's scope" in denied.error
    hidden = registry.call("delete_post", {"account": "work", "uri": "at://x"}, ctx)
    assert hidden.denied
    dry = registry.call("post", {"account": "work", "text": "hi"}, ctx)
    assert dry.ok and dry.dry_run and dry.result["would"]["args"]["text"] == "hi"
    assert ctx.writes_done == 0


def test_policy_write_budget(manager, fake_atproto):
    ctx = ToolContext(manager, policy=ToolPolicy(allow_writes=True, max_writes=2))
    assert registry.call("post", {"account": "work", "text": "1"}, ctx).ok
    # A write whose API call fails still counts as an attempt: the budget caps
    # attempts, so a looping agent cannot hammer the API.
    failed = registry.call("like", {"account": "work", "uri": "at://did:plc:x/app.bsky.feed.post/1"}, ctx)
    assert failed.ok and failed.result["success"] is False
    assert ctx.writes_done == 2
    third = registry.call("post", {"account": "work", "text": "3"}, ctx)
    assert third.denied and "budget exhausted" in third.error
    assert registry.call("get_timeline", {"account": "work"}, ctx).ok   # reads unaffected


def test_policy_public_network_guard(manager):
    ctx = ToolContext(manager, policy=ToolPolicy(allow_writes=True, allow_public_writes=False))
    r = registry.call("post", {"account": "work", "text": "hi"}, ctx)
    assert r.denied and "public Bluesky network" in r.error


def test_policy_approval_mode_records_instead_of_sending(manager, fake_atproto):
    ctx = ToolContext(manager, policy=ToolPolicy(allow_writes=True, approval="writes"))
    r = registry.call("post", {"account": "work", "text": "hi"}, ctx)
    assert r.ok and r.pending_approval
    assert all(not c.sent for c in fake_atproto.instances)
    ctx.approved = True
    r2 = registry.call("post", {"account": "work", "text": "hi"}, ctx)
    assert r2.ok and not r2.pending_approval and r2.result["success"]


def test_policy_describe_mentions_mode():
    assert "DISABLED" in ToolPolicy().describe()
    text = ToolPolicy(accounts=["work"], allow_writes=True, max_writes=3, approval="writes").describe()
    assert "work" in text and "max 3" in text and "approval" in text


# ----------------------------------------------------------------------
# MCP server
# ----------------------------------------------------------------------

def test_mcp_server_exposes_policy_filtered_tools(manager, fake_atproto):
    pytest.importorskip("mcp")
    from skyctl.mcp_server import build_mcp_server

    server = build_mcp_server(manager, ToolPolicy(accounts=["work"], allow_writes=True, max_writes=1))

    async def scenario():
        tools = {t.name: t for t in await server.list_tools()}
        assert "delete_post" not in tools and "post" in tools
        assert tools["get_timeline"].annotations.readOnlyHint is True
        assert tools["post"].annotations.readOnlyHint is False
        assert tools["post"].inputSchema["required"] == ["account", "text"]

        def text(res):
            return json.loads(res[0].text)

        assert text(await server.call_tool("list_accounts", {})) == [{"account": "work", "handle": "work.test", "pds_url": "https://bsky.social"}]
        ok = text(await server.call_tool("post", {"account": "work", "text": "hello"}))
        assert ok["success"] is True
        denied = text(await server.call_tool("post", {"account": "personal", "text": "x"}))
        assert denied["denied"] is True
        budget = text(await server.call_tool("post", {"account": "work", "text": "again"}))
        assert "budget" in budget["error"]
        resources = [str(r.uri) for r in await server.list_resources()]
        assert "skyctl://accounts" in resources
        prompts = [p.name for p in await server.list_prompts()]
        assert "engage_with_mentions" in prompts

    asyncio.run(scenario())
    assert [s["text"] for s in fake_atproto.instances[-1].sent] == ["hello"]
