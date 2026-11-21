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

"""Agents, tested against an in-memory PDS and scripted models."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import skyctl.client as client_mod
from conftest import FakeAtProto, require
from skyctl import AccountConfig, BskyManager
from skyctl.agents import (
    AgentConfigError,
    AgentGroup,
    PublicNetworkError,
    fit_to_post,
    is_public_network,
    load_agents_config,
    parse_items,
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

PDS = "http://localhost:2583"

CONFIG = """
[groups.newsroom]
members = ["olive", "sam"]
model = "script:default"

[agents.olive]
name = "Olive"
persona = "An urban planner."
viewpoint = "People before cars."
interests = ["urban design", "air quality"]
post_style = "Upbeat, short sentences."

[agents.olive.personality]
mbti = "enfp"
traits = ["enthusiastic"]
values = ["community"]
tone = "warm"

[agents.sam]
name = "Sam"
persona = "A van driver."
model = "script:other"
"""

ITEM = "<item><url>https://news.test/a</url><post>{}</post></item>"


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
            uri=f"at://{self.me.did}/app.bsky.feed.post/{n:04d}",
            cid=f"cid{n}",
            value=SimpleNamespace(text=text, created_at=f"2026-01-01T00:00:{n:02d}Z", reply=reply_to),
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

    def __init__(self, *answers, supports_web_search=True):
        self.answers = list(answers)
        self.calls = []
        self.supports_web_search = supports_web_search

    def complete(self, system, prompt, web_search=False):
        self.calls.append({"system": system, "prompt": prompt, "web_search": web_search})
        answer = self.answers.pop(0) if self.answers else ITEM.format("Default take.")
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def pds(monkeypatch):
    FakePDS.repos = {}
    FakePDS.counter = 0
    monkeypatch.setattr(client_mod, "AtProtoClient", FakePDS)
    # Link cards would otherwise fetch the source page.
    monkeypatch.setattr(
        client_mod,
        "fetch_link_metadata",
        lambda url: {"title": url, "description": "", "thumb": None},
    )


def make_manager(pds_url=PDS):
    return BskyManager(
        {alias: AccountConfig(alias, f"{alias}.test", "pw", pds_url=pds_url) for alias in ("olive", "sam")}
    )


@pytest.fixture
def group(pds, tmp_path):
    config_file = tmp_path / "agents.toml"
    config_file.write_text(CONFIG)
    scripts = {"olive": Script(), "sam": Script()}
    made = []

    def factory(spec, **options):
        made.append(spec)
        return scripts["sam" if spec == "script:other" else "olive"]

    g = AgentGroup.from_config(
        "newsroom",
        config_file,
        manager=make_manager(),
        llm_factory=factory,
        transcript_dir=tmp_path / "log",
    )
    g.scripts, g.made = scripts, made
    return g


# -- guard rails -------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "public"),
    [
        (None, True),
        ("", True),
        ("https://bsky.social", True),
        ("https://morel.us-east.host.bsky.network", True),
        ("bsky.social", True),
        ("http://localhost:2583", False),
        ("https://pds.lab.example", False),
        ("https://notbsky.social.example", False),
    ],
)
def test_is_public_network(url, public):
    assert is_public_network(url) is public


def test_group_refuses_the_public_network(pds, tmp_path):
    config_file = tmp_path / "agents.toml"
    config_file.write_text(CONFIG)
    for url in (None, "https://bsky.social"):
        with pytest.raises(PublicNetworkError):
            AgentGroup.from_config("newsroom", config_file, manager=make_manager(url))


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


def test_load_config_with_personality(tmp_path):
    f = tmp_path / "agents.toml"
    f.write_text(CONFIG)
    g = load_agents_config(f)["newsroom"]
    olive, sam = g.members
    assert olive.account == "olive" and sam.model == "script:other"
    assert olive.personality.mbti == "ENFP"
    assert olive.personality.traits == ["enthusiastic"]
    assert olive.post_style == "Upbeat, short sentences."
    assert sam.personality.mbti == "" and sam.personality.describe() == ""
    assert g.web_search is True and g.link_cards is True


@pytest.mark.parametrize(
    "bad",
    [
        '[groups.g]\nmembers = []\n[agents.a]\npersona = "x"',
        '[groups.g]\nmembers = ["a", "ghost"]\n[agents.a]\npersona = "x"',
        '[groups.g]\nmembers = ["a"]\n[agents.a]\nname = "A"',
        '[agents.a]\npersona = "x"',
        '[groups.g]\nmembers = ["a"]\n[agents.a]\npersona = "x"\n[agents.a.personality]\nmbti = "ABCD"',
        '[groups.g]\nmembers = ["a"]\n[agents.a]\npersona = "x"\npersonality = "INTJ"',
    ],
)
def test_bad_config(tmp_path, bad):
    f = tmp_path / "agents.toml"
    f.write_text(bad)
    with pytest.raises(AgentConfigError):
        load_agents_config(f)


def test_account_must_exist(pds, tmp_path):
    f = tmp_path / "agents.toml"
    f.write_text(CONFIG.replace('name = "Sam"', 'name = "Sam"\naccount = "nobody"'))
    with pytest.raises(AgentConfigError, match="nobody"):
        AgentGroup.from_config("newsroom", f, manager=make_manager())


# -- text helpers ------------------------------------------------------


def test_parse_items():
    raw = (
        "I searched.\n<item><url> https://a.test/x </url><post>One</post></item>\n"
        "<item><url>not a url</url><post>Two</post></item><item><post></post></item>"
    )
    assert parse_items(raw) == [("https://a.test/x", "One"), (None, "Two")]
    assert parse_items("<post>Bare</post>") == [(None, "Bare")]
    assert parse_items("  plain text  ") == [(None, "plain text")]
    assert parse_items("") == []


def test_fit_to_post():
    assert fit_to_post("short") == "short"
    long = fit_to_post("word " * 200)
    assert grapheme_len(long) <= 300 and long.endswith("…")
    assert grapheme_len(fit_to_post("x" * 500)) == 300


# -- research ----------------------------------------------------------


def test_research_searches_and_posts_with_link_card(group):
    group.scripts["olive"].answers = [ITEM.format("Paris cut traffic 40%. More of this!")]
    [action] = group.research(agent="olive")
    assert action.status == "posted" and action.source_url == "https://news.test/a"

    [post] = group.feed("olive")
    assert post["text"] == "Paris cut traffic 40%. More of this!"
    sent = FakePDS.instances[0].sent[0]
    assert sent["embed"].external.uri == "https://news.test/a"
    assert sent["reply_to"] is None

    call = group.scripts["olive"].calls[0]
    assert call["web_search"] is True
    assert "urban design, air quality" in call["prompt"]
    logged = [json.loads(line) for line in group.transcript_path.read_text().splitlines()]
    assert logged[0]["status"] == "posted" and logged[0]["agent"] == "olive"


def test_system_prompt_carries_the_whole_character(group):
    group.research(agent="olive", dry_run=True)
    system = group.scripts["olive"].calls[0]["system"]
    for expected in (
        "You are Olive",
        "An urban planner.",
        "Myers-Briggs type ENFP",
        "- E: energised",
        "- P: open-ended",
        "Traits: enthusiastic",
        "Values: community",
        "Tone: warm",
        "People before cars.",
        "urban design, air quality",
        "Upbeat, short sentences.",
    ):
        assert expected in system


def test_agents_work_alone(group):
    group.scripts["olive"].answers = [ITEM.format("Olive's take.")]
    group.scripts["sam"].answers = [ITEM.format("Sam's take.")]
    actions = group.research()
    assert [(a.agent, a.status) for a in actions] == [("olive", "posted"), ("sam", "posted")]
    assert group.made == ["script:default", "script:other"]
    # Sam never sees Olive's post, and nobody replies to anybody.
    sam_call = group.scripts["sam"].calls[0]
    assert "Olive" not in sam_call["prompt"] + sam_call["system"]
    assert all(s["reply_to"] is None for inst in FakePDS.instances for s in inst.sent)
    assert [p["text"] for p in group.feed("sam")] == ["Sam's take."]


def test_agent_is_told_what_it_already_posted(group):
    group.scripts["olive"].answers = [ITEM.format("First take.")]
    group.research(agent="olive")
    group.research(agent="olive", dry_run=True)
    assert "- First take." in group.scripts["olive"].calls[1]["prompt"]
    assert "First take." not in group.scripts["olive"].calls[0]["prompt"]


def test_max_posts_and_topic(group):
    group.scripts["olive"].answers = ["".join(ITEM.format(n) for n in ("one", "two", "three"))]
    actions = group.research(agent="olive", topic="Tram lines", max_posts=2)
    assert [a.text for a in actions] == ["one", "two"]
    assert all(a.topic == "Tram lines" for a in actions)
    prompt = group.scripts["olive"].calls[0]["prompt"]
    assert "Tram lines" in prompt and "up to 2" in prompt
    assert len(group.feed("olive")) == 2


def test_dry_run_posts_nothing_and_trims(group):
    group.scripts["olive"].answers = [ITEM.format("blah " * 100)]
    [action] = group.research(agent="olive", dry_run=True)
    assert action.status == "dry-run" and grapheme_len(action.text) <= 300
    assert group.feed("olive") == []


def test_link_cards_can_be_turned_off(group):
    group.config.link_cards = False
    group.research(agent="olive")
    assert FakePDS.instances[0].sent[0]["embed"] is None


def test_model_without_search_needs_sources(group, monkeypatch):
    group.scripts["olive"].supports_web_search = False
    [action] = group.research(agent="olive")
    assert action.status == "failed" and "cannot search the web" in action.error
    assert group.scripts["olive"].calls == []

    monkeypatch.setattr("skyctl.agents.fetch_source_text", lambda url: "The study found X.")
    [action] = group.research(agent="olive", urls=["https://e.test/s"])
    assert action.status == "posted"
    call = group.scripts["olive"].calls[0]
    assert call["web_search"] is False and "The study found X." in call["prompt"]


def test_web_search_off_in_config(group):
    group.config.web_search = False
    [action] = group.research(agent="olive")
    assert action.status == "failed" and "web_search is off" in action.error


def test_failures_are_reported(group):
    group.scripts["olive"].answers = [LLMError("boom"), "   "]
    assert group.research(agent="olive")[0].error == "boom"
    assert group.research(agent="olive")[0].error == "The model returned no posts."
    with pytest.raises(AgentConfigError):
        group.research(agent="ghost")


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
    register_provider("script", lambda model, **o: Script(model))
    assert get_provider("script:abc").complete("s", "p") == "abc"


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
    require("anthropic")
    fake = FakeAnthropic(
        SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="thinking"), _text("Hello "), _text("world")],
        )
    )
    out = AnthropicProvider("claude-opus-5-5", effort="low", client=fake).complete("sys", "hi")
    assert out == "Hello world"
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["system"] == "sys"
    assert call["messages"] == [{"role": "user", "content": "hi"}]
    assert call["output_config"] == {"effort": "low"}
    # Sent in the JSON body (extra_body) so SDK versions without a typed `fallbacks` argument work too.
    assert call["extra_body"] == {"fallbacks": "default"} and "tools" not in call


def test_anthropic_provider_no_fallback_param_on_other_models():
    require("anthropic")
    fake = FakeAnthropic(SimpleNamespace(stop_reason="end_turn", content=[_text("ok")]))
    AnthropicProvider("claude-haiku-5-5", client=fake).complete("s", "p")
    assert "extra_body" not in fake.calls[0] and "betas" not in fake.calls[0]


def test_anthropic_provider_web_search_resumes_paused_turn():
    require("anthropic")
    paused = SimpleNamespace(stop_reason="pause_turn", content=["partial"])
    done = SimpleNamespace(stop_reason="end_turn", content=[_text("found it")])
    fake = FakeAnthropic(paused, done)
    out = AnthropicProvider("claude-opus-5-5", client=fake).complete("s", "p", web_search=True)
    assert out == "found it"
    assert fake.calls[0]["tools"][0]["name"] == "web_search"
    assert fake.calls[1]["messages"][-1] == {"role": "assistant", "content": ["partial"]}


def test_anthropic_provider_refusal():
    require("anthropic")
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

    monkeypatch.setattr(httpx, "post", lambda *a, **k: SimpleNamespace(status_code=500, text="down"))
    with pytest.raises(LLMError, match="500"):
        OpenAICompatProvider("m", "http://x/v1").complete("s", "p")


# -- CLI ---------------------------------------------------------------


@pytest.fixture
def cli_env(pds, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents.toml").write_text(CONFIG)
    monkeypatch.setenv("BSKY_ACCOUNTS", "olive,sam")
    for alias in ("OLIVE", "SAM"):
        monkeypatch.setenv(f"BSKY_{alias}_HANDLE", f"{alias.lower()}.test")
        monkeypatch.setenv(f"BSKY_{alias}_PASSWORD", "pw")
    monkeypatch.setenv("BSKY_PDS_URL", PDS)
    register_provider("script", lambda model, **o: Script(ITEM.format("From the CLI.")))
    return CliRunner()


def test_cli_list_research_and_feed(cli_env):
    result = cli_env.invoke(cli, ["agents", "list", "--json"])
    assert result.exit_code == 0, result.output
    members = json.loads(result.output)[0]["members"]
    assert members[0]["mbti"] == "ENFP" and members[1]["model"] == "script:other"
    assert "ENFP" in cli_env.invoke(cli, ["agents", "list"]).output

    result = cli_env.invoke(cli, ["agents", "research", "newsroom", "--dry-run"])
    assert result.exit_code == 0 and "From the CLI." in result.output

    result = cli_env.invoke(cli, ["agents", "research", "newsroom", "--agent", "olive", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)[0]["status"] == "posted"

    result = cli_env.invoke(cli, ["agents", "feed", "newsroom"])
    assert "From the CLI." in result.output and "No posts yet." in result.output
    result = cli_env.invoke(cli, ["agents", "feed", "newsroom", "--agent", "olive", "--json"])
    assert json.loads(result.output)["olive"][0]["text"] == "From the CLI."


def test_cli_refuses_public_network(cli_env, monkeypatch):
    monkeypatch.delenv("BSKY_PDS_URL")
    result = cli_env.invoke(cli, ["agents", "research", "newsroom", "--json"])
    assert result.exit_code == 1
    assert "private PDS" in json.loads(result.output)["error"]
