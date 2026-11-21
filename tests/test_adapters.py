"""Ports and adapters: secrets, object/session/job stores, settings resolution."""

import json
from datetime import datetime, timezone

import pytest

from skyctl import AccountConfig, BskyClient
from skyctl.adapters import (
    FileObjectStore,
    MemoryObjectStore,
    ObjectJobStore,
    ObjectSessionStore,
    object_store_for,
    parse_secret_payload,
    secret_store_for,
    session_store_for,
)
from skyctl.adapters.local import FileSessionStore, StaticSecretStore
from skyctl.models import ScheduledPost
from skyctl.ports import JobStore, ObjectStore, SecretStore, SessionStore
from skyctl.settings import Settings, detect_cloud, load_settings


# ----------------------------------------------------------------------
# Secrets
# ----------------------------------------------------------------------

def test_secret_payload_accepts_json_and_dotenv():
    as_json = parse_secret_payload(json.dumps({"BSKY_ACCOUNTS": "a", "BSKY_A_HANDLE": "a.test", "N": 1}))
    assert as_json == {"BSKY_ACCOUNTS": "a", "BSKY_A_HANDLE": "a.test", "N": "1"}
    as_env = parse_secret_payload("BSKY_ACCOUNTS=a\nBSKY_A_HANDLE=a.test\n# comment\nBSKY_A_PASSWORD='p w'\n")
    assert as_env == {"BSKY_ACCOUNTS": "a", "BSKY_A_HANDLE": "a.test", "BSKY_A_PASSWORD": "p w"}
    assert parse_secret_payload("  ") == {}


def test_secret_store_factory_selects_by_scheme(tmp_path):
    env = tmp_path / ".env"
    env.write_text("BSKY_ACCOUNTS=x\n")
    assert secret_store_for("env", env_file=str(env)).load() == {"BSKY_ACCOUNTS": "x"}
    assert secret_store_for("", env_file=str(tmp_path / "missing")).load() == {}

    aws = secret_store_for("aws-sm://skyctl/prod")
    assert type(aws).__name__ == "AwsSecretsManagerStore" and aws.secret_id == "skyctl/prod"
    kv = secret_store_for("azure-kv://my-vault/skyctl")
    assert (kv.vault, kv.secret_name) == ("my-vault", "skyctl")
    gcp = secret_store_for("gcp-sm://proj/skyctl@3")
    assert (gcp.project, gcp.secret_id, gcp.version) == ("proj", "skyctl", "3")
    with pytest.raises(ValueError):
        secret_store_for("ftp://nope")
    with pytest.raises(ValueError):
        secret_store_for("azure-kv://vault-only")


def test_cloud_secret_stores_parse_payload_with_injected_clients():
    from types import SimpleNamespace

    from skyctl.adapters.aws import AwsSecretsManagerStore
    from skyctl.adapters.azure import AzureKeyVaultStore
    from skyctl.adapters.gcp import GcpSecretManagerStore

    class FakeSM:
        def get_secret_value(self, SecretId):
            assert SecretId == "s"
            return {"SecretString": '{"BSKY_ACCOUNTS": "a"}'}

    class FakeKV:
        def get_secret(self, name):
            return SimpleNamespace(value="BSKY_ACCOUNTS=b")

    class FakeGSM:
        def access_secret_version(self, request):
            assert request["name"] == "projects/p/secrets/s/versions/latest"
            return SimpleNamespace(payload=SimpleNamespace(data=b'{"BSKY_ACCOUNTS": "c"}'))

    assert AwsSecretsManagerStore("s", client=FakeSM()).load() == {"BSKY_ACCOUNTS": "a"}
    assert AzureKeyVaultStore("v", "s", client=FakeKV()).load() == {"BSKY_ACCOUNTS": "b"}
    assert GcpSecretManagerStore("p", "s", client=FakeGSM()).load() == {"BSKY_ACCOUNTS": "c"}


# ----------------------------------------------------------------------
# Object stores (contract test over every local implementation)
# ----------------------------------------------------------------------

def _stores(tmp_path):
    yield "file", FileObjectStore(tmp_path / "state")
    yield "memory", MemoryObjectStore()
    pytest.importorskip("fsspec")
    from skyctl.adapters.fsspec_store import FsspecObjectStore

    yield "fsspec-memory", FsspecObjectStore("memory://skyctl-test")
    yield "fsspec-file", FsspecObjectStore(f"file://{tmp_path / 'fs'}")


def test_object_store_contract(tmp_path):
    for name, store in _stores(tmp_path):
        assert isinstance(store, ObjectStore), name
        assert not store.exists("a/b.txt"), name
        store.write("a/b.txt", b"hello")
        assert store.exists("a/b.txt") and store.read("a/b.txt") == b"hello", name
        store.write("a/c.txt", b"x")
        store.write("d.txt", b"y")
        assert store.list("a") == ["a/b.txt", "a/c.txt"], name
        assert set(store.list()) >= {"a/b.txt", "a/c.txt", "d.txt"}, name
        store.append_line("log.jsonl", '{"n": 1}')
        store.append_line("log.jsonl", '{"n": 2}\n')
        assert store.read("log.jsonl") == b'{"n": 1}\n{"n": 2}\n', name
        store.delete("a/b.txt")
        store.delete("a/b.txt")  # idempotent
        assert not store.exists("a/b.txt"), name
        assert store.list("nope") == [], name
        with pytest.raises(ValueError):
            store.read("../escape")


def test_object_store_factory(tmp_path):
    assert isinstance(object_store_for(f"file://{tmp_path}"), FileObjectStore)
    assert isinstance(object_store_for("file://./.skyctl"), FileObjectStore)
    assert isinstance(object_store_for("memory://x"), MemoryObjectStore)
    pytest.importorskip("fsspec")
    from skyctl.adapters.fsspec_store import FsspecObjectStore

    # Bucket URLs go through fsspec; "file" via fsspec proves the wrapper without a cloud SDK.
    store = object_store_for(f"file://{tmp_path / 'via-fsspec'}")
    assert isinstance(store, FileObjectStore)   # file:// is served by the stdlib adapter
    assert FsspecObjectStore("memory://z").scheme == "memory"
    with pytest.raises((ImportError, ValueError, Exception)):
        FsspecObjectStore("s3://bucket/prefix").exists("x")   # no s3fs / credentials here


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------

def test_session_store_roundtrip(tmp_path):
    for store in (session_store_for(MemoryObjectStore()), FileSessionStore(tmp_path / "s")):
        assert isinstance(store, SessionStore)
        assert store.get("personal") is None
        store.set("personal", "sess-1")
        assert store.get("personal") == "sess-1"
        store.clear("personal")
        store.clear("personal")
        assert store.get("personal") is None


def test_client_uses_session_store(fake_atproto):
    store = ObjectSessionStore(MemoryObjectStore())
    client = BskyClient(AccountConfig("personal", "me.test", "pw"), session_store=store)
    client.login()
    assert store.get("personal") == "fake-session-string"
    assert fake_atproto.instances[-1].login_calls[0]["password"] == "pw"

    # A second client reuses the cached session instead of the password.
    client2 = BskyClient(AccountConfig("personal", "me.test", "pw"), session_store=store)
    client2.login()
    assert fake_atproto.instances[-1].login_calls[0]["session_string"] == "fake-session-string"

    # Expired session: falls back to the password and keeps working.
    fake_atproto.fail_session_login = True
    client3 = BskyClient(AccountConfig("personal", "me.test", "pw"), session_store=store)
    client3.login()
    assert fake_atproto.instances[-1].login_calls[0]["password"] == "pw"
    client3.logout()
    assert store.get("personal") is None


def test_legacy_session_dir_still_works(fake_atproto, tmp_path):
    client = BskyClient(AccountConfig("work", "w.test", "pw"), session_dir=tmp_path)
    client.login()
    assert (tmp_path / "work.session").read_text() == "fake-session-string"


def test_client_reads_images_from_media_store(fake_atproto):
    media = MemoryObjectStore()
    media.write("media/kite.png", b"PNGDATA")
    client = BskyClient(AccountConfig("personal", "me.test", "pw"), media_store=media)
    result = client.post("photo", image_paths=["media/kite.png"], image_alts=["a kite"])
    assert result.success
    assert fake_atproto.instances[-1].uploads == [b"PNGDATA"]
    missing = client.post("photo", image_paths=["media/none.png"])
    assert not missing.success and "Image not found" in missing.error


# ----------------------------------------------------------------------
# Jobs
# ----------------------------------------------------------------------

def test_job_store_roundtrip_and_lease():
    store = ObjectJobStore(MemoryObjectStore())
    assert isinstance(store, JobStore)
    assert store.load() == {}
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sp = ScheduledPost(id="j1", account="a", text="t", run_at=now, cron="0 9 * * *", last_run=now, retry_at=now)
    store.save({"j1": sp})
    loaded = store.load()["j1"]
    assert (loaded.cron, loaded.last_run, loaded.retry_at, loaded.status) == ("0 9 * * *", now, now, "pending")

    assert store.lease("me", 60)
    assert not store.lease("other", 60)
    assert store.lease("me", 60)          # re-entrant for the same owner
    store.release("other")                # not the owner: no effect
    assert not store.lease("other", 60)
    store.release("me")
    assert store.lease("other", 60)


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------

def test_detect_cloud(monkeypatch):
    for var in ("SKYCTL_CLOUD", "ECS_CONTAINER_METADATA_URI_V4", "CONTAINER_APP_NAME", "K_SERVICE"):
        monkeypatch.delenv(var, raising=False)
    assert detect_cloud() == "local"
    monkeypatch.setenv("K_SERVICE", "skyctl")
    assert detect_cloud() == "gcp"
    monkeypatch.setenv("CONTAINER_APP_NAME", "skyctl")
    monkeypatch.delenv("K_SERVICE")
    assert detect_cloud() == "azure"
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4")
    monkeypatch.delenv("CONTAINER_APP_NAME")
    assert detect_cloud() == "aws"
    monkeypatch.setenv("SKYCTL_CLOUD", "local")
    assert detect_cloud() == "local"


def test_load_settings_merges_secrets_without_overriding_platform_env(monkeypatch):
    import os

    import skyctl.settings as settings_mod
    import skyctl.adapters as adapters_mod

    # load_settings() writes into the real environment; isolate it.
    monkeypatch.setattr(os, "environ", dict(os.environ))

    monkeypatch.setattr(settings_mod, "_loaded", None)
    monkeypatch.setenv("SKYCTL_SECRETS", "aws-sm://whatever")
    monkeypatch.setenv("SKYCTL_AGENT_MAX_WRITES", "2")
    monkeypatch.setenv("BSKY_ACCOUNTS", "platform")
    monkeypatch.delenv("SKYCTL_HTTP_TOKEN", raising=False)
    monkeypatch.setattr(
        adapters_mod,
        "secret_store_for",
        lambda url, env_file=".env": StaticSecretStore({"BSKY_ACCOUNTS": "secret", "SKYCTL_HTTP_TOKEN": "tok"}),
    )
    settings = load_settings(force=True)
    assert settings.secrets_are_remote
    assert settings.agent.max_writes == 2 and settings.agent.allow_writes is False
    assert settings.http_token == "tok"                 # came from the secret
    assert __import__("os").environ["BSKY_ACCOUNTS"] == "platform"   # platform value kept
    assert isinstance(StaticSecretStore({}), SecretStore)
    assert Settings.from_env().agent.accounts is None


def test_raw_value_secret_url_fragment():
    from types import SimpleNamespace

    from skyctl.adapters import RawValueSecretStore, secret_store_for
    from skyctl.adapters.aws import AwsSecretsManagerStore

    store = secret_store_for("aws-sm://skyctl/http-token#SKYCTL_HTTP_TOKEN")
    assert isinstance(store, RawValueSecretStore) and store.key == "SKYCTL_HTTP_TOKEN"
    store.inner._client = SimpleNamespace(get_secret_value=lambda SecretId: {"SecretString": "s3cr3t-token\n"})
    assert store.load() == {"SKYCTL_HTTP_TOKEN": "s3cr3t-token"}
    # Without the fragment, a raw token parses to nothing (it is not a map).
    plain = AwsSecretsManagerStore("x", client=SimpleNamespace(get_secret_value=lambda SecretId: {"SecretString": "s3cr3t-token"}))
    assert plain.load() == {}


def test_file_store_lists_under_a_relative_root(tmp_path, monkeypatch):
    """Regression: the default local state URL is relative (file://./.skyctl)."""
    monkeypatch.chdir(tmp_path)
    store = FileObjectStore(".skyctl")
    store.write("plans/a.json", b"{}")
    store.write("runs/r.jsonl", b"")
    assert store.list("plans/") == ["plans/a.json"]
    assert store.list() == ["plans/a.json", "runs/r.jsonl"]
