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
Every CLI command, human and --json output, success and failure exit codes.
The CLI is the product's main surface, so each command is exercised here end to end.
"""

import base64
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from conftest import make_post_view
from skyctl.cli import cli

EXAMPLES = Path(__file__).resolve().parents[1] / "deploy" / "examples"
ROOT = "at://did:plc:alice/app.bsky.feed.post/root"


@pytest.fixture
def run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key, value in {
        "BSKY_ACCOUNTS": "personal,work",
        "BSKY_PERSONAL_HANDLE": "me.test",
        "BSKY_PERSONAL_PASSWORD": "xxxx",
        "BSKY_WORK_HANDLE": "work.test",
        "BSKY_WORK_PASSWORD": "xxxx",
        "SKYCTL_CONFIG_DIR": str(tmp_path / "config"),
    }.items():
        monkeypatch.setenv(key, value)
    runner = CliRunner()
    return lambda *args, **kw: runner.invoke(cli, [str(a) for a in args], **kw)


def sent(fake):
    return [s for inst in fake.instances for s in inst.sent]


def seed_root(fake):
    """Make ROOT fetchable on every fake client created from now on."""
    original = fake.__init__

    def init(self, *a, **k):
        original(self, *a, **k)
        self.posts[ROOT] = make_post_view(ROOT, "cid-root")

    fake.__init__ = init


# ----------------------------------------------------------------------
# Basics
# ----------------------------------------------------------------------


def test_version_and_help(run):
    from skyctl import __version__

    assert run("--version").output.strip() == f"skyctl {__version__}"
    out = run("--help").output
    for command in ("run-cloud-task", "mcp", "plan", "deploy", "eval", "cloud", "platform", "task", "serve"):
        assert command in out


def test_accounts_profile_timeline_notifications(run, fake_atproto):
    accounts = run("accounts", "--json")
    assert json.loads(accounts.output) == [
        {"alias": "personal", "handle": "me.test"},
        {"alias": "work", "handle": "work.test"},
    ]
    assert "work.test" in run("accounts").output
    assert run("timeline", "personal", "--json").exit_code == 0
    assert "timeline post" in run("timeline", "personal").output
    notes = run("notifications", "work", "--json")
    assert json.loads(notes.output)[0]["reason"] == "mention"
    assert "mention" in run("notifications", "work").output


def test_profile(run, fake_atproto, monkeypatch):
    from types import SimpleNamespace

    profile = {
        "handle": "me.test",
        "display_name": "Me",
        "description": "hello",
        "followers_count": 3,
        "follows_count": 4,
        "posts_count": 5,
    }
    monkeypatch.setattr(
        fake_atproto, "get_profile", lambda self, handle: SimpleNamespace(model_dump=lambda: profile), raising=False
    )
    assert json.loads(run("profile", "personal", "--json").output)["display_name"] == "Me"
    assert "Me" in run("profile", "personal").output


def test_post_all_thread_reply_repost_like(run, fake_atproto):
    seed_root(fake_atproto)
    assert run("post-all", "hello everyone", "--exclude", "work").exit_code == 0
    assert [s["text"] for s in sent(fake_atproto)] == ["hello everyone"]
    out = json.loads(run("post-all", "again", "--json").output)
    assert [r["account"] for r in out] == ["personal", "work"]
    assert run("post-all", "x" * 301).exit_code == 1
    assert "Dry run" in run("post-all", "preview only", "--dry-run").output
    thread = json.loads(run("thread", "work", "word " * 100, "--json").output)
    assert thread["success"] and thread["planned"] == 2 and len(thread["posts"]) == 2
    assert run("reply", "work", ROOT, "nice").exit_code == 0
    assert json.loads(run("repost", "work", ROOT, "--json").output)["success"] is True
    assert run("like", "work", ROOT).exit_code == 0
    assert run("like", "work", "at://did:plc:x/app.bsky.feed.post/missing").exit_code == 1


def test_jobs_and_cancel(run):
    assert run("schedule", "work", "later", "2031-01-01T09:00:00").exit_code == 0
    jobs = json.loads(run("jobs", "--json").output)
    assert len(jobs) == 1
    assert "later" in run("jobs").output
    assert run("cancel", jobs[0]["id"]).exit_code == 0
    assert json.loads(run("jobs", "--json").output) == []
    assert run("cancel", "nope").exit_code == 1


def test_logout(run, fake_atproto):
    run("post", "work", "hi")
    assert "Cleared session for work" in run("logout", "work").output


# ----------------------------------------------------------------------
# Cloud entry points: tick, invoke, worker, platform
# ----------------------------------------------------------------------


def test_tick_human_output(run):
    result = run("tick")
    assert result.exit_code == 0 and "sent 0, failed 0" in result.output


def test_invoke_payload_file_and_errors(run, tmp_path):
    ok = run("invoke", '{"kind": "tick"}')
    assert ok.exit_code == 0 and json.loads(ok.output[ok.output.index("{") :])["ok"] is True
    push = {
        "message": {"data": base64.b64encode(b'{"kind": "tick"}').decode(), "messageId": "1"},
        "subscription": "projects/p/subscriptions/s",
    }
    (tmp_path / "push.json").write_text(json.dumps(push))
    assert run("invoke", "--file", tmp_path / "push.json").exit_code == 0
    assert run("invoke", '{"kind": "nope"}').exit_code == 1
    assert run("invoke", "not json").exit_code == 1
    assert run("invoke").exit_code == 1


def test_worker_once(run, monkeypatch):
    monkeypatch.setenv("SKYCTL_QUEUE_URL", "memory://cli-worker")
    from skyctl.adapters.queues import queue_for

    queue_for("memory://cli-worker").send({"kind": "tick"})
    result = run("worker", "--once")
    assert result.exit_code == 0 and '"succeeded": 1' in result.output
    import skyctl.platform.runtime as runtime_mod
    import skyctl.settings as settings_mod

    monkeypatch.delenv("SKYCTL_QUEUE_URL")
    # Each real CLI invocation is a fresh process: drop the caches a second in-process call would reuse.
    monkeypatch.setattr(runtime_mod, "_runtime", None)
    monkeypatch.setattr(settings_mod, "_loaded", None)
    assert run("worker", "--once").exit_code == 1


def test_platform_info_and_list(run, monkeypatch):
    info = json.loads(run("platform", "info", "--json").output)
    assert info["platform"] == "local" and "toolset" in info
    assert "Local process" in run("platform", "info").output
    monkeypatch.setenv("K_SERVICE", "svc")
    assert json.loads(run("platform", "info", "--json").output)["compute"] == "cloud-run"
    rows = json.loads(run("platform", "list", "--json").output)
    assert {"aws", "azure", "gcp", "kubernetes"} <= {r["platform"] for r in rows}
    assert "Platforms" in run("platform", "list").output


# ----------------------------------------------------------------------
# Deployment specs
# ----------------------------------------------------------------------


def test_deploy_validate_render_check(run, tmp_path):
    spec = EXAMPLES / "managed.deploy.toml"
    assert run("deploy", "validate", spec, "--json").exit_code == 0
    assert "info" in run("deploy", "validate", spec, "-t", "compose").output
    out = tmp_path / "build"
    rendered = run("deploy", "render", spec, "--target", "gcp", "--target", "compose", "--out", out)
    assert (
        rendered.exit_code == 0
        and (out / "gcp" / "main.tf.json").exists()
        and (out / "compose" / "compose.json").exists()
    )
    assert run("deploy", "render", spec, "--out", out).exit_code == 1  # no target chosen
    checked = run("deploy", "check", spec)
    assert checked.exit_code == 0 and "Conformance" in checked.output
    assert json.loads(run("deploy", "check", spec, "--json").output)["checks"]
    bad = tmp_path / "bad.toml"
    bad.write_text(spec.read_text().replace('timezone = "UTC"              #', 'timezone = "Europe/London" #'))
    assert run("deploy", "check", bad).exit_code == 1
    assert run("deploy", "render", bad, "--target", "azure", "--out", out).exit_code == 1
    assert run("deploy", "render", bad, "--target", "azure", "--out", out, "--force").exit_code == 0
    broken = tmp_path / "broken.toml"
    broken.write_text("[app]\nname = 'X'\n")
    assert run("deploy", "validate", broken).exit_code == 1


# ----------------------------------------------------------------------
# Task agent: tasks list, run with approval, approve, show
# ----------------------------------------------------------------------


@pytest.fixture
def scripted(monkeypatch):
    import skyctl.agent.runner as runner_mod
    from skyctl.ports import ChatTurn, ToolCall

    class Poster:
        model, tool_format = "script:poster", "anthropic"

        def chat(self, system, messages, tools):
            if len(messages) == 1:
                return ChatTurn(
                    "posting", [ToolCall("c", "post", {"account": "work", "text": "from a task"})], "tool_use"
                )
            return ChatTurn("done", [], "end")

    monkeypatch.setattr(runner_mod.AgentRunner, "_provider_for", lambda self, task: Poster())


def test_task_list_and_approval_flow(run, fake_atproto, scripted, tmp_path):
    assert run("task", "list").exit_code == 1  # no tasks.toml
    (tmp_path / "tasks.toml").write_text('[tasks.daily]\ngoal = "Post"\naccounts = ["work"]\nallow_writes = true\n')
    assert "daily" in run("task", "list").output
    held = run("task", "run", "post something", "--allow-writes", "--approval", "writes", "-v")
    assert held.exit_code == 0 and "needs_approval" in held.output and "~ post" in held.output
    run_id = next(p.stem for p in (tmp_path / ".skyctl" / "runs").glob("*.jsonl"))
    assert run("task", "approve", run_id, input="n\n").output.strip().endswith("Nothing sent.")
    approved = run("task", "approve", run_id, "--json")
    assert approved.exit_code == 0 and json.loads(approved.output)[0]["ok"] is True
    assert [s["text"] for s in sent(fake_atproto)] == ["from a task"]
    assert "turn 1" in run("task", "show", run_id).output
    assert run("task", "show", "nope").exit_code == 1
    assert run("task", "approve", "nope").exit_code == 1
    assert run("task", "run", "x", "--context", "novalue").exit_code == 1


def test_plan_execute_command(run, fake_atproto, tmp_path):
    from skyctl import BskyManager
    from skyctl.adapters import object_store_for
    from skyctl.planning import Plan, PlanStore, check_plan
    from skyctl.tools import ToolContext, ToolPolicy, registry

    ctx = ToolContext(BskyManager.from_env(), policy=ToolPolicy(allow_writes=True))
    p = Plan.model_validate(
        {"goal": "g", "steps": [{"id": "a", "tool": "post", "args": {"account": "work", "text": "planned"}}]}
    )
    store = PlanStore(object_store_for("file://./.skyctl"))
    awaiting = store.create(p, check_plan(p, ctx, registry), created_by="agent", approval_mode="out_of_band")
    assert run("plan", "execute", awaiting["id"]).exit_code == 1  # not approved
    ready = store.create(p, check_plan(p, ctx, registry), created_by="agent", approval_mode="none")
    done = run("plan", "execute", ready["id"], "--json")
    assert done.exit_code == 0 and json.loads(done.output)["status"] == "completed"
    assert "completed" in run("plan", "show", ready["id"]).output
    assert json.loads(run("plan", "show", ready["id"], "--json").output)["id"] == ready["id"]
    assert "Plans" in run("plan", "list").output
    assert run("plan", "show", "nope").exit_code == 1


# ----------------------------------------------------------------------
# Evaluations through the CLI
# ----------------------------------------------------------------------


def test_eval_run_with_a_registered_model(run):
    from skyctl.agent.chat import register_chat_provider
    from skyctl.testing.agents import FactCheckerAgent

    register_chat_provider(
        "cli-eval",
        lambda model, **kw: FactCheckerAgent(
            "Riverside transit strike", ("strike is over",), news_query="Riverside transit strike"
        ),
    )
    ok = run("eval", "run", "--model", "cli-eval:x", "--scenario", "correct-misinformation-in-comments", "--json")
    assert ok.exit_code == 0, ok.output
    report = json.loads(ok.output)
    assert report["pass_all_n"] == 1.0 and report["model"] == "cli-eval:x"
    table = run("eval", "run", "--model", "cli-eval:x", "--scenario", "correct-misinformation-in-comments")
    assert "pass^1 100%" in table.output
    failing = run("eval", "run", "--model", "cli-eval:x", "--scenario", "ambiguous-account")
    assert failing.exit_code == 1
    assert run("eval", "run", "--model", "openai:gpt").exit_code == 1  # no OPENAI_API_KEY
    assert "correct-misinformation-in-comments" in run("eval", "list").output


# ----------------------------------------------------------------------
# run-cloud-task: human output for each outcome
# ----------------------------------------------------------------------


def test_run_cloud_task_no_wait_and_timeout_outputs(run, monkeypatch):
    import skyctl.remote as remote

    class Stub:
        name = "stub"

        def submit_task(self, task):
            return "run-1"

        def run_events(self, run_id):
            return [{"type": "queued", "run_id": run_id}]

    monkeypatch.setattr(remote, "resolve_profile", lambda name: remote.CloudProfile(name=name, transport="local"))
    monkeypatch.setattr(remote, "connect", lambda profile: Stub())
    queued = run("run-cloud-task", "--cloud", "aws", "-d", "say hi", "--no-wait")
    assert queued.exit_code == 0 and "skyctl cloud status run-1 --cloud aws" in queued.output
    timeout = run("run-cloud-task", "--cloud", "aws", "-d", "say hi", "--timeout", "0", "--poll", "0")
    assert timeout.exit_code == 2 and "timeout" in timeout.output
    assert run("cloud", "status", "run-1", "--cloud", "aws").output.startswith("run-1: running or queued")
