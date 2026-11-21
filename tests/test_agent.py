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

"""The task agent: runner loop, budgets, approval, idempotency, providers, CLI."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from conftest import make_post_view, require
from skyctl.adapters import MemoryObjectStore
from skyctl.agent import AgentRunner, RunLog, TaskConfigError, TaskSpec, load_tasks
from skyctl.agent.chat import AnthropicChat, OpenAIChat, get_chat_provider, register_chat_provider
from skyctl.llm import LLMError, LLMRefusal
from skyctl.ports import ChatTurn, Message, ToolCall, ToolResult


class Scripted:
    """A ChatProvider that replays turns and records what it was shown."""

    tool_format = "anthropic"

    def __init__(self, turns, model="script:test"):
        self.turns = list(turns)
        self.model = model
        self.calls = []

    def chat(self, system, messages, tools):
        self.calls.append({"system": system, "messages": list(messages), "tools": [t["name"] for t in tools]})
        if not self.turns:
            return ChatTurn(text="(no more script)", tool_calls=[], stop_reason="end")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def tool_turn(*calls, text=""):
    return ChatTurn(
        text=text,
        tool_calls=[ToolCall(f"c{i}", name, args) for i, (name, args) in enumerate(calls)],
        stop_reason="tool_use",
        usage={"input_tokens": 100, "output_tokens": 20},
    )


def final(text="All done."):
    return ChatTurn(text=text, tool_calls=[], stop_reason="end", usage={"input_tokens": 50, "output_tokens": 10})


def last_tool_results(provider):
    msg = provider.calls[-1]["messages"][-1]
    return {r.name + ":" + r.call_id: json.loads(r.content) for r in msg.tool_results}


# ----------------------------------------------------------------------
# TaskSpec
# ----------------------------------------------------------------------


def test_task_spec_validation_and_policy():
    with pytest.raises(TaskConfigError):
        TaskSpec(goal="  ")
    with pytest.raises(TaskConfigError):
        TaskSpec(goal="x", approval="maybe")
    with pytest.raises(TaskConfigError):
        TaskSpec.from_dict({"goal": "x", "bogus": 1})
    t = TaskSpec(goal="x", accounts=["Work"], allow_writes=True, max_writes=2, tools=["post"])
    policy = t.policy()
    assert policy.accounts == ["work"] and policy.allow_writes and policy.max_writes == 2
    assert policy.allow_tools == ["post"]
    assert TaskSpec.from_dict(t.to_dict()) == t


def test_load_tasks(tmp_path):
    f = tmp_path / "tasks.toml"
    f.write_text(
        '[tasks.Daily]\ngoal = "Engage"\naccounts = ["work"]\nallow_writes = true\napproval = "writes"\n'
        '[tasks.report]\ngoal = "Summarise"\n'
    )
    tasks = load_tasks(f)
    assert set(tasks) == {"daily", "report"}
    assert tasks["daily"].name == "daily" and tasks["daily"].approval == "writes"
    f.write_text("[tasks.bad]\ngoal = 'x'\nnope = 1\n")
    with pytest.raises(TaskConfigError):
        load_tasks(f)
    with pytest.raises(TaskConfigError):
        load_tasks(tmp_path / "missing.toml")


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------


def test_run_reads_then_writes_then_reports(manager, fake_atproto):
    provider = Scripted(
        [
            tool_turn(("list_accounts", {}), ("get_notifications", {"account": "work", "limit": 5})),
            tool_turn(
                (
                    "reply",
                    {
                        "account": "work",
                        "parent_uri": "at://did:plc:fan/app.bsky.feed.post/n1",
                        "text": "Yes, 20% off with a .edu address.",
                    },
                )
            ),
            final("Replied to one question about discounts."),
        ]
    )
    state = MemoryObjectStore()
    work_pds = manager.client("work")._client
    work_pds.posts["at://did:plc:fan/app.bsky.feed.post/n1"] = make_post_view(
        "at://did:plc:fan/app.bsky.feed.post/n1", "cid-n1"
    )
    runner = AgentRunner(manager, provider=provider, state=state)
    task = TaskSpec(goal="Answer questions in mentions", accounts=["work"], allow_writes=True, name="answer")
    result = runner.run(task)

    assert result.status == "completed" and result.ok
    assert result.summary == "Replied to one question about discounts."
    assert (result.turns, result.tool_calls, result.writes) == (3, 3, 1)
    assert result.usage == {"input_tokens": 250, "output_tokens": 50}
    assert result.model == "script:test"

    # The system prompt carries the accounts and the policy; the first user message the goal.
    first = provider.calls[0]
    assert "- work: @work.test" in first["system"] and "personal" not in first["system"]
    assert "writes ENABLED (max 5 per run)" in first["system"]
    assert first["messages"][0].text.startswith("Task: Answer questions in mentions")
    assert "delete_post" not in first["tools"] and "reply" in first["tools"]

    # Tool results were fed back in order, with the untrusted-content note.
    results = {r.name: json.loads(r.content) for r in provider.calls[1]["messages"][-1].tool_results}
    assert results["get_notifications"]["note"].startswith("Content fetched")
    assert results["list_accounts"]["accounts"][0]["account"] == "work"

    # The reply actually went out, to the right parent.
    assert len(work_pds.sent) == 1 and work_pds.sent[0]["reply_to"].parent.cid == "cid-n1"

    # Transcript in the state store: task, turns, tool calls, result.
    events = RunLog.load(state, result.run_id)
    kinds = [e["type"] for e in events]
    assert kinds == ["task", "turn", "tool_call", "tool_call", "turn", "tool_call", "turn", "result"]
    assert events[-1]["status"] == "completed"


def test_dry_run_previews_writes(manager, fake_atproto):
    provider = Scripted([tool_turn(("post", {"account": "work", "text": "hi"})), final()])
    result = AgentRunner(manager, provider=provider).run(TaskSpec(goal="post hi", allow_writes=False))
    assert result.status == "completed" and result.writes == 0
    fed_back = next(iter(last_tool_results(provider).values()))
    assert fed_back["dry_run"] is True and fed_back["would"]["args"]["text"] == "hi"
    assert all(not c.sent for c in fake_atproto.instances)
    assert "dry run" in provider.calls[0]["messages"][0].text


def test_denied_calls_are_reported_to_the_model_not_raised(manager):
    provider = Scripted(
        [
            tool_turn(
                ("post", {"account": "personal", "text": "x"}), ("nonexistent_tool", {}), ("post", {"account": "work"})
            ),
            final(),
        ]
    )
    result = AgentRunner(manager, provider=provider).run(TaskSpec(goal="g", accounts=["work"], allow_writes=True))
    assert result.status == "completed" and result.writes == 0
    msg = provider.calls[1]["messages"][-1]
    assert all(r.is_error for r in msg.tool_results)
    errors = [json.loads(r.content)["error"] for r in msg.tool_results]
    assert "outside this run's scope" in errors[0]
    assert "Unknown tool" in errors[1]
    assert "Invalid arguments" in errors[2]


def test_turn_budget(manager):
    provider = Scripted([tool_turn(("list_accounts", {}))] * 5)
    result = AgentRunner(manager, provider=provider).run(TaskSpec(goal="loop", max_turns=3))
    assert result.status == "budget_exhausted" and result.turns == 3 and not result.ok


def test_tool_call_budget(manager):
    provider = Scripted([tool_turn(*[("list_accounts", {})] * 4), final()])
    result = AgentRunner(manager, provider=provider).run(TaskSpec(goal="g", max_tool_calls=2))
    assert result.status == "completed" and result.tool_calls == 2
    fed = provider.calls[1]["messages"][-1].tool_results
    assert [r.is_error for r in fed] == [False, False, True, True]
    assert "budget exhausted" in fed[2].content


def test_deadline(manager):
    provider = Scripted([tool_turn(("list_accounts", {}))] * 3)
    runner = AgentRunner(manager, provider=provider)
    result = runner.run(TaskSpec(goal="g", deadline_seconds=0))
    assert result.status == "deadline"


def test_model_errors_and_refusals_become_statuses(manager):
    refused = AgentRunner(manager, provider=Scripted([LLMRefusal("nope")])).run(TaskSpec(goal="g"))
    assert refused.status == "refused" and "nope" in refused.error
    errored = AgentRunner(manager, provider=Scripted([LLMError("down")])).run(TaskSpec(goal="g"))
    assert errored.status == "error" and errored.error == "down"
    crashed = AgentRunner(manager, provider=Scripted([RuntimeError("bug")])).run(TaskSpec(goal="g"))
    assert crashed.status == "error" and "RuntimeError: bug" in crashed.error


def test_missing_provider_is_a_clean_error(manager):
    def factory(spec, **options):
        raise LLMError(f"no key for {spec}")

    result = AgentRunner(manager, provider_factory=factory).run(TaskSpec(goal="g", model="openai:gpt"))
    assert result.status == "error" and result.error == "no key for openai:gpt"


def test_approval_workflow(manager, fake_atproto):
    state = MemoryObjectStore()
    provider = Scripted(
        [
            tool_turn(
                ("post", {"account": "work", "text": "draft A"}), ("post", {"account": "work", "text": "draft B"})
            ),
            final("Two posts queued."),
        ]
    )
    runner = AgentRunner(manager, provider=provider, state=state)
    result = runner.run(TaskSpec(goal="g", allow_writes=True, approval="writes"))
    assert result.status == "needs_approval" and result.ok
    assert result.pending_writes == 2 and result.writes == 0
    assert all(not c.sent for c in fake_atproto.instances)
    fed = next(iter(last_tool_results(provider).values()))
    assert fed["pending_approval"] is True

    pending = runner.pending_calls(result.run_id)
    assert [p["args"]["text"] for p in pending] == ["draft A", "draft B"]

    approved = runner.approve(result.run_id, call_ids=[pending[1]["call_id"]])
    assert len(approved) == 1 and approved[0]["ok"] and approved[0]["result"]["success"]
    work = next(c for c in fake_atproto.instances if c.sent)
    assert [s["text"] for s in work.sent] == ["draft B"]
    assert RunLog.load(state, result.run_id)[-1]["type"] == "approved_call"


def test_retried_run_does_not_repeat_writes(manager, fake_atproto):
    """Same idempotency key (a retry of one logical run) → the write is skipped."""
    state = MemoryObjectStore()
    script = lambda: [tool_turn(("post", {"account": "work", "text": "once"})), final()]
    task = lambda: TaskSpec(
        goal="g", allow_writes=True, name="nightly", idempotency_key="nightly@2026-03-02T09:00:00+00:00"
    )
    first = AgentRunner(manager, provider=Scripted(script()), state=state).run(task())
    second_provider = Scripted(script())
    second = AgentRunner(manager, provider=second_provider, state=state).run(task())
    assert first.writes == 1 and second.writes == 0
    fed = next(iter(last_tool_results(second_provider).values()))
    assert fed["skipped"] is True and fed["previous"]["run_id"] == first.run_id
    work = next(c for c in fake_atproto.instances if c.sent)
    assert len(work.sent) == 1


def test_same_task_name_on_a_new_day_may_repeat_a_write(manager, fake_atproto):
    """The ledger is scoped to a run, never to the bare task name."""
    state = MemoryObjectStore()
    script = lambda: [tool_turn(("post", {"account": "work", "text": "Good morning"})), final()]
    for day in ("2026-03-02", "2026-03-03"):
        r = AgentRunner(manager, provider=Scripted(script()), state=state).run(
            TaskSpec(goal="g", allow_writes=True, name="daily", idempotency_key=f"daily@{day}T09:00:00+00:00")
        )
        assert r.writes == 1
    # Two manual runs without a key are two runs, too.
    for _ in range(2):
        assert (
            AgentRunner(manager, provider=Scripted(script()), state=state)
            .run(TaskSpec(goal="g", allow_writes=True, name="daily"))
            .writes
            == 1
        )


def test_tool_schema_format_follows_the_provider(manager):
    class RawScripted(Scripted):
        def chat(self, system, messages, tools):
            self.calls.append({"tools": tools})
            return final()

    anthropic_style = RawScripted([])
    AgentRunner(manager, provider=anthropic_style).run(TaskSpec(goal="g"))
    assert "input_schema" in anthropic_style.calls[0]["tools"][0]

    openai_style = RawScripted([])
    openai_style.tool_format = "openai"
    AgentRunner(manager, provider=openai_style).run(TaskSpec(goal="g"))
    assert openai_style.calls[0]["tools"][0]["type"] == "function"


def test_on_event_streams_transcript(manager):
    seen = []
    provider = Scripted([tool_turn(("list_accounts", {})), final()])
    AgentRunner(manager, provider=provider, on_event=seen.append).run(TaskSpec(goal="g"))
    assert [e["type"] for e in seen] == ["task", "turn", "tool_call", "turn", "result"]


# ----------------------------------------------------------------------
# Chat providers: message conversion and response parsing
# ----------------------------------------------------------------------


def _conversation():
    return [
        Message(role="user", text="Task: go"),
        Message(role="assistant", text="Looking.", tool_calls=[ToolCall("c1", "list_accounts", {})]),
        Message(role="tool", tool_results=[ToolResult("c1", "list_accounts", '[{"account":"work"}]')]),
    ]


def test_anthropic_chat_conversion_and_parsing():
    require("anthropic")

    class Block(SimpleNamespace):
        def model_dump(self, exclude_none=True):
            return dict(vars(self))

    class FakeMessages:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(
                content=[
                    Block(type="text", text="Posting now."),
                    Block(type="tool_use", id="t1", name="post", input={"account": "work", "text": "hi"}),
                ],
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=12, output_tokens=3),
            )

    fake = SimpleNamespace(messages=FakeMessages())
    chat = AnthropicChat("claude-x", client=fake, effort="low")
    turn = chat.chat("SYS", _conversation(), [{"name": "post", "description": "d", "input_schema": {"type": "object"}}])
    assert turn.text == "Posting now." and turn.stop_reason == "tool_use"
    assert turn.tool_calls == [ToolCall("t1", "post", {"account": "work", "text": "hi"})]
    assert turn.usage == {"input_tokens": 12, "output_tokens": 3}
    assert turn.raw[1]["name"] == "post"

    req = fake.messages.requests[0]
    assert req["system"] == "SYS" and req["model"] == "claude-x" and req["output_config"] == {"effort": "low"}
    assert req["messages"][0] == {"role": "user", "content": "Task: go"}
    assert req["messages"][1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "list_accounts", "input": {}}
    assert req["messages"][2]["content"][0]["type"] == "tool_result"
    assert req["messages"][2]["content"][0]["tool_use_id"] == "c1"

    # A prior assistant turn with raw blocks is echoed back verbatim.
    msgs = [*_conversation(), Message(role="assistant", text="x", raw=turn.raw)]
    chat.chat("SYS", msgs, [])
    assert fake.messages.requests[1]["messages"][3]["content"] == turn.raw


def test_openai_chat_conversion_and_parsing(monkeypatch):
    import httpx

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, body=json, headers=headers)
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "post", "arguments": '{"account": "work", "text": "hi"}'},
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    chat = OpenAIChat("gpt-x", "https://example.test/v1", api_key="k", headers={"x-extra": "1"})
    turn = chat.chat("SYS", _conversation(), [{"type": "function", "function": {"name": "post"}}])
    assert turn.tool_calls == [ToolCall("call_1", "post", {"account": "work", "text": "hi"})]
    assert turn.stop_reason == "tool_use" and turn.usage == {"input_tokens": 7, "output_tokens": 2}
    assert captured["url"] == "https://example.test/v1/chat/completions"
    assert captured["headers"] == {"x-extra": "1", "Authorization": "Bearer k"}
    body = captured["body"]
    assert body["messages"][0] == {"role": "system", "content": "SYS"}
    assert body["messages"][2]["tool_calls"][0]["function"] == {"name": "list_accounts", "arguments": "{}"}
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": '[{"account":"work"}]'}
    assert body["tool_choice"] == "auto"


def test_chat_provider_factory(monkeypatch):
    register_chat_provider("script", lambda model, **o: Scripted([], model=f"script:{model}"))
    assert get_chat_provider("script:abc").model == "script:abc"

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://r.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "key")
    az = get_chat_provider("azure-openai:my-deployment")
    assert isinstance(az, OpenAIChat)
    assert az.base_url == "https://r.openai.azure.com/openai/v1" and az.headers == {"api-key": "key"}

    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    monkeypatch.delenv("AZURE_OPENAI_TOKEN", raising=False)
    import skyctl.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_azure_entra_token", lambda: None)
    with pytest.raises(LLMError, match="managed identity"):
        get_chat_provider("azure-openai:my-deployment")
    monkeypatch.setattr(llm_mod, "_azure_entra_token", lambda: "entra-token")
    assert get_chat_provider("azure-openai:my-deployment").headers == {"Authorization": "Bearer entra-token"}

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError):
        get_chat_provider("openai:gpt")
    with pytest.raises(LLMError):
        get_chat_provider("openai:")


def test_persona_llm_registry_knows_cloud_specs():
    from skyctl.llm import parse_model_spec

    assert parse_model_spec("bedrock:anthropic.claude-x") == ("bedrock", "anthropic.claude-x")
    assert parse_model_spec("vertex:claude-x") == ("vertex", "claude-x")
    assert parse_model_spec("azure-openai:dep") == ("azure-openai", "dep")
    assert parse_model_spec("foundry:dep") == ("foundry", "dep")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("BSKY_ACCOUNTS=work\nBSKY_WORK_HANDLE=work.test\nBSKY_WORK_PASSWORD=pw\n")
    for var in ("SKYCTL_SECRETS", "SKYCTL_STATE_URL", "SKYCTL_CLOUD", "SKYCTL_AGENT_ALLOW_WRITES"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_cli_task_run_and_show(cli_env, fake_atproto, monkeypatch):
    import skyctl.agent.runner as runner_mod

    script = [tool_turn(("post", {"account": "work", "text": "cli post"})), final("Posted.")]
    monkeypatch.setattr(runner_mod.AgentRunner, "_provider_for", lambda self, task: Scripted(list(script)))
    from skyctl.cli import cli

    runner = CliRunner()
    dry = runner.invoke(cli, ["task", "run", "post something", "--json"])
    assert dry.exit_code == 0, dry.output
    data = json.loads(dry.output)
    assert data["status"] == "completed" and data["writes"] == 0

    live = runner.invoke(cli, ["task", "run", "post something", "--allow-writes", "--max-writes", "1", "--json"])
    assert live.exit_code == 0, live.output
    data = json.loads(live.output)
    assert data["writes"] == 1 and data["task"]["max_writes"] == 1
    assert (cli_env / ".skyctl" / "runs" / f"{data['run_id']}.jsonl").exists()

    shown = runner.invoke(cli, ["task", "show", data["run_id"]])
    assert shown.exit_code == 0 and "post" in shown.output and "completed" in shown.output

    both = runner.invoke(cli, ["task", "run", "goal", "--task", "x"])
    assert both.exit_code == 1


def test_cli_task_list_and_named_task(cli_env, monkeypatch):
    (cli_env / "tasks.toml").write_text('[tasks.daily]\ngoal = "Engage"\naccounts = ["work"]\nallow_writes = true\n')
    import skyctl.agent.runner as runner_mod

    monkeypatch.setattr(runner_mod.AgentRunner, "_provider_for", lambda self, task: Scripted([final("ok")]))
    from skyctl.cli import cli

    runner = CliRunner()
    listed = runner.invoke(cli, ["task", "list", "--json"])
    assert listed.exit_code == 0 and json.loads(listed.output)["daily"]["allow_writes"] is True
    ran = runner.invoke(cli, ["task", "run", "--task", "daily", "--dry-run", "--json"])
    assert ran.exit_code == 0, ran.output
    assert json.loads(ran.output)["task"]["dry_run"] is True
    missing = runner.invoke(cli, ["task", "run", "--task", "nope", "--json"])
    assert missing.exit_code == 1 and "not found" in missing.output


def test_cli_tick(cli_env, fake_atproto):
    from skyctl.cli import cli

    runner = CliRunner()
    past = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "")
    assert runner.invoke(cli, ["schedule", "work", "late post", past]).exit_code == 0
    ticked = runner.invoke(cli, ["tick", "--json"])
    assert ticked.exit_code == 0, ticked.output
    out = json.loads(ticked.output)
    assert len(out["sent"]) == 1 and out["failed"] == []
    assert any(c.sent and c.sent[0]["text"] == "late post" for c in fake_atproto.instances)


def test_a_failed_write_is_not_recorded_in_the_ledger(manager, fake_atproto):
    """Regression: a retry must re-attempt a write that failed, not skip it as 'already made'."""
    state = MemoryObjectStore()
    fake = manager.client("work").login()._client
    fake.fail_sends = 1
    script = lambda: [tool_turn(("post", {"account": "work", "text": "important"})), final()]
    task = lambda: TaskSpec(goal="g", allow_writes=True, idempotency_key="k1")
    first = AgentRunner(manager, provider=Scripted(script()), state=state).run(task())
    assert fake.sent == [] and first.writes == 1  # attempted, failed
    AgentRunner(manager, provider=Scripted(script()), state=state).run(task())
    assert [s["text"] for s in fake.sent] == ["important"]


def test_cli_plan_review_flow(cli_env, fake_atproto):
    from skyctl import BskyManager
    from skyctl.adapters import object_store_for
    from skyctl.cli import cli
    from skyctl.planning import Plan, PlanStore, check_plan
    from skyctl.tools import ToolContext, ToolPolicy, registry

    manager = BskyManager.from_env()
    state = object_store_for("file://./.skyctl")
    ctx = ToolContext(manager, policy=ToolPolicy(allow_writes=True))
    p = Plan.model_validate(
        {
            "goal": "Say hi",
            "steps": [
                {"id": "a", "tool": "post", "args": {"account": "work", "text": "hi from a plan"}, "why": "greeting"}
            ],
        }
    )
    rec = PlanStore(state).create(p, check_plan(p, ctx, registry), created_by="agent", approval_mode="out_of_band")
    runner = CliRunner()
    listed = runner.invoke(cli, ["plan", "list", "--json"])
    assert listed.exit_code == 0 and json.loads(listed.output)[0]["status"] == "awaiting_approval"
    shown = runner.invoke(cli, ["plan", "show", rec["id"]])
    assert "hi from a plan" in shown.output and "[a] post as @work.test" in shown.output
    declined = runner.invoke(cli, ["plan", "approve", rec["id"]], input="n\n")
    assert "Not approved" in declined.output
    approved = runner.invoke(cli, ["plan", "approve", rec["id"], "--execute"], input="y\n")
    assert approved.exit_code == 0, approved.output
    assert "approved by cli:" in approved.output and "completed" in approved.output
    assert runner.invoke(cli, ["plan", "cancel", rec["id"]]).exit_code == 1  # already completed
