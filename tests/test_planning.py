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

"""The plan protocol: model, static checks, store transitions, deterministic execution."""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_post_view
from skyctl.adapters import MemoryObjectStore
from skyctl.planning import (
    ApprovalRequired,
    Plan,
    PlanError,
    PlanStore,
    check_plan,
    execute_plan,
    render_preview,
)
from skyctl.planning.model import RefError, iter_refs, resolve_path, substitute
from skyctl.tools import ToolContext, ToolPolicy, registry


@pytest.fixture
def ctx(manager):
    c = ToolContext(manager, policy=ToolPolicy(allow_writes=True, max_writes=10))
    c.plans = PlanStore(MemoryObjectStore())
    c.principal = "tester"
    c.grounding = "off"  # these tests build plans by hand; grounding has its own tests below
    return c


def plan(*steps, **kw) -> Plan:
    return Plan.model_validate({"goal": kw.pop("goal", "test goal"), "steps": list(steps), **kw})


def step(id, tool, **args):
    extra = {k[1:]: args.pop(k) for k in list(args) if k.startswith("_")}
    return {"id": id, "tool": tool, "args": args, **extra}


def errors(check):
    return [f.message for f in check.findings if f.level == "error"]


# ----------------------------------------------------------------------
# Model
# ----------------------------------------------------------------------


def test_plan_model_and_hash():
    p = plan(step("a", "post", account="work", text="hi"))
    assert p.hash.startswith("sha256:") and p.hash == plan(step("a", "post", account="work", text="hi")).hash
    assert p.hash != plan(step("a", "post", account="work", text="hi!")).hash
    with pytest.raises(ValueError, match="duplicate step ids"):
        plan(step("a", "post", account="w", text="x"), step("a", "like", account="w", uri="at://x"))
    with pytest.raises(ValueError):
        Plan.model_validate({"goal": "g", "steps": []})
    with pytest.raises(ValueError):
        Plan.model_validate({"goal": "g", "steps": [{"id": "a", "tool": "post", "oops": 1}]})


def test_refs():
    args = {"quote_uri": {"$ref": "p1.uri"}, "nested": [{"$ref": "t1.posts.0.url"}], "plain": "x"}
    assert sorted(iter_refs(args)) == ["p1.uri", "t1.posts.0.url"]
    results = {"p1": {"uri": "at://p1"}, "t1": {"posts": [{"url": "https://t1"}]}}
    assert substitute(args, lambda p: resolve_path(results, p)) == {
        "quote_uri": "at://p1",
        "nested": ["https://t1"],
        "plain": "x",
    }
    with pytest.raises(RefError):
        resolve_path(results, "t1.posts.5.url")
    with pytest.raises(RefError):
        resolve_path(results, "zz.uri")


# ----------------------------------------------------------------------
# Static checks
# ----------------------------------------------------------------------


def test_a_good_plan_passes_with_a_preview(ctx):
    p = plan(
        step("p1", "post", account="work", text="Launch day! https://example.com #skyctl", _why="announce"),
        step("q1", "post", account="work", text="ICYMI", quote_uri={"$ref": "p1.uri"}, _depends_on=["p1"]),
        step("t1", "thread", account="work", text="word " * 120),
        intent="publish",
        assumptions=["UTC"],
    )
    c = check_plan(p, ctx, registry)
    assert c.ok, errors(c)
    assert c.summary["writes"] == 3 and c.summary["writes_by_account"] == {"work": 3}
    assert c.summary["public_network_accounts"] == ["work"]
    first = c.preview[0]
    assert first["handle"] == "work.test" and {f["type"] for f in first["facets"]} == {"link", "tag"}
    assert c.preview[1]["uses"] == ["p1.uri"] and len(c.preview[2]["parts"]) == 2
    text = render_preview(c, p)
    assert "[p1] post as @work.test" in text and "uses: p1.uri" in text and "Assumes: UTC" in text


@pytest.mark.parametrize(
    ("bad_step", "message"),
    [
        (step("a", "nope"), "unknown tool"),
        (step("a", "plan_create", plan={}), "cannot be a plan step"),
        (step("a", "post", account="work"), "invalid arguments for post: text"),
        (step("a", "post", account="work", text="x" * 301), "301 graphemes"),
        (step("a", "post", account="work", text="   "), "text is empty"),
        (step("a", "post", account="ghost", text="hi"), "unknown account"),
        (step("a", "post", account="work", text="hi", images=["1", "2", "3", "4", "5"]), "at most 4 images"),
        (
            step("a", "post", account="work", text="hi", images=["a.png"], link_card="https://x.y"),
            "both images and a link card",
        ),
        (step("a", "schedule_post", account="work", text="hi", run_at="2001-01-01T00:00:00Z"), "not in the future"),
        (step("a", "schedule_post", account="work", text="hi", run_at="tomorrow"), "not ISO 8601"),
        (step("a", "schedule_cron", account="work", text="hi", cron="every day"), "valid 5-field"),
        (step("a", "post", account="work", text="hi", quote_uri={"$ref": "later.uri"}), "earlier step"),
        (step("a", "post", account="work", text="hi", _depends_on=["zz"]), "not an earlier step"),
    ],
)
def test_static_checks_catch(ctx, bad_step, message):
    c = check_plan(plan(bad_step), ctx, registry)
    assert not c.ok and any(message in m for m in errors(c)), errors(c)


def test_ref_fields_are_checked_against_the_tool_result(ctx):
    c = check_plan(
        plan(
            step("p1", "post", account="work", text="a"),
            step("q1", "post", account="work", text="b", quote_uri={"$ref": "p1.nonsense"}),
        ),
        ctx,
        registry,
    )
    assert any("post results have no 'nonsense'" in m for m in errors(c))


def test_questions_block_execution(ctx):
    c = check_plan(plan(step("a", "post", account="work", text="hi"), questions=["Which account?"]), ctx, registry)
    assert not c.ok and "Which account?" in errors(c)[0]


def test_policy_scope_budget_and_permission(manager):
    c = ToolContext(manager, policy=ToolPolicy(accounts=["work"], allow_writes=True, max_writes=1))
    assert any(
        "outside this session's scope" in m
        for m in errors(check_plan(plan(step("a", "post", account="personal", text="x")), c, registry))
    )
    two = plan(step("a", "post", account="work", text="1"), step("b", "post", account="work", text="2"))
    assert any("only 1 remain" in m for m in errors(check_plan(two, c, registry)))
    assert any(
        "not permitted" in m
        for m in errors(check_plan(plan(step("a", "delete_post", account="work", uri="at://x")), c, registry))
    )
    # Writes disabled: valid, but the plan would only preview.
    c2 = ToolContext(manager, policy=ToolPolicy(allow_writes=False))
    r = check_plan(plan(step("a", "post", account="work", text="x")), c2, registry)
    assert r.ok and "only preview" in r.findings[0].message


def test_hygiene_warnings(ctx):
    p = plan(
        step("a", "post", account="work", text="same"),
        step("b", "post", account="work", text="same"),
        step("c", "reply", account="work", parent_uri="at://p/1", text="one"),
        step("d", "reply", account="work", parent_uri="at://p/1", text="two"),
    )
    c = check_plan(p, ctx, registry)
    warnings = [f.message for f in c.findings if f.level == "warning"]
    assert c.ok and any("duplicates step 'a'" in w for w in warnings) and any("second reply" in w for w in warnings)


def test_second_reply_to_same_author_warns(ctx):
    p = plan(
        step("a", "reply", account="work", parent_uri="at://did:plc:alice/app.bsky.feed.post/1", text="one"),
        step("b", "reply", account="work", parent_uri="at://did:plc:alice/app.bsky.feed.post/2", text="two"),
        step("c", "reply", account="work", parent_uri="at://did:plc:bob/app.bsky.feed.post/3", text="three"),
    )
    c = check_plan(p, ctx, registry)
    same_author = [f for f in c.findings if "same author" in f.message]
    assert c.ok and [f.step for f in same_author] == ["b"] and same_author[0].level == "warning"


def test_preview_shows_the_post_answered_and_link_provenance(ctx):
    from skyctl.tools.registry import _remember_uris

    parent = "at://did:plc:alice/app.bsky.feed.post/1"
    _remember_uris(
        ctx, {"data": {"post": {"uri": parent, "author": {"handle": "alice.test"}, "text": "The bridge  is closed."}}}
    )
    ctx.seen_urls.add("https://news.example/bridge")
    p = plan(
        step(
            "a",
            "reply",
            account="work",
            parent_uri=parent,
            text="It reopened: https://news.example/bridge and https://made.up/x",
        )
    )
    text = render_preview(check_plan(p, ctx, registry), p)
    assert "in reply to @alice.test: “The bridge is closed.”" in text
    assert "link: https://news.example/bridge (retrieved in this run)" in text
    assert "link: https://made.up/x (NOT retrieved by the agent)" in text


def test_pluggable_validator_link_allowlist(ctx, monkeypatch):
    monkeypatch.setenv("SKYCTL_PLAN_LINK_ALLOWLIST", "example.com")
    ok = plan(step("a", "post", account="work", text="see https://docs.example.com/x"))
    bad = plan(step("a", "post", account="work", text="see https://evil.test/x"))
    assert check_plan(ok, ctx, registry).ok
    assert any("evil.test" in m for m in errors(check_plan(bad, ctx, registry)))


# ----------------------------------------------------------------------
# Store transitions
# ----------------------------------------------------------------------


def _create(ctx, p, mode="none"):
    return ctx.plans.create(p, check_plan(p, ctx, registry), created_by="tester", approval_mode=mode)


def test_store_approval_states(ctx):
    writes = plan(step("a", "post", account="work", text="hi"))
    reads = plan(step("a", "get_timeline", account="work"))
    assert _create(ctx, writes, "none")["status"] == "approved"
    assert _create(ctx, writes, "out_of_band")["status"] == "awaiting_approval"
    assert _create(ctx, reads, "out_of_band")["status"] == "approved"  # read-only needs no approval
    rec = _create(ctx, writes, "out_of_band")
    with pytest.raises(PlanError, match="hash mismatch"):
        ctx.plans.approve(rec["id"], "sha256:other", by="h", via="cli")
    approved = ctx.plans.approve(rec["id"], rec["plan_hash"], by="human", via="cli")
    assert approved["approval"]["by"] == "human" and approved["status"] == "approved"
    with pytest.raises(PlanError, match="not awaiting"):
        ctx.plans.approve(rec["id"], rec["plan_hash"], by="human", via="cli")
    assert ctx.plans.cancel(rec["id"], by="human")["status"] == "cancelled"
    with pytest.raises(PlanError):
        execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry)
    assert len(ctx.plans.list()) == 4 and len(ctx.plans.list(status="cancelled")) == 1
    with pytest.raises(PlanError):
        ctx.plans.get("../escape")


# ----------------------------------------------------------------------
# Execution
# ----------------------------------------------------------------------


def test_execute_is_hash_bound_and_approval_gated(ctx, manager):
    rec = _create(ctx, plan(step("a", "post", account="work", text="hi")), "out_of_band")
    with pytest.raises(PlanError, match="hash mismatch"):
        execute_plan(ctx.plans, rec["id"], "sha256:tampered", ctx, registry)
    with pytest.raises(ApprovalRequired):
        execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry)
    assert not manager.client("work")._client.sent
    done = execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry, in_band_approval="client-confirmation")
    assert done["status"] == "completed" and done["approval"]["via"] == "client-confirmation"
    assert [e["type"] for e in done["events"]] == ["created", "approved", "executing", "completed"]


def test_execute_chains_refs_and_is_idempotent(ctx, manager):
    p = plan(
        step("t1", "thread", account="work", text="word " * 120),
        step("r1", "reply", account="work", parent_uri={"$ref": "t1.posts.0.uri"}, text="and one more thing"),
    )
    rec = _create(ctx, p)
    done = execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry)
    assert done["status"] == "completed"
    sent = manager.client("work")._client.sent
    assert len(sent) == 3 and sent[2]["reply_to"].parent.uri.endswith("/sent1")
    assert execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry)["status"] == "completed"
    assert len(sent) == 3
    assert ctx.writes_done == 2


def test_failure_stops_dependents_and_retry_resumes_without_duplicates(ctx, manager):
    fake = manager.client("work").login()._client
    p = plan(
        step("a", "post", account="work", text="first"),
        step("b", "post", account="work", text="second"),
        step("c", "post", account="work", text="third", quote_uri={"$ref": "a.uri"}),
    )
    rec = _create(ctx, p)
    fake.fail_sends = 0
    original = fake.send_post
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("network down")
        return original(*args, **kwargs)

    fake.send_post = flaky
    failed = execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry)
    assert failed["status"] == "failed"
    assert {k: v["status"] for k, v in failed["steps"].items()} == {"a": "done", "b": "failed", "c": "skipped"}
    retried = execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry)
    assert retried["status"] == "completed"
    assert [s["text"] for s in fake.sent] == ["first", "second", "third"]  # "first" not repeated
    assert fake.sent[2]["embed"].record.uri == retried["steps"]["a"]["result"]["uri"]


def test_continue_on_error(ctx, manager):
    p = plan(
        step("a", "like", account="work", uri="at://did:plc:x/app.bsky.feed.post/missing", _continue_on_error=True),
        step("b", "post", account="work", text="still posted"),
    )
    rec = _create(ctx, p)
    out = execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry)
    assert out["steps"]["a"]["status"] == "failed" and out["steps"]["b"]["status"] == "done"
    assert out["status"] == "failed"  # reported honestly, but b ran


def test_policy_is_rechecked_at_execution(ctx, manager):
    rec = _create(ctx, plan(step("a", "post", account="work", text="hi")))
    ctx.policy = ToolPolicy(accounts=["personal"], allow_writes=True)  # tightened after approval
    with pytest.raises(PlanError, match="no longer passes"):
        execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry)
    assert not manager.client("work")._client.sent


def test_concurrent_execution_is_refused(ctx):
    rec = _create(ctx, plan(step("a", "post", account="work", text="hi")))
    far = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    ctx.plans.update(rec["id"], lambda r: r.update(status="executing", claimed_by="other-host", claimed_until=far))
    with pytest.raises(PlanError, match="already executing"):
        execute_plan(ctx.plans, rec["id"], rec["plan_hash"], ctx, registry)


def test_writes_disabled_executes_as_dry_run(manager):
    c = ToolContext(manager, policy=ToolPolicy(allow_writes=False))
    c.plans = PlanStore(MemoryObjectStore())
    rec = _create(c, plan(step("a", "post", account="work", text="hi")))
    out = execute_plan(c.plans, rec["id"], rec["plan_hash"], c, registry)
    assert out["status"] == "dry_run" and out["steps"]["a"]["result"]["dry_run"] is True
    assert not manager.client("work")._client.sent


def test_exposure_versus_permission(manager):
    """A 'plan' profile cannot call post directly, but an approved plan may."""
    c = ToolContext(manager, policy=ToolPolicy(allow_writes=True, expose_tags={"read", "plan"}))
    c.plans = PlanStore(MemoryObjectStore())
    direct = registry.call("post", {"account": "work", "text": "x"}, c)
    assert direct.denied and "plan_create" in direct.error
    created = registry.call(
        "plan_create", {"plan": {"goal": "g", "steps": [step("a", "post", account="work", text="x")]}}, c
    ).result
    done = registry.call("plan_execute", {"plan_id": created["plan_id"], "plan_hash": created["plan_hash"]}, c).result
    assert done["status"] == "completed" and done["steps"]["a"]["url"].startswith("https://bsky.app/")


# ----------------------------------------------------------------------
# Grounding: plans may only target posts the session has observed
# ----------------------------------------------------------------------


def test_grounding_refuses_invented_targets_and_accepts_observed_ones(manager):
    c = ToolContext(manager, policy=ToolPolicy(allow_writes=True))
    target = "at://did:plc:other/app.bsky.feed.post/found"
    p = plan(step("r", "reply", account="work", parent_uri=target, text="hi"))
    refused = check_plan(p, c, registry)
    assert not refused.ok and "was not returned by any tool" in errors(refused)[0]
    registry.call("search_posts", {"account": "work", "query": "anything"}, c)  # the fake returns `target`
    assert target in c.seen_uris and check_plan(p, c, registry).ok
    # $refs to earlier steps are grounded by construction.
    chained = plan(
        step("p", "post", account="work", text="a"),
        step("q", "post", account="work", text="b", quote_uri={"$ref": "p.uri"}),
    )
    assert check_plan(chained, c, registry).ok
    c.grounding = "warning"
    soft = check_plan(plan(step("l", "like", account="work", uri="at://did:plc:x/app.bsky.feed.post/zz")), c, registry)
    assert soft.ok and "not returned" in soft.findings[0].message


def test_grounding_is_not_reapplied_by_the_executor(manager):
    """A plan proposed by one session (e.g. the delegated agent) can be executed by another."""
    proposer = ToolContext(manager, policy=ToolPolicy(allow_writes=True))
    proposer.plans = PlanStore(MemoryObjectStore())
    registry.call("search_posts", {"account": "work", "query": "x"}, proposer)
    manager.client("work")._client.posts["at://did:plc:other/app.bsky.feed.post/found"] = make_post_view(
        "at://did:plc:other/app.bsky.feed.post/found", "cid-found"
    )
    p = plan(step("l", "like", account="work", uri="at://did:plc:other/app.bsky.feed.post/found"))
    rec = proposer.plans.create(p, check_plan(p, proposer, registry), created_by="agent", approval_mode="none")
    executor = ToolContext(manager, policy=ToolPolicy(allow_writes=True))
    executor.plans = proposer.plans
    assert executor.seen_uris == set()
    assert execute_plan(executor.plans, rec["id"], rec["plan_hash"], executor, registry)["status"] == "completed"
