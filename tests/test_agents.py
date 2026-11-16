"""Agent groups, tested against an in-memory PDS and scripted models."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import skyctl.client as client_mod
from skyctl import BskyManager
from skyctl.agents import (
    AgentConfigError,
    AgentGroup,
    PublicNetworkError,
    extract_tagged,
    fit_to_post,
    is_public_network,
    load_agents_config,
)
from skyctl.cli import cli
from skyctl.config import load_accounts
from skyctl.llm import (
    AnthropicProvider,
    LLMError,
    LLMRefusal,
    OpenAICompatProvider,
    get_provider,
    parse_model_spec,
    register_provider,
)
from skyctl.richtext import grapheme_len

from conftest import FakeAtProto

PDS = "http://localhost:2583"

CONFIG = """
[groups.debate]
topic = "Cars in cities"
members = ["optimist", "skeptic"]
model = "script:default"
max_thread_depth = 3

[agents.optimist]
name = "Olive"
persona = "An urban planner."
viewpoint = "Ban cars."

[agents.skeptic]
name = "Sam"
persona = "A van driver."
model = "script:other"
"""


class FakePDS(FakeAtProto):
    """A FakeAtProto whose accounts share one set of repos, like a real PDS."""

    repos = {}
    counter = 0

    def login(self, login=None, password=None, session_string=None):
        self.me = SimpleNamespace(did=f"did:plc:{login.split('.')[0]}", handle=login)
        FakePDS.repos.setdefault(self.me.did, [])
        return super().login(login, password, session_string)

    def send_post(self, text, reply_to=None, embed=None, langs=None, facets=None):
        super().send_post(text, reply_to, embed, langs, facets)
        FakePDS.counter += 1
        n = FakePDS.counter
        record = SimpleNamespace(
            uri=f"{'at://' + self.me.did}/app.bsky.feed.post/{n:04d}",
            cid=f"cid{n}",
            value=SimpleNamespace(
                text=text, created_at=f"2026-01-01T00:00:{n:02d}Z", reply=reply_to
            ),
        )
        FakePDS.repos[self.me.did].insert(0, record)
        return SimpleNamespace(uri=record.uri, cid=record.cid)

    @property
    def com(self):
        def list_records(params):
            return SimpleNamespace(records=FakePDS.repos[params["repo"]][: params["limit"]])

        repo = SimpleNamespace(list_records=list_records)
        return SimpleNamespace(atproto=SimpleNamespace(repo=repo))


class Script:
    """A model that answers from a queue and remembers what it was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, system, prompt, web_search=False):
        self.calls.append({"system": system, "prompt": prompt, "web_search": web_search})
        answer = self.answers.pop(0) if self.answers else "<reply>SKIP</reply>"
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def pds(monkeypatch):
    FakePDS.repos = {}
    FakePDS.counter = 0
    monkeypatch.setattr(client_mod, "AtProtoClient", FakePDS)


def make_manager(pds_url=PDS):
    from skyctl import AccountConfig

    return BskyManager({
        alias: AccountConfig(alias, f"{alias}.test", "pw", pds_url=pds_url)
        for alias in ("optimist", "skeptic")
    })


@pytest.fixture
def group(pds, tmp_path):
    config_file = tmp_path / "agents.toml"
    config_file.write_text(CONFIG)
    scripts = {"optimist": Script(), "skeptic": Script()}
    made = []

    def factory(spec, **options):
        made.append(spec)
        return scripts["skeptic" if spec == "script:other" else "optimist"]

    g = AgentGroup.from_config(
        "debate", config_file, manager=make_manager(),
        llm_factory=factory, transcript_dir=tmp_path / "log",
    )
    g.scripts, g.made = scripts, made
    return g


# -- guard rails -------------------------------------------------------

@pytest.mark.parametrize("url,public", [
    (None, True),
    ("", True),
    ("https://bsky.social", True),
    ("https://morel.us-east.host.bsky.network", True),
    ("bsky.social", True),
    ("http://localhost:2583", False),
    ("https://pds.lab.example", False),
    ("https://notbsky.social.example", False),
])
def test_is_public_network(url, public):
    assert is_public_network(url) is public


def test_group_refuses_the_public_network(pds, tmp_path):
    config_file = tmp_path / "agents.toml"
    config_file.write_text(CONFIG)
    for url in (None, "https://bsky.social"):
        with pytest.raises(PublicNetworkError):
            AgentGroup.from_config("debate", config_file, manager=make_manager(url))


def test_pds_url_is_loaded_and_used(pds, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("")
    monkeypatch.setenv("BSKY_ACCOUNTS", "a,b")
    for alias in ("A", "B"):
        monkeypatch.setenv(f"BSKY_{alias}_HANDLE", f"{alias.lower()}.test")
        monkeypatch.setenv(f"BSKY_{alias}_PASSWORD", "pw")
    monkeypatch.setenv("BSKY_PDS_URL", PDS)
    monkeypatch.setenv("BSKY_B_PDS_URL", "http://other:3000")
    accounts = load_accounts(str(env))
    assert accounts["a"].pds_url == PDS
    assert accounts["b"].pds_url == "http://other:3000"
    assert BskyManager(accounts).client("a")._client.base_url == PDS


# -- config ------------------------------------------------------------

def test_load_config(tmp_path):
    f = tmp_path / "agents.toml"
    f.write_text(CONFIG)
    g = load_agents_config(f)["debate"]
    assert [p.alias for p in g.members] == ["optimist", "skeptic"]
    assert g.members[0].account == "optimist"
    assert g.members[1].model == "script:other"
    assert g.max_thread_depth == 3


@pytest.mark.parametrize("bad", [
    '[groups.g]\nmembers = ["a"]\n[agents.a]\npersona = "x"',
    '[groups.g]\nmembers = ["a", "ghost"]\n[agents.a]\npersona = "x"',
    '[groups.g]\nmembers = ["a", "b"]\n[agents.a]\npersona = "x"\n[agents.b]\nname = "B"',
    '[agents.a]\npersona = "x"',
])
def test_bad_config(tmp_path, bad):
    f = tmp_path / "agents.toml"
    f.write_text(bad)
    with pytest.raises(AgentConfigError):
        load_agents_config(f)


def test_account_must_exist(pds, tmp_path):
    f = tmp_path / "agents.toml"
    f.write_text(CONFIG.replace('name = "Sam"', 'name = "Sam"\naccount = "nobody"'))
    with pytest.raises(AgentConfigError, match="nobody"):
        AgentGroup.from_config("debate", f, manager=make_manager())


# -- text helpers ------------------------------------------------------

def test_extract_tagged():
    assert extract_tagged("Sure!\n<reply> hi there </reply> bye", "reply") == "hi there"
    assert extract_tagged("  no tags  ", "reply") == "no tags"


def test_fit_to_post():
    assert fit_to_post("short") == "short"
    long = fit_to_post("word " * 200)
    assert grapheme_len(long) <= 300 and long.endswith("…")
    assert grapheme_len(fit_to_post("x" * 500)) == 300


# -- research ----------------------------------------------------------

def test_research_posts_a_thread_and_logs_it(group):
    group.scripts["optimist"].answers = [
        "Thinking...\n<post>Streets are for people.</post>\n<post>Evidence: Pontevedra.</post>"
    ]
    [action] = group.research(agent="optimist")
    assert action.status == "posted" and len(action.uris) == 2
    assert action.topic == "Cars in cities"

    feed = group.explore()
    assert [p["text"] for p in feed] == ["Streets are for people.", "Evidence: Pontevedra."]
    assert [p["depth"] for p in feed] == [0, 1]
    assert feed[1]["reply_root"]["uri"] == feed[0]["uri"]

    call = group.scripts["optimist"].calls[0]
    assert "You are Olive" in call["system"] and "Ban cars." in call["system"]
    assert "Cars in cities" in call["prompt"]
    logged = [json.loads(line) for line in group.transcript_path.read_text().splitlines()]
    assert logged[0]["action"] == "research" and logged[0]["status"] == "posted"


def test_research_uses_each_agents_model(group):
    group.research(dry_run=True)
    assert group.made == ["script:default", "script:other"]


def test_research_dry_run_posts_nothing(group):
    group.scripts["optimist"].answers = ["<post>Hello</post>"]
    [action] = group.research(agent="optimist", dry_run=True)
    assert action.status == "dry-run" and action.text == "Hello"
    assert group.explore() == []


def test_research_includes_sources_and_caps_posts(group, monkeypatch):
    monkeypatch.setattr("skyctl.agents.fetch_source_text", lambda url: "The study found X.")
    group.scripts["optimist"].answers = ["<post>1</post><post>2</post><post>3</post>"]
    [action] = group.research(
        agent="optimist", topic="Custom", urls=["https://e.test/s"], max_posts=2
    )
    prompt = group.scripts["optimist"].calls[0]["prompt"]
    assert "The study found X." in prompt and "Custom" in prompt
    assert len(action.uris) == 2


def test_research_model_failure_is_reported(group):
    group.scripts["optimist"].answers = [LLMError("boom")]
    [action] = group.research(agent="optimist")
    assert action.status == "failed" and action.error == "boom"


# -- comment -----------------------------------------------------------

def test_agents_reply_to_each_other_with_correct_threading(group):
    group.scripts["optimist"].answers = ["<post>Ban cars downtown.</post>"]
    group.research(agent="optimist")
    root = group.explore()[0]

    group.scripts["skeptic"].answers = ["<reply>And my deliveries?</reply>"]
    actions = group.comment()
    # Optimist has nothing to answer when its turn comes; skeptic replies.
    assert [(a.agent, a.status) for a in actions] == [("skeptic", "posted")]
    assert actions[0].target_author == "optimist"
    prompt = group.scripts["skeptic"].calls[0]["prompt"]
    assert "Olive: Ban cars downtown." in prompt

    group.scripts["optimist"].answers = ["<reply>Cargo bikes and delivery windows.</reply>"]
    actions = group.comment()
    # Skeptic sees the new reply in the same run and (by script) passes on it.
    assert [(a.agent, a.status) for a in actions] == [
        ("optimist", "posted"), ("skeptic", "skipped"),
    ]
    prompt = group.scripts["optimist"].calls[-1]["prompt"]
    assert "Olive: Ban cars downtown." in group.scripts["skeptic"].calls[-1]["prompt"]
    assert "You: Ban cars downtown." in prompt and "Sam: And my deliveries?" in prompt

    feed = group.explore()
    assert [p["depth"] for p in feed] == [0, 1, 2]
    # Nested reply keeps the original root and points at its direct parent.
    assert feed[2]["reply_root"]["uri"] == root["uri"]
    assert feed[2]["reply_parent"]["uri"] == feed[1]["uri"]


def test_no_double_replies_and_skip(group):
    group.scripts["optimist"].answers = ["<post>Ban cars.</post>"]
    group.research(agent="optimist")
    group.scripts["skeptic"].answers = ["<reply>SKIP</reply>", "<reply>No.</reply>"]
    assert group.comment(agent="skeptic")[0].status == "skipped"
    assert group.comment(agent="skeptic")[0].status == "posted"
    assert group.comment(agent="skeptic") == []  # already answered


def test_thread_depth_limit_stops_the_back_and_forth(group):
    group.scripts["optimist"].answers = ["<post>Ban cars.</post>"] + ["<reply>yes</reply>"] * 9
    group.scripts["skeptic"].answers = ["<reply>no</reply>"] * 9
    group.research(agent="optimist")
    for _ in range(6):
        group.comment()
    assert max(p["depth"] for p in group.explore()) == 3  # max_thread_depth


def test_comment_dry_run_and_long_reply(group):
    group.scripts["optimist"].answers = ["<post>Ban cars.</post>"]
    group.research(agent="optimist")
    group.scripts["skeptic"].answers = ["<reply>" + "blah " * 100 + "</reply>"]
    [action] = group.comment(agent="skeptic", dry_run=True)
    assert action.status == "dry-run" and grapheme_len(action.text) <= 300
    assert len(group.explore()) == 1


def test_unknown_agent(group):
    with pytest.raises(AgentConfigError):
        group.comment(agent="ghost")


def test_run_round_researches_then_comments(group):
    group.scripts["optimist"].answers = ["<post>Ban cars.</post>", "<reply>Why not?</reply>"]
    group.scripts["skeptic"].answers = ["<post>Vans matter.</post>", "<reply>Costs.</reply>"]
    actions = group.run_round(max_comments=1)
    assert [(a.agent, a.action) for a in actions] == [
        ("optimist", "research"), ("skeptic", "research"),
        ("optimist", "comment"), ("skeptic", "comment"),
    ]
    assert all(a.status == "posted" for a in actions)
    # Skeptic answers the reply addressed to its own post first.
    assert actions[3].target_uri == actions[2].uris[0]
    # A second round does no new research: everyone has a top-level post.
    assert all(a.action == "comment" for a in group.run_round())


# -- model backends ----------------------------------------------------

def test_parse_model_spec(monkeypatch):
    monkeypatch.delenv("SKYCTL_AGENT_MODEL", raising=False)
    assert parse_model_spec(None) == ("anthropic", "claude-opus-5-5")
    assert parse_model_spec("claude-haiku-5-5") == ("anthropic", "claude-haiku-5-5")
    assert parse_model_spec("ollama:llama3.1:8b") == ("ollama", "llama3.1:8b")
    assert parse_model_spec("openrouter:meta/llama") == ("openrouter", "meta/llama")
    monkeypatch.setenv("SKYCTL_AGENT_MODEL", "ollama:qwen")
    assert parse_model_spec("") == ("ollama", "qwen")


def test_register_provider():
    register_provider("script", lambda model, **o: Script(f"<reply>{model}</reply>"))
    assert get_provider("script:abc").complete("s", "p") == "<reply>abc</reply>"


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        get_provider("openai:some-model")


def _text(text):
    return SimpleNamespace(type="text", text=text)


class FakeAnthropic:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        create = lambda **kw: (self.calls.append(kw), self.responses.pop(0))[1]
        self.messages = SimpleNamespace(create=create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=create))


def test_anthropic_provider_request_shape():
    fake = FakeAnthropic(SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="thinking"), _text("Hello "), _text("world")],
    ))
    out = AnthropicProvider("claude-opus-5-5", effort="low", client=fake).complete("sys", "hi")
    assert out == "Hello world"
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "hi"}]
    assert call["output_config"] == {"effort": "low"}
    assert call["fallbacks"] == "default" and "tools" not in call


def test_anthropic_provider_no_fallback_param_on_other_models():
    fake = FakeAnthropic(SimpleNamespace(stop_reason="end_turn", content=[_text("ok")]))
    AnthropicProvider("claude-haiku-5-5", client=fake).complete("s", "p")
    assert "fallbacks" not in fake.calls[0] and "betas" not in fake.calls[0]


def test_anthropic_provider_web_search_resumes_paused_turn():
    paused = SimpleNamespace(stop_reason="pause_turn", content=["partial"])
    done = SimpleNamespace(stop_reason="end_turn", content=[_text("found it")])
    fake = FakeAnthropic(paused, done)
    out = AnthropicProvider("claude-opus-5-5", client=fake).complete("s", "p", web_search=True)
    assert out == "found it"
    assert fake.calls[0]["tools"][0]["name"] == "web_search"
    assert fake.calls[1]["messages"][-1] == {"role": "assistant", "content": ["partial"]}


def test_anthropic_provider_refusal():
    fake = FakeAnthropic(SimpleNamespace(stop_reason="refusal", content=[]))
    with pytest.raises(LLMRefusal):
        AnthropicProvider("claude-opus-5-5", client=fake).complete("s", "p")


def test_openai_compat_provider(monkeypatch):
    import httpx

    seen = {}

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"choices": [{"message": {"content": " hi "}}]},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    out = OpenAICompatProvider("m", "http://x/v1/", api_key="k").complete("sys", "p")
    assert out == "hi"
    assert seen["url"] == "http://x/v1/chat/completions"
    assert seen["headers"] == {"Authorization": "Bearer k"}
    assert seen["json"]["messages"][0] == {"role": "system", "content": "sys"}

    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: SimpleNamespace(status_code=500, text="down")
    )
    with pytest.raises(LLMError, match="500"):
        OpenAICompatProvider("m", "http://x/v1").complete("s", "p")


# -- CLI ---------------------------------------------------------------

@pytest.fixture
def cli_env(pds, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents.toml").write_text(CONFIG)
    monkeypatch.setenv("BSKY_ACCOUNTS", "optimist,skeptic")
    for alias in ("OPTIMIST", "SKEPTIC"):
        monkeypatch.setenv(f"BSKY_{alias}_HANDLE", f"{alias.lower()}.test")
        monkeypatch.setenv(f"BSKY_{alias}_PASSWORD", "pw")
    monkeypatch.setenv("BSKY_PDS_URL", PDS)
    register_provider("script", lambda model, **o: Script("<post>From the CLI.</post>"))
    return CliRunner()


def test_cli_list_and_research_and_explore(cli_env):
    result = cli_env.invoke(cli, ["agents", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["members"][1]["model"] == "script:other"

    result = cli_env.invoke(cli, ["agents", "research", "debate", "--agent", "optimist", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["status"] == "posted"

    result = cli_env.invoke(cli, ["agents", "explore", "debate"])
    assert "From the CLI." in result.output

    result = cli_env.invoke(cli, ["agents", "comment", "debate", "--dry-run"])
    assert result.exit_code == 0, result.output


def test_cli_refuses_public_network(cli_env, monkeypatch):
    monkeypatch.delenv("BSKY_PDS_URL")
    result = cli_env.invoke(cli, ["agents", "comment", "debate", "--json"])
    assert result.exit_code == 1
    assert "private PDS" in json.loads(result.output)["error"]
