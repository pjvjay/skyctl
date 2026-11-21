"""
skyctl as an MCP server for agentic clients, exercised through a real MCP
ClientSession (in-memory transport): profiles, context resources, prompts,
the plan protocol, every approval mode (incl. elicitation), progress, and
delegation to skyctl's own agent.
"""

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402
from mcp.types import ElicitResult  # noqa: E402

from skyctl.adapters import MemoryObjectStore  # noqa: E402
from skyctl.agent import AgentRunner, TaskSpec  # noqa: E402
from skyctl.mcp_server import McpConfig, build_mcp_server  # noqa: E402
from skyctl.ports import ChatTurn, ToolCall  # noqa: E402
from skyctl.tools import ToolPolicy  # noqa: E402

from conftest import make_post_view  # noqa: E402

MENTION = "at://did:plc:fan/app.bsky.feed.post/n1"

REPLY_PLAN = {
    "goal": "Answer the student-discount question in my mentions on the work account",
    "intent": "engage",
    "assumptions": ["'mentions' means unread notifications"],
    "steps": [{"id": "r1", "tool": "reply", "why": "@fan.test asked about a student discount",
               "args": {"account": "work", "parent_uri": MENTION, "text": "Yes! Students get 20% off with a .edu address."}}],
}


def server_for(manager, approval="client", profile="plan", state=None, runner=None, allow_writes=True):
    manager.client("work")._client.posts[MENTION] = make_post_view(MENTION, "cid-n1")
    return build_mcp_server(
        manager, ToolPolicy(accounts=["work"], allow_writes=allow_writes, max_writes=5),
        config=McpConfig(profile=profile, approval=approval), state=state or MemoryObjectStore(), runner=runner,
    )


def data(result):
    assert not result.isError, result.content[0].text
    return json.loads(result.content[0].text)


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------
# Surface: what an agent sees
# ----------------------------------------------------------------------

def test_plan_profile_surface(manager):
    server = server_for(manager)

    async def scenario():
        async with create_connected_server_and_client_session(server) as client:
            init_tools = {t.name: t for t in (await client.list_tools()).tools}
            # Agents get reads and the plan protocol; no direct writes.
            assert {"get_notifications", "get_post", "preview_post", "plan_check", "plan_create", "plan_execute"} <= set(init_tools)
            assert not {"post", "reply", "like", "delete_post"} & set(init_tools)
            assert init_tools["plan_execute"].annotations.destructiveHint is True
            assert init_tools["plan_check"].annotations.readOnlyHint is True
            # The plan schema is self-contained, so any model can fill it in.
            plan_schema = init_tools["plan_create"].inputSchema["properties"]["plan"]

            def schema_keys(node):
                if isinstance(node, dict):
                    for k, v in node.items():
                        yield k
                        yield from schema_keys(v)
                elif isinstance(node, list):
                    for v in node:
                        yield from schema_keys(v)

            assert "steps" in plan_schema["properties"]
            assert not {"$ref", "$defs"} & set(schema_keys(init_tools["plan_create"].inputSchema))
            policy = json.loads((await client.read_resource("skyctl://policy")).contents[0].text)
            assert policy["profile"] == "plan" and "reply" in policy["plan_only_tools"]
            guide = (await client.read_resource("skyctl://guide")).contents[0].text
            assert "| engage" in guide and "$ref" in guide
            prompts = {p.name for p in (await client.list_prompts()).prompts}
            assert {"act_on_request", "triage_mentions", "publish_announcement", "schedule_campaign", "daily_digest"} <= prompts
            msg = await client.get_prompt("act_on_request", {"request": "thank everyone who replied to my launch post"})
            assert "thank everyone who replied" in msg.messages[0].content.text
    run(scenario())


def test_profiles_gate_direct_tools(manager):
    names = lambda profile: {t.name for t in run(server_for(manager, profile=profile).list_tools())}  # noqa: E731
    assert "post" not in names("read") and "plan_create" not in names("read")
    assert "post" in names("direct") and "plan_create" in names("direct")
    assert "delete_post" not in names("direct")                 # policy: allow_delete is off
    assert "post" not in names("plan")


# ----------------------------------------------------------------------
# The agentic loop, with each approval mode
# ----------------------------------------------------------------------

async def _draft_and_create(client) -> dict:
    # 1. gather
    notes = data(await client.call_tool("get_notifications", {"account": "work"}))
    assert notes["note"].startswith("Content fetched") and notes["data"][0]["uri"] == MENTION
    # 2. a first draft that is too long is rejected with a precise error
    bad = json.loads(json.dumps(REPLY_PLAN))
    bad["steps"][0]["args"]["text"] = "x" * 301
    checked = data(await client.call_tool("plan_check", {"plan": bad}))
    assert not checked["ok"] and "301 graphemes" in checked["errors"][0]["message"]
    refused = await client.call_tool("plan_create", {"plan": bad})
    assert refused.isError and "not valid" in refused.content[0].text
    # 3. the fixed plan is created
    created = data(await client.call_tool("plan_create", {"plan": REPLY_PLAN}))
    assert "reply as @work.test" in created["preview_text"]
    return created


def test_elicitation_accept_executes_with_progress(manager):
    asked, progress = [], []

    async def approve(context, params):
        asked.append(params.message)
        return ElicitResult(action="accept", content={"approve": True})

    async def scenario():
        server = server_for(manager, approval="elicit")
        async with create_connected_server_and_client_session(server, elicitation_callback=approve) as client:
            created = await _draft_and_create(client)
            assert created["status"] == "awaiting_approval"

            async def on_progress(done, total, message):
                progress.append((done, total, message))

            result = await client.call_tool("plan_execute", {"plan_id": created["plan_id"], "plan_hash": created["plan_hash"]},
                                            progress_callback=on_progress)
            done = data(result)
            assert done["status"] == "completed" and done["approval"]["via"] == "elicitation"
            assert done["steps"]["r1"]["url"].startswith("https://bsky.app/")
    run(scenario())
    assert "Approve this plan?" in asked[0] and "Students get 20% off" in asked[0]
    assert progress and progress[-1][:2] == (1, 1)
    assert manager.client("work")._client.sent[0]["text"].startswith("Yes! Students")


def test_elicitation_decline_does_nothing(manager):
    async def decline(context, params):
        return ElicitResult(action="decline")

    async def scenario():
        async with create_connected_server_and_client_session(server_for(manager, approval="elicit"),
                                                               elicitation_callback=decline) as client:
            created = await _draft_and_create(client)
            r = await client.call_tool("plan_execute", {"plan_id": created["plan_id"], "plan_hash": created["plan_hash"]})
            assert r.isError and "did not approve" in r.content[0].text
            status = data(await client.call_tool("plan_status", {"plan_id": created["plan_id"]}))
            assert status["status"] == "awaiting_approval"
    run(scenario())
    assert not manager.client("work")._client.sent


def test_elicit_mode_without_client_support_points_to_out_of_band(manager):
    async def scenario():
        async with create_connected_server_and_client_session(server_for(manager, approval="elicit")) as client:
            created = await _draft_and_create(client)
            r = await client.call_tool("plan_execute", {"plan_id": created["plan_id"], "plan_hash": created["plan_hash"]})
            assert r.isError and "skyctl plan approve" in r.content[0].text
    run(scenario())
    assert not manager.client("work")._client.sent


def test_client_mode_treats_the_call_as_approval(manager):
    async def scenario():
        async with create_connected_server_and_client_session(server_for(manager, approval="client")) as client:
            created = await _draft_and_create(client)
            done = data(await client.call_tool("plan_execute", {"plan_id": created["plan_id"], "plan_hash": created["plan_hash"]}))
            assert done["approval"]["via"] == "client-confirmation"
            again = data(await client.call_tool("plan_execute", {"plan_id": created["plan_id"], "plan_hash": created["plan_hash"]}))
            assert again["status"] == "completed"
    run(scenario())
    assert len(manager.client("work")._client.sent) == 1


def test_out_of_band_needs_an_approver(manager):
    state = MemoryObjectStore()

    async def scenario():
        server = server_for(manager, approval="out_of_band", state=state)
        async with create_connected_server_and_client_session(server) as client:
            created = await _draft_and_create(client)
            r = await client.call_tool("plan_execute", {"plan_id": created["plan_id"], "plan_hash": created["plan_hash"]})
            assert r.isError and "needs approval (out_of_band)" in r.content[0].text
            # A human approves elsewhere (CLI / HTTP) — exactly this hash.
            from skyctl.planning import PlanStore

            PlanStore(state).approve(created["plan_id"], created["plan_hash"], by="alice@example.com", via="cli")
            tampered = await client.call_tool("plan_execute", {"plan_id": created["plan_id"], "plan_hash": "sha256:0"})
            assert tampered.isError and "hash mismatch" in tampered.content[0].text
            done = data(await client.call_tool("plan_execute", {"plan_id": created["plan_id"], "plan_hash": created["plan_hash"]}))
            assert done["approval"]["by"] == "alice@example.com"
            plans = json.loads((await client.read_resource("skyctl://plans")).contents[0].text)["plans"]
            assert plans[0]["status"] == "completed"
            one = json.loads((await client.read_resource(f"skyctl://plans/{created['plan_id']}")).contents[0].text)
            assert one["plan"]["steps"][0]["why"].startswith("@fan.test")
    run(scenario())


# ----------------------------------------------------------------------
# Delegation: skyctl's own agent plans, the client decides
# ----------------------------------------------------------------------

class PlannerModel:
    """A scripted model that behaves like a good planner: read, check, create, report."""

    model, tool_format = "script:planner", "anthropic"

    def __init__(self, attempt_direct_write=False):
        self.attempt_direct_write = attempt_direct_write
        self.tools_seen = None

    def chat(self, system, messages, tools):
        self.tools_seen = self.tools_seen or [t["name"] for t in tools]
        n = len(messages)
        if n == 1:
            calls = [ToolCall("c1", "get_notifications", {"account": "work"})]
            if self.attempt_direct_write:
                calls.append(ToolCall("c0", "reply", {"account": "work", "parent_uri": MENTION, "text": "sneaky"}))
            return ChatTurn("Reading mentions.", calls, "tool_use")
        if n == 3:
            return ChatTurn("", [ToolCall("c2", "plan_check", {"plan": REPLY_PLAN})], "tool_use")
        if n == 5:
            return ChatTurn("", [ToolCall("c3", "plan_create", {"plan": REPLY_PLAN})], "tool_use")
        return ChatTurn("Proposed one reply; it awaits approval.", [], "end")


def test_agent_run_delegates_planning_and_returns_a_plan(manager):
    state = MemoryObjectStore()
    model = PlannerModel(attempt_direct_write=True)
    runner = AgentRunner(manager, provider=model, state=state)

    async def scenario():
        server = server_for(manager, approval="client", state=state, runner=runner)
        async with create_connected_server_and_client_session(server) as client:
            out = data(await client.call_tool("agent_run", {"goal": "answer pricing questions in my mentions"}))
            assert out["status"] == "needs_approval" and out["plan_status"] == "awaiting_approval"
            assert "reply as @work.test" in out["preview_text"]
            done = data(await client.call_tool("plan_execute", {"plan_id": out["plan_id"], "plan_hash": out["plan_hash"]}))
            assert done["status"] == "completed"
            status = data(await client.call_tool("agent_status", {"run_id": out["run_id"]}))
            assert status["status"] == "needs_approval"
    run(scenario())
    # The delegated agent could only read and plan; its direct write attempt was refused.
    assert "reply" not in model.tools_seen and "plan_create" in model.tools_seen and "plan_execute" not in model.tools_seen
    sent = manager.client("work")._client.sent
    assert [s["text"] for s in sent] == [REPLY_PLAN["steps"][0]["args"]["text"]]


def test_runner_plan_mode_auto_executes_only_without_approval(manager):
    state = MemoryObjectStore()
    manager.client("work")._client.posts[MENTION] = make_post_view(MENTION, "cid-n1")
    auto = AgentRunner(manager, provider=PlannerModel(), state=state).run(
        TaskSpec(goal="answer", accounts=["work"], allow_writes=True, mode="plan", approval="none"))
    assert auto.status == "completed" and auto.plan_status == "completed" and auto.writes == 1
    held = AgentRunner(manager, provider=PlannerModel(), state=state).run(
        TaskSpec(goal="answer", accounts=["work"], allow_writes=True, mode="plan", approval="writes"))
    assert held.status == "needs_approval" and held.plan_status == "awaiting_approval"
    assert len(manager.client("work")._client.sent) == 1


def test_read_only_request_needs_no_plan(manager):
    class Reader:
        model, tool_format = "script:reader", "anthropic"

        def chat(self, system, messages, tools):
            if len(messages) == 1:
                return ChatTurn("", [ToolCall("c", "get_timeline", {"account": "work"})], "tool_use")
            return ChatTurn("Your timeline is quiet today.", [], "end")

    r = AgentRunner(manager, provider=Reader(), state=MemoryObjectStore()).run(
        TaskSpec(goal="what's on my timeline?", accounts=["work"], mode="plan"))
    assert r.status == "completed" and r.plan_id is None and "quiet" in r.summary
