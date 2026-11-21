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
`skyctl run_cloud_task --cloud aws -- description "Find a post about Trump, look at the comments and respond to
any spewing blatant misinformation with the facts from the most recent news"`, end to end.

The operator's CLI runs in this process. "The cloud" is a worker thread running skyctl's real queue worker
and dispatcher. Both talk through the real AWS adapters (S3 for state, SQS for the queue) against moto.
Bluesky is a simulated World; the model is a scripted fact-checker using the real tools.
"""

import json
import threading
import time

import pytest
from click.testing import CliRunner

from conftest import require
from skyctl.testing.agents import FactCheckerAgent
from skyctl.testing.world import World

REQUEST = (
    "Find a post about Trump, look at the comments and respond to any spewing blatant misinformation "
    "with the facts from the most recent news"
)
ARTICLE = "https://news.example/riverside-forum-friday"


def build_world() -> World:
    """Bland, fictional event logistics: the 'misinformation' is a cancelled-event rumour."""
    world = World()
    world.add_account("newsdesk.test")
    world.add_post("events.test", "Trump to speak at the Riverside economic forum on Friday", key="topic")
    world.reply("topic", "rumour.test", "The Riverside forum was cancelled, there is no event anymore.", key="false")
    world.reply("topic", "opinion.test", "I think these forums are a waste of time.", key="opinion")
    world.reply("topic", "rumour.test", "Seriously, it was cancelled, stop sharing this.", key="false_again")
    world.add_article(
        ARTICLE,
        "Riverside economic forum goes ahead on Friday",
        "The Riverside economic forum will go ahead on Friday as planned, organisers confirmed on "
        "Monday. The speaker list is unchanged.",
        source="news.example",
    )
    return world


def fact_checker():
    return FactCheckerAgent(topic="Trump", false_markers=("cancelled",), news_query="Riverside economic forum")


@pytest.fixture
def aws_deployment(monkeypatch, tmp_path):
    """moto S3 + SQS, a cloud worker thread, and an `aws` profile pointing at them."""
    moto = require("moto")
    boto3 = require("boto3")
    for key, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "SKYCTL_CONFIG_DIR": str(tmp_path / "config"),
    }.items():
        monkeypatch.setenv(key, value)
    with moto.mock_aws():
        boto3.client("s3").create_bucket(Bucket="skyctl-state")
        queue_url = boto3.client("sqs").create_queue(QueueName="skyctl-work", Attributes={"VisibilityTimeout": "60"})[
            "QueueUrl"
        ]
        state_url = "s3://skyctl-state/skyctl"
        sqs_url = "sqs://" + queue_url.split("://", 1)[1]
        monkeypatch.setenv("SKYCTL_CLOUD_AWS_STATE_URL", state_url)
        monkeypatch.setenv("SKYCTL_CLOUD_AWS_QUEUE_URL", sqs_url)

        # ---- the cloud side -------------------------------------------------------------
        from skyctl import BskyManager
        from skyctl.agent import AgentRunner
        from skyctl.models import AccountConfig
        from skyctl.platform import Runtime, get_platform, services
        from skyctl.platform.invocation import default_dispatcher, run_worker
        from skyctl.settings import AgentDefaults, Settings

        world = build_world()
        manager = BskyManager(
            {"work": AccountConfig("work", "newsdesk.test", "pw")}, atproto_factory=world.client_factory
        )
        settings = Settings(
            state_url=state_url,
            queue_url=sqs_url,
            agent=AgentDefaults(allow_writes=True, max_writes=5, accounts=["work"]),
        )
        runtime = Runtime(settings, platform=get_platform("aws"), compute="ecs")
        search, fetcher = world.research()
        runner = AgentRunner(
            manager,
            state=runtime.object_store(),
            search=search,
            fetcher=fetcher,
            provider_factory=lambda spec, **kw: fact_checker(),
        )  # a fresh "model" per run
        dispatcher = default_dispatcher(runtime, manager=manager, runner=runner)
        stop = threading.Event()
        worker = threading.Thread(
            target=run_worker,
            args=(services.build("queue", sqs_url), dispatcher),
            kwargs={"idle_exit_seconds": None, "stop": stop.is_set, "wait_seconds": 0, "heartbeat_seconds": None},
            daemon=True,
        )
        worker.start()
        try:
            yield {"world": world, "state_url": state_url, "runtime": runtime}
        finally:
            stop.set()
            worker.join(timeout=10)


def _invoke(args, input=None):
    from skyctl.cli import cli

    return CliRunner().invoke(cli, args, input=input, catch_exceptions=False)


def test_the_exact_command_plans_in_aws_and_waits_for_approval(aws_deployment):
    world = aws_deployment["world"]
    result = _invoke(
        ["run_cloud_task", "--cloud", "aws", "--poll", "0.2", "--timeout", "60", "--json", "--", "description", REQUEST]
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    # Non-interactive: never approved silently; the plan waits in the state bucket.
    assert out["status"] == "awaiting_approval" and out["plan_id"] and out["cloud"] == "aws"
    assert world.writes == []

    from skyctl.planning import PlanStore
    from skyctl.platform import services

    record = PlanStore(services.build("state", aws_deployment["state_url"])).get(out["plan_id"])
    steps = record["plan"]["steps"]
    # One reply per author, only to the false claim, citing the article it actually read.
    assert [s["args"]["parent_uri"] for s in steps] == [world.uri("false")]
    assert ARTICLE in steps[0]["args"]["text"] and "cancelled" in steps[0]["why"]
    assert record["approval"]["mode"] == "out_of_band" and record["status"] == "awaiting_approval"

    # The operator approves later; the deployment executes with its own identity.
    approved = _invoke(["cloud", "approve", out["plan_id"], "--cloud", "aws", "--yes"])
    assert approved.exit_code == 0, approved.output
    deadline = time.time() + 30
    while (
        time.time() < deadline
        and PlanStore(services.build("state", aws_deployment["state_url"])).get(out["plan_id"])["status"] != "completed"
    ):
        time.sleep(0.2)
    done = PlanStore(services.build("state", aws_deployment["state_url"])).get(out["plan_id"])
    assert done["status"] == "completed" and done["approval"]["by"].startswith("cli:")
    assert [w["reply_to"] for w in world.writes if w["op"] == "post"] == [world.uri("false")]
    assert [e["type"] for e in done["events"]][:2] == ["created", "approved"]


def test_auto_approve_runs_the_whole_loop(aws_deployment):
    world = aws_deployment["world"]
    result = _invoke(
        [
            "run-cloud-task",
            "--cloud",
            "aws",
            "--description",
            REQUEST,
            "--approve",
            "auto",
            "--poll",
            "0.2",
            "--timeout",
            "60",
            "--json",
        ]
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["status"] == "completed" and out["plan_status"] == "completed"
    [step] = out["steps"].values()
    assert step["status"] == "done" and step["url"].startswith("https://bsky.app/profile/")
    posted = [w for w in world.writes if w["op"] == "post"]
    assert len(posted) == 1 and ARTICLE in posted[0]["text"] and posted[0]["reply_to"] == world.uri("false")


def test_human_output_shows_progress_and_the_plan(aws_deployment):
    result = _invoke(
        ["run_cloud_task", "--cloud", "aws", "--poll", "0.2", "--timeout", "60", "--", "description", REQUEST]
    )
    assert result.exit_code == 0, result.output
    text = result.output
    assert "citations required" in text  # auto-on: the request mentions news/facts
    for tool in ("search_posts", "get_post_thread", "web_search", "fetch_page", "plan_create"):
        assert tool in text
    assert "proposed plan" in text and "reply as @newsdesk.test" in text and "awaiting_approval" in text
    # The reviewer sees what is being answered and that the cited source was actually read.
    assert "in reply to @rumour.test" in text and "was cancelled" in text
    assert f"link: {ARTICLE} (retrieved in this run)" in text
    assert "skyctl cloud approve" in text


def test_no_wait_returns_immediately(aws_deployment):
    result = _invoke(["run-cloud-task", "--cloud", "aws", "-d", REQUEST, "--no-wait", "--json"])
    out = json.loads(result.output)
    assert out["status"] == "queued" and out["run_id"]
    deadline = time.time() + 30
    status = {}
    while time.time() < deadline:
        status = json.loads(_invoke(["cloud", "status", out["run_id"], "--cloud", "aws", "--json"]).output)
        if status["status"] != "queued":
            break
        time.sleep(0.2)
    assert status["status"] == "needs_approval" and status["result"]["plan_id"]


# ----------------------------------------------------------------------
# Argument handling
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((None, ("--", "description", "do X")), "do X"),  # as written in the request, after click strips "--"
        ((None, ("description", "do X")), "do X"),
        (("do X", ()), "do X"),
        ((None, ("do", "X")), "do X"),
        ((None, ("--description", "do X")), "do X"),
    ],
)
def test_parse_description(args, expected):
    from skyctl.remote import parse_description

    description, words = args
    words = tuple(w for w in words if w != "--")
    assert parse_description(description, words) == expected


def test_parse_description_errors():
    from skyctl.remote import RemoteError, parse_description

    with pytest.raises(RemoteError, match="either"):
        parse_description("a", ("b",))
    with pytest.raises(RemoteError, match="Describe the task"):
        parse_description(None, ("description",))


def test_citation_heuristic():
    from skyctl.remote import wants_citations

    assert wants_citations(REQUEST)
    assert wants_citations("fact-check the replies")
    assert not wants_citations("thank everyone who replied to my launch post")


def test_unknown_cloud_explains_how_to_connect(monkeypatch, tmp_path):
    monkeypatch.setenv("SKYCTL_CONFIG_DIR", str(tmp_path))
    result = CliRunner().invoke(
        __import__("skyctl.cli", fromlist=["cli"]).cli, ["run_cloud_task", "--cloud", "gcp", "--", "description", "x"]
    )
    assert result.exit_code == 1 and "skyctl cloud import gcp" in result.output


def test_alias_is_hidden_but_works():
    from skyctl.cli import cli

    help_text = CliRunner().invoke(cli, ["--help"]).output
    assert "run-cloud-task" in help_text and "run_cloud_task" not in help_text
    assert "--description" in CliRunner().invoke(cli, ["run_cloud_task", "--help"]).output


# ----------------------------------------------------------------------
# Other transports: local (this machine) and http (a deployment's `skyctl serve`)
# ----------------------------------------------------------------------


def _world_runner(state):
    from skyctl import BskyManager
    from skyctl.agent import AgentRunner
    from skyctl.models import AccountConfig

    world = build_world()
    manager = BskyManager({"work": AccountConfig("work", "newsdesk.test", "pw")}, atproto_factory=world.client_factory)
    search, fetcher = world.research()
    runner = AgentRunner(
        manager, state=state, search=search, fetcher=fetcher, provider_factory=lambda spec, **kw: fact_checker()
    )
    return world, manager, runner


def test_local_connection_runs_the_same_loop():
    from skyctl.adapters import MemoryObjectStore
    from skyctl.agent import TaskSpec
    from skyctl.remote import LocalConnection, run_cloud_task
    from skyctl.tools import ToolContext, ToolPolicy

    world, manager, runner = _world_runner(MemoryObjectStore())
    conn = LocalConnection(
        runner=runner, ctx_factory=lambda: ToolContext(manager, policy=ToolPolicy(allow_writes=True))
    )
    task = TaskSpec(goal=REQUEST, allow_writes=True, mode="plan", plan_approval="out_of_band", require_citations=True)
    asked = []
    out = run_cloud_task(
        conn, task, approve="ask", confirm=lambda rec: asked.append(rec["id"]) or True, poll=0.05, timeout=30
    )
    assert out["status"] == "completed" and asked == [out["plan_id"]]
    assert [w["reply_to"] for w in world.writes if w["op"] == "post"] == [world.uri("false")]
    declined = run_cloud_task(conn, task, approve="ask", confirm=lambda rec: False, poll=0.05, timeout=30)
    assert declined["status"] == "awaiting_approval"
    assert len([w for w in world.writes if w["op"] == "post"]) == 1


def test_http_connection_against_a_real_serve_app(monkeypatch):
    from starlette.testclient import TestClient

    from skyctl.adapters import MemoryObjectStore
    from skyctl.agent import TaskSpec
    from skyctl.platform import Runtime, get_platform
    from skyctl.platform.auth import build_auth_chain
    from skyctl.remote import CloudProfile, HttpConnection, RemoteError, run_cloud_task
    from skyctl.server import create_app
    from skyctl.settings import AgentDefaults, Settings

    monkeypatch.setenv("SKYCTL_HTTP_TOKEN", "agent-token")
    monkeypatch.setenv("SKYCTL_APPROVER_TOKEN", "operator-token")
    state = MemoryObjectStore()
    world, manager, runner = _world_runner(state)
    settings = Settings(
        http_token="agent-token",
        state_url="memory://x",
        agent=AgentDefaults(allow_writes=True, max_writes=5, accounts=["work"]),
    )
    runtime = Runtime(settings, platform=get_platform("local"))
    runtime._state = state
    app = create_app(
        settings, manager=manager, runner=runner, runtime=runtime, mcp_server=False, auth_chain=build_auth_chain()
    )
    with TestClient(app, base_url="http://skyctl.test") as client:
        profile = CloudProfile(name="staging", transport="http", endpoint="http://skyctl.test")
        conn = HttpConnection(profile, client=client)
        task = TaskSpec(
            goal=REQUEST, allow_writes=True, mode="plan", plan_approval="out_of_band", require_citations=True
        )
        out = run_cloud_task(conn, task, approve="auto", poll=0.05, timeout=30)
        assert out["status"] == "completed", out
        assert [w["reply_to"] for w in world.writes if w["op"] == "post"] == [world.uri("false")]
        # Without the approver token, the HTTP path cannot approve: the agent token is not enough.
        monkeypatch.delenv("SKYCTL_APPROVER_TOKEN")
        with pytest.raises(RemoteError, match="SKYCTL_APPROVER_TOKEN"):
            run_cloud_task(HttpConnection(profile, client=client), task, approve="auto", poll=0.05, timeout=30)


# ----------------------------------------------------------------------
# Profiles
# ----------------------------------------------------------------------


def test_cloud_profiles_add_import_list_remove(monkeypatch, tmp_path):
    from skyctl.cli import cli

    monkeypatch.setenv("SKYCTL_CONFIG_DIR", str(tmp_path))
    run = lambda *a: CliRunner().invoke(cli, list(a))
    assert (
        run(
            "cloud",
            "add",
            "aws",
            "--state-url",
            "s3://b/skyctl",
            "--queue-url",
            "sqs://sqs.us-east-1.amazonaws.com/1/q",
            "--env",
            "AWS_REGION=us-east-1",
        ).exit_code
        == 0
    )
    outputs = {
        "skyctl_connection": {
            "value": {
                "platform": "gcp",
                "state_url": "gs://p-skyctl-state/skyctl",
                "queue_url": "pubsub://p/skyctl-work?subscription=skyctl-work-push",
                "env": {"GOOGLE_CLOUD_PROJECT": "p"},
            }
        },
        "service_url": {"value": "https://skyctl-svc-123.europe-west1.run.app"},
    }
    (tmp_path / "out.json").write_text(json.dumps(outputs))
    imported = run("cloud", "import", "gcp", "--outputs-file", str(tmp_path / "out.json"))
    assert imported.exit_code == 0, imported.output
    listed = json.loads(run("cloud", "list", "--json").output)
    assert listed["aws"]["env"] == {"AWS_REGION": "us-east-1"} and listed["gcp"]["endpoint"].startswith(
        "https://skyctl-svc"
    )
    text = (tmp_path / "clouds.toml").read_text()
    assert "token" not in text.replace("token_env", "").replace("approver_token_env", "")  # no secrets stored
    assert run("cloud", "remove", "aws").exit_code == 0 and "aws" not in json.loads(
        run("cloud", "list", "--json").output
    )
    assert run("cloud", "remove", "aws").exit_code == 1
    assert run("cloud", "add", "bad", "--state-url", "s3://b").exit_code == 1  # neither queue nor endpoint
    assert (
        run(
            "cloud", "import", "x", "--outputs-file", str(tmp_path / "out.json"), "--terraform-dir", str(tmp_path)
        ).exit_code
        == 1
    )


def test_generated_terraform_exposes_the_connection():
    from pathlib import Path

    from skyctl.deploy import load_spec, render

    spec = load_spec(Path(__file__).resolve().parents[1] / "deploy" / "examples" / "managed.deploy.toml")
    for target, env_key in (
        ("aws", "AWS_REGION"),
        ("azure", "AZURE_STORAGE_ACCOUNT_NAME"),
        ("gcp", "GOOGLE_CLOUD_PROJECT"),
    ):
        conn = render(spec, target).terraform["output"]["skyctl_connection"]["value"]
        assert conn["platform"] == target and conn["state_url"] and conn["queue_url"] and env_key in conn["env"]
    aws = render(spec, "aws").terraform
    policy = json.loads(aws["resource"]["aws_iam_policy"]["operator"]["policy"])
    assert {
        a for st in policy["Statement"] for a in (st["Action"] if isinstance(st["Action"], list) else [st["Action"]])
    } == {"sqs:SendMessage", "s3:GetObject", "s3:PutObject", "s3:ListBucket"}


def test_plan_execute_invocation_requires_an_approved_plan():
    from skyctl.adapters import MemoryObjectStore
    from skyctl.planning import Plan, PlanStore, check_plan
    from skyctl.platform import Runtime, get_platform
    from skyctl.platform.invocation import Invocation, default_dispatcher
    from skyctl.settings import AgentDefaults, Settings
    from skyctl.tools import ToolContext, ToolPolicy, registry

    state = MemoryObjectStore()
    _world, manager, runner = _world_runner(state)
    runtime = Runtime(
        Settings(state_url="memory://x", agent=AgentDefaults(allow_writes=True)), platform=get_platform("local")
    )
    runtime._state = state
    d = default_dispatcher(runtime, manager=manager, runner=runner)
    ctx = ToolContext(manager, policy=ToolPolicy(allow_writes=True))
    ctx.grounding = "off"
    p = Plan.model_validate(
        {"goal": "g", "steps": [{"id": "a", "tool": "post", "args": {"account": "work", "text": "hi"}}]}
    )
    rec = PlanStore(state).create(p, check_plan(p, ctx, registry), created_by="t", approval_mode="out_of_band")
    payload = {"kind": "plan.execute", "plan_id": rec["id"], "plan_hash": rec["plan_hash"]}
    refused = d.dispatch(Invocation("plan.execute", payload))
    assert not refused.ok and not refused.retry and "needs approval" in refused.error
    PlanStore(state).approve(rec["id"], rec["plan_hash"], by="op", via="cli")
    done = d.dispatch(Invocation("plan.execute", payload))
    assert done.ok and done.result["status"] == "completed"
    assert not d.dispatch(Invocation("plan.execute", {"kind": "plan.execute"})).ok
