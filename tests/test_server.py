"""The HTTP service: auth, runs, tick, MCP over streamable HTTP."""

import json
import logging
import time

import pytest

pytest.importorskip("starlette")
pytest.importorskip("mcp")

from starlette.testclient import TestClient  # noqa: E402

from skyctl.adapters import MemoryObjectStore, ObjectJobStore  # noqa: E402
from skyctl.agent import AgentRunner  # noqa: E402
from skyctl.ports import ChatTurn, ToolCall  # noqa: E402
from skyctl.scheduler import BskyScheduler  # noqa: E402
from skyctl.server import create_app  # noqa: E402
from skyctl.settings import AgentDefaults, Settings  # noqa: E402

logging.disable(logging.CRITICAL)

MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class Scripted:
    model = "script:test"
    tool_format = "anthropic"

    def chat(self, system, messages, tools):
        if len(messages) == 1:
            return ChatTurn(text="", tool_calls=[ToolCall("c1", "post", {"account": "work", "text": "via http"})], stop_reason="tool_use")
        return ChatTurn(text="done", tool_calls=[], stop_reason="end")


@pytest.fixture
def app(manager, monkeypatch):
    monkeypatch.delenv("SKYCTL_HTTP_ALLOW_ANON", raising=False)
    settings = Settings(
        http_token="tok",
        state_url="memory://t",
        agent=AgentDefaults(allow_writes=True, allow_delete=False, max_writes=3, accounts=["work"]),
    )
    state = MemoryObjectStore()
    runner = AgentRunner(manager, provider=Scripted(), state=state)
    scheduler = BskyScheduler(manager, job_store=ObjectJobStore(state))
    return create_app(settings, manager=manager, scheduler=scheduler, runner=runner)


def wait_for(client, run_id, headers, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/runs/{run_id}", headers=headers).json()
        if data["status"] != "running":
            return data
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def test_health_is_public_everything_else_needs_the_token(app):
    with TestClient(app) as c:
        assert c.get("/healthz").json() == {"ok": True}
        assert c.get("/readyz").status_code == 401
        assert c.get("/readyz", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = c.get("/readyz", headers={"Authorization": "Bearer tok"})
        assert ok.status_code == 200 and ok.json()["accounts"] == ["personal", "work"]


def test_no_token_configured_refuses_unless_anon_allowed(manager, monkeypatch):
    settings = Settings(http_token=None, state_url="memory://t")
    monkeypatch.delenv("SKYCTL_HTTP_ALLOW_ANON", raising=False)
    with TestClient(create_app(settings, manager=manager, mcp_server=False)) as c:
        assert c.get("/readyz").status_code == 503
    monkeypatch.setenv("SKYCTL_HTTP_ALLOW_ANON", "true")
    with TestClient(create_app(settings, manager=manager, mcp_server=False)) as c:
        assert c.get("/readyz").status_code == 200


def test_runs_respect_deployment_ceilings(app, fake_atproto):
    h = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        assert c.post("/runs", content=b"not json", headers=h).status_code == 400
        assert c.post("/runs", json={"goal": "x", "bogus": 1}, headers=h).status_code == 400
        assert c.post("/runs", json={"goal": "x", "allow_delete": True}, headers=h).status_code == 403

        started = c.post("/runs", json={"goal": "post it", "allow_writes": True, "max_writes": 99, "accounts": ["work", "personal"]}, headers=h)
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        data = wait_for(c, run_id, h)
        assert data["status"] == "completed"
        assert data["result"]["writes"] == 1
        assert data["result"]["task"]["max_writes"] == 3            # clamped to the deployment ceiling
        assert data["result"]["task"]["accounts"] == ["work"]       # personal filtered out
        assert [e["type"] for e in data["events"]][:2] == ["task", "turn"]
        assert c.get("/runs/nope", headers=h).status_code == 404


def test_tick_endpoint(app):
    h = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        data = c.post("/tick", headers=h).json()
        assert data["sent"] == [] and data["skipped"] is None


def test_mcp_over_streamable_http(app):
    h = {"Authorization": "Bearer tok", **MCP_HEADERS}
    with TestClient(app) as c:
        init = c.post("/mcp/", headers=h, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
        })
        assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "skyctl"
        listed = c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in listed.json()["result"]["tools"]]
        # Remote sessions default to the "plan" profile: reads + plans, no direct writes.
        assert {"plan_create", "plan_execute", "get_notifications"} <= set(names)
        assert "post" not in names and "delete_post" not in names
        called = c.post("/mcp/", headers=h, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "list_accounts", "arguments": {}},
        })
        payload = json.loads(called.json()["result"]["content"][0]["text"])
        assert payload == {"accounts": [{"account": "work", "handle": "work.test", "pds_url": "https://bsky.social"}]}
        assert c.post("/mcp/", headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"}).status_code == 401


# ----------------------------------------------------------------------
# /invoke and capability-driven run strategy
# ----------------------------------------------------------------------

def test_invoke_endpoint_handles_push_envelopes(app):
    import base64

    h = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        r = c.post("/invoke", json={"kind": "tick"}, headers=h)
        assert r.status_code == 200 and r.json()["outcomes"][0]["kind"] == "tick"
        push = {"message": {"data": base64.b64encode(b'{"kind": "tick"}').decode(), "messageId": "1"},
                "subscription": "projects/p/subscriptions/s"}
        assert c.post("/invoke", json=push, headers=h).status_code == 200
        assert c.post("/invoke", json={"kind": "nope"}, headers=h).status_code == 422     # permanent: no retry
        assert c.post("/invoke", content=b"garbage", headers=h).status_code == 400
        assert c.post("/invoke", json={"kind": "tick"}).status_code == 401
        # CloudEvents webhook validation handshake is public and echoes the origin.
        opt = c.options("/invoke", headers={"WebHook-Request-Origin": "eventgrid.azure.net"})
        assert opt.status_code == 200 and opt.headers["webhook-allowed-origin"] == "eventgrid.azure.net"
        # Event Grid subscription validation returns the code as the body.
        validation = [{"id": "v", "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent", "data": {"validationCode": "xyz"}}]
        assert c.post("/invoke", json=validation, headers=h).json() == {"validationResponse": "xyz"}


def test_runs_are_queued_where_background_threads_are_unsafe(manager, monkeypatch):
    from skyctl.adapters.queues import MemoryQueue
    from skyctl.platform import Runtime, get_platform

    settings = Settings(http_token="tok", state_url="memory://t", queue_url="memory://runs-test",
                        agent=AgentDefaults(allow_writes=False))
    runtime = Runtime(settings, platform=get_platform("gcp"), compute="cloud-run")
    state = MemoryObjectStore()
    runner = AgentRunner(manager, provider=Scripted(), state=state)
    app = create_app(settings, manager=manager, runner=runner, runtime=runtime, mcp_server=False)
    h = {"Authorization": "Bearer tok"}
    with TestClient(app) as c:
        r = c.post("/runs", json={"goal": "queued please"}, headers=h)
        assert r.status_code == 202 and r.json()["strategy"] == "queue"
        run_id = r.json()["run_id"]
        [msg] = runtime.queue().receive()
        assert msg.body["kind"] == "task.run" and msg.body["run_id"] == run_id
        assert c.get(f"/runs/{run_id}", headers=h).json()["status"] == "queued"

    # Without a queue, request-billed Cloud Run cannot run agents asynchronously at all.
    no_queue = Runtime(Settings(http_token="tok", state_url="memory://t"), platform=get_platform("gcp"), compute="cloud-run")
    app = create_app(no_queue.settings, manager=manager, runner=runner, runtime=no_queue, mcp_server=False)
    with TestClient(app) as c:
        r = c.post("/runs", json={"goal": "x"}, headers=h)
        assert r.status_code == 501 and "SKYCTL_QUEUE_URL" in r.json()["error"]


def test_readyz_describes_the_runtime(app):
    with TestClient(app) as c:
        body = c.get("/readyz", headers={"Authorization": "Bearer tok"}).json()
        assert body["runtime"]["platform"] == "local" and body["auth"] == ["bearer"]
        assert "compute.background_after_response" in body["runtime"]["capabilities"]


# ----------------------------------------------------------------------
# Plans over HTTP: approval is separated from the agent's credential
# ----------------------------------------------------------------------

def _plan_app(manager, monkeypatch, state):
    from skyctl.platform import Runtime, get_platform
    from skyctl.platform.auth import build_auth_chain

    monkeypatch.setenv("SKYCTL_HTTP_TOKEN", "agent-tok")
    monkeypatch.setenv("SKYCTL_APPROVER_TOKEN", "human-tok")
    settings = Settings(http_token="agent-tok", state_url="memory://plans",
                        agent=AgentDefaults(allow_writes=True, max_writes=5, accounts=["work"]))
    runtime = Runtime(settings, platform=get_platform("local"))
    runtime._state = state
    runner = AgentRunner(manager, provider=Scripted(), state=state)
    return create_app(settings, manager=manager, runner=runner, runtime=runtime, mcp_server=False,
                      auth_chain=build_auth_chain())


def test_agent_token_cannot_approve_its_own_plan(manager, monkeypatch, fake_atproto):
    from skyctl.planning import Plan, PlanStore, check_plan
    from skyctl.tools import ToolContext, ToolPolicy, registry

    state = MemoryObjectStore()
    ctx = ToolContext(manager, policy=ToolPolicy(accounts=["work"], allow_writes=True))
    p = Plan.model_validate({"goal": "g", "steps": [{"id": "a", "tool": "post", "args": {"account": "work", "text": "hello"}}]})
    rec = PlanStore(state).create(p, check_plan(p, ctx, registry), created_by="mcp-http", approval_mode="out_of_band")
    agent, human = {"Authorization": "Bearer agent-tok"}, {"Authorization": "Bearer human-tok"}
    body = {"plan_hash": rec["plan_hash"]}
    with TestClient(_plan_app(manager, monkeypatch, state)) as c:
        assert c.get("/plans", headers=agent).json()["plans"][0]["status"] == "awaiting_approval"
        assert c.post(f"/plans/{rec['id']}/execute", json=body, headers=agent).status_code == 403
        denied = c.post(f"/plans/{rec['id']}/approve", json=body, headers=agent)
        assert denied.status_code == 403 and "may not approve" in denied.json()["error"]
        assert c.post(f"/plans/{rec['id']}/approve", json={"plan_hash": "sha256:x"}, headers=human).status_code == 409
        ok = c.post(f"/plans/{rec['id']}/approve", json=body, headers=human)
        assert ok.status_code == 200 and ok.json()["approval"]["by"] == "approver-token"
        done = c.post(f"/plans/{rec['id']}/execute", json=body, headers=agent)
        assert done.status_code == 200 and done.json()["status"] == "completed"
        assert c.get(f"/plans/{rec['id']}", headers=agent).json()["events"][-1]["type"] == "completed"
        assert c.get("/plans/nope", headers=agent).status_code == 404
    assert [s["text"] for s in manager.client("work")._client.sent] == ["hello"]


def test_approver_token_must_differ_from_agent_token():
    from skyctl.platform.auth import StaticBearer

    with pytest.raises(ValueError, match="must differ"):
        StaticBearer("same", "same")
