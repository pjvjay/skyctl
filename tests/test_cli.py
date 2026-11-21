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

import json

import pytest
from click.testing import CliRunner

from skyctl.cli import cli

ROOT = "at://did:plc:alice/app.bsky.feed.post/root"


@pytest.fixture
def run(tmp_path, monkeypatch):
    """Invoke the CLI in an empty directory with placeholder credentials."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BSKY_ACCOUNTS", "personal,work")
    monkeypatch.setenv("BSKY_PERSONAL_HANDLE", "me.test")
    monkeypatch.setenv("BSKY_PERSONAL_PASSWORD", "xxxx")
    monkeypatch.setenv("BSKY_WORK_HANDLE", "work.test")
    monkeypatch.setenv("BSKY_WORK_PASSWORD", "xxxx")
    runner = CliRunner()
    return lambda *args, **kw: runner.invoke(cli, list(args), **kw)


def all_logins(fake_atproto):
    return [c for inst in fake_atproto.instances for c in inst.login_calls]


def all_sent(fake_atproto):
    return [s for inst in fake_atproto.instances for s in inst.sent]


def test_help_lists_new_commands(run):
    out = run("--help").output
    for command in ("thread", "like", "delete", "search"):
        assert command in out
    post_help = run("post", "--help").output
    for flag in ("--dry-run", "--alt", "--quote", "--json"):
        assert flag in post_help


def test_post_dry_run_shows_facets_and_makes_no_api_call(run, fake_atproto):
    result = run("post", "personal", "hi @bsky.app https://example.com #test", "--dry-run")
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert "@bsky.app" in result.output
    assert "https://example.com" in result.output
    assert "#test" in result.output
    assert all_logins(fake_atproto) == []
    assert all_sent(fake_atproto) == []


def test_post_dry_run_json(run):
    result = run("post", "personal", "see https://example.com", "--dry-run", "--json")
    data = json.loads(result.output)
    assert data["dry_run"] is True
    assert data["graphemes"] == 23
    assert data["facets"] == [{"type": "link", "text": "https://example.com", "value": "https://example.com"}]


def test_over_long_post_is_rejected_with_clear_error(run, fake_atproto):
    result = run("post", "personal", "a" * 301)
    assert result.exit_code == 1
    assert "301" in result.output and "300" in result.output
    assert all_logins(fake_atproto) == []


def test_thread_dry_run_splits_long_text(run, fake_atproto):
    text = " ".join(["word"] * 61)  # 304 graphemes
    result = run("thread", "personal", text, "--dry-run", "--json")
    data = json.loads(result.output)
    assert len(data["posts"]) == 2
    assert all(p["graphemes"] <= 300 for p in data["posts"])
    assert all_sent(fake_atproto) == []


def test_post_json_output_and_session_cache(run, fake_atproto, tmp_path):
    result = run("post", "personal", "hello", "--json")
    data = json.loads(result.output)
    assert data["success"] is True
    assert data["url"].startswith("https://bsky.app/profile/")
    assert (tmp_path / ".skyctl_sessions" / "personal.session").exists()

    run("post", "personal", "again", "--json")
    assert all_logins(fake_atproto)[-1]["session_string"] == "fake-session-string"


def test_unknown_account_is_a_clean_error(run):
    result = run("post", "nobody", "hello")
    assert result.exit_code == 1
    assert "not found" in result.output


def test_search_json(run):
    result = run("search", "personal", "kites", "--json")
    assert json.loads(result.output)[0]["text"] == "result for kites"


def test_delete_asks_for_confirmation(run, fake_atproto):
    declined = run("delete", "personal", ROOT, input="n\n")
    assert declined.exit_code == 1
    assert all(inst.deleted == [] for inst in fake_atproto.instances)

    confirmed = run("delete", "personal", ROOT, "--yes")
    assert confirmed.exit_code == 0
    assert any(inst.deleted == [ROOT] for inst in fake_atproto.instances)


def test_recurring_schedule_from_cli_shows_in_jobs(run):
    created = run("schedule", "work", "Daily update", "--cron", "0 9 * * mon-fri", "--json")
    job_id = json.loads(created.output)["id"]

    jobs = json.loads(run("jobs", "--json").output)
    assert [j["id"] for j in jobs] == [job_id]
    assert jobs[0]["status"] == "recurring"

    assert run("cancel", job_id).exit_code == 0
    assert json.loads(run("jobs", "--json").output) == []


def test_schedule_requires_exactly_one_of_run_at_and_cron(run):
    assert run("schedule", "work", "x").exit_code == 1
    assert run("schedule", "work", "x", "2030-01-01T09:00:00", "--cron", "0 9 * * *").exit_code == 1


def test_one_time_schedule_still_works(run):
    result = run("schedule", "personal", "Morning!", "2030-03-01T09:00:00")
    assert result.exit_code == 0
    jobs = json.loads(run("jobs", "--json").output)
    assert jobs[0]["status"] == "pending"
    assert jobs[0]["next_run"].startswith("2030-03-01T09:00:00")
