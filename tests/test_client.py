from types import SimpleNamespace

from atproto import models as at_models

import skyctl
import skyctl.client as client_mod
from skyctl import AccountConfig, BskyClient
from conftest import make_post_view

ROOT = "at://did:plc:alice/app.bsky.feed.post/root"
MIDDLE = "at://did:plc:bob/app.bsky.feed.post/middle"


def test_public_classes_are_exported():
    assert skyctl.BskyManager and skyctl.BskyScheduler and skyctl.BskyClient


# -- posting quality ---------------------------------------------------

def test_post_sends_facets_for_link_mention_and_tag(client):
    result = client.post("hi @bsky.app https://example.com #test")
    assert result.success
    facets = client._client.sent[0]["facets"]
    features = [f.features[0] for f in facets]
    assert isinstance(features[0], at_models.AppBskyRichtextFacet.Mention)
    assert features[0].did == "did:plc:bsky"
    assert features[1].uri == "https://example.com"
    assert features[2].tag == "test"


def test_unresolvable_mention_is_left_as_plain_text(client):
    client.post("hello @unknown.example.com")
    assert client._client.sent[0]["facets"] is None


def test_plain_text_post_has_no_facets(client):
    client.post("nothing special here")
    assert client._client.sent[0]["facets"] is None


def test_too_long_post_is_rejected_before_any_api_call(client):
    result = client.post("a" * 301)
    assert not result.success
    assert "301" in result.error
    assert client._client.login_calls == []
    assert client._client.sent == []


def test_alt_text_reaches_the_image_embed(client, tmp_path):
    a, b = tmp_path / "kite.png", tmp_path / "sky.png"
    a.write_bytes(b"png-a")
    b.write_bytes(b"png-b")
    client.post("photos", image_paths=[str(a), str(b)], image_alts=["A red kite"])
    embed = client._client.sent[0]["embed"]
    assert isinstance(embed, at_models.AppBskyEmbedImages.Main)
    assert [i.alt for i in embed.images] == ["A red kite", "sky"]  # 2nd falls back to file name
    assert client._client.uploads == [b"png-a", b"png-b"]


def test_more_alts_than_images_is_an_error(client):
    result = client.post("x", image_alts=["orphan"])
    assert not result.success and "alt" in result.error


def test_missing_image_is_an_error_before_login(client):
    result = client.post("x", image_paths=["/no/such/file.png"])
    assert not result.success and "not found" in result.error
    assert client._client.login_calls == []


# -- replies and threads -----------------------------------------------

def test_reply_to_top_level_post_uses_parent_as_root(client):
    client._client.posts[ROOT] = make_post_view(ROOT, "cid-root")
    client.reply("hi", parent_uri=ROOT)
    ref = client._client.sent[0]["reply_to"]
    assert (ref.parent.uri, ref.parent.cid) == (ROOT, "cid-root")
    assert (ref.root.uri, ref.root.cid) == (ROOT, "cid-root")


def test_reply_to_nested_reply_carries_the_original_thread_root(client):
    parent_reply = SimpleNamespace(
        root=SimpleNamespace(uri=ROOT, cid="cid-root"),
        parent=SimpleNamespace(uri=ROOT, cid="cid-root"),
    )
    client._client.posts[MIDDLE] = make_post_view(MIDDLE, "cid-middle", reply=parent_reply)
    result = client.reply("deep reply", parent_uri=MIDDLE)
    assert result.success
    ref = client._client.sent[0]["reply_to"]
    assert (ref.parent.uri, ref.parent.cid) == (MIDDLE, "cid-middle")
    assert (ref.root.uri, ref.root.cid) == (ROOT, "cid-root")


def test_thread_chains_replies_under_one_root(client):
    text = " ".join(f"word{i}" for i in range(200))
    results = client.thread(text)
    sent = client._client.sent
    assert len(results) == len(sent) >= 3
    assert all(r.success for r in results)
    assert sent[0]["reply_to"] is None
    for i in range(1, len(sent)):
        ref = sent[i]["reply_to"]
        assert ref.root.uri == results[0].uri
        assert ref.parent.uri == results[i - 1].uri


def test_thread_stops_at_first_failure(client):
    client.login()
    fake = client._client
    original = fake.send_post

    def flaky(**kwargs):
        if len(fake.sent) == 1:
            raise RuntimeError("network down")
        return original(**kwargs)

    fake.send_post = flaky
    results = client.thread(["one", "two", "three"])
    assert [r.success for r in results] == [True, False]


# -- embeds ------------------------------------------------------------

def test_quote_post_produces_a_record_embed(client):
    client._client.posts[ROOT] = make_post_view(ROOT, "cid-root")
    client.post("worth reading", quote_uri=ROOT)
    embed = client._client.sent[0]["embed"]
    assert isinstance(embed, at_models.AppBskyEmbedRecord.Main)
    assert (embed.record.uri, embed.record.cid) == (ROOT, "cid-root")


def test_quote_with_image_produces_record_with_media(client, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    client._client.posts[ROOT] = make_post_view(ROOT, "cid-root")
    client.post("look", image_paths=[str(img)], quote_uri=ROOT)
    embed = client._client.sent[0]["embed"]
    assert isinstance(embed, at_models.AppBskyEmbedRecordWithMedia.Main)
    assert embed.record.record.uri == ROOT


def test_quote_accepts_a_bsky_app_url(client):
    uri = "at://did:plc:alice/app.bsky.feed.post/abc"
    client._client.posts[uri] = make_post_view(uri, "cid-abc")
    client.post("quoting", quote_uri="https://bsky.app/profile/alice.test/post/abc")
    assert client._client.sent[0]["embed"].record.uri == uri


def test_link_card_produces_an_external_embed(client, monkeypatch):
    monkeypatch.setattr(
        client_mod,
        "fetch_link_metadata",
        lambda url: {"title": "Example", "description": "A page", "thumb": None},
    )
    client.post("read this", link_card="https://example.com")
    embed = client._client.sent[0]["embed"]
    assert isinstance(embed, at_models.AppBskyEmbedExternal.Main)
    assert embed.external.uri == "https://example.com"
    assert embed.external.title == "Example"


def test_images_and_link_card_together_are_rejected(client, tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    result = client.post("x", image_paths=[str(img)], link_card="https://example.com")
    assert not result.success


# -- other actions -----------------------------------------------------

def test_like_repost_delete_and_search(client):
    client._client.posts[ROOT] = make_post_view(ROOT, "cid-root")
    assert client.like(ROOT).success
    assert client.repost(ROOT).success
    assert client.delete_post(ROOT) is True
    assert client._client.likes == [(ROOT, "cid-root")]
    assert client._client.reposts == [(ROOT, "cid-root")]
    assert client._client.deleted == [ROOT]
    assert client.search_posts("kites")[0]["text"] == "result for kites"


# -- session reuse -----------------------------------------------------

def test_second_start_reuses_cached_session(tmp_path, fake_atproto):
    config = AccountConfig("personal", "me.test", "app-pass")

    first = BskyClient(config, session_dir=tmp_path).login()
    assert first._client.login_calls == [
        {"login": "me.test", "password": "app-pass", "session_string": None}
    ]
    session_file = tmp_path / "personal.session"
    assert session_file.read_text() == "fake-session-string"
    assert (session_file.stat().st_mode & 0o777) == 0o600

    second = BskyClient(config, session_dir=tmp_path).login()
    assert second._client.login_calls == [
        {"login": None, "password": None, "session_string": "fake-session-string"}
    ]


def test_expired_session_falls_back_to_password(tmp_path, fake_atproto):
    config = AccountConfig("personal", "me.test", "app-pass")
    (tmp_path / "personal.session").write_text("stale")
    fake_atproto.fail_session_login = True

    client = BskyClient(config, session_dir=tmp_path).login()
    assert client._logged_in
    assert client._client.login_calls[-1]["password"] == "app-pass"


def test_no_session_file_without_session_dir(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client.login()
    assert list(tmp_path.iterdir()) == []
