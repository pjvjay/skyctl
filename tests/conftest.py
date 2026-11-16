"""
Shared fixtures. Every test runs against FakeAtProto, so the suite needs
no network access and no real credentials.
"""

from types import SimpleNamespace

import pytest

import skyctl.client as client_mod
from skyctl import AccountConfig, BskyClient, BskyManager


class FakeAtProto:
    """Stands in for atproto.Client and records what it was asked to do."""

    instances = []
    fail_session_login = False

    def __init__(self):
        self.login_calls = []
        self.sent = []
        self.likes = []
        self.reposts = []
        self.deleted = []
        self.uploads = []
        self.posts = {}          # uri -> post view returned by get_posts
        self.fail_sends = 0      # make the next N send_post calls raise
        FakeAtProto.instances.append(self)

    # -- auth ----------------------------------------------------------
    def login(self, login=None, password=None, session_string=None):
        self.login_calls.append(
            {"login": login, "password": password, "session_string": session_string}
        )
        if session_string and FakeAtProto.fail_session_login:
            raise RuntimeError("session expired")
        return SimpleNamespace(handle=login or "cached.test")

    def export_session_string(self):
        return "fake-session-string"

    def on_session_change(self, callback):
        self.session_callback = callback

    # -- writes --------------------------------------------------------
    def send_post(self, text, reply_to=None, embed=None, langs=None, facets=None):
        if self.fail_sends > 0:
            self.fail_sends -= 1
            raise RuntimeError("network down")
        n = len(self.sent) + 1
        self.sent.append(
            {"text": text, "reply_to": reply_to, "embed": embed, "langs": langs, "facets": facets}
        )
        return SimpleNamespace(
            uri=f"at://did:plc:me/app.bsky.feed.post/sent{n}", cid=f"cid-sent{n}"
        )

    def upload_blob(self, data):
        self.uploads.append(data)
        blob = {
            "$type": "blob",
            "ref": {"$link": "bafkreibme22gw2h7y2h7tg2fhqotaqjucnbc24deqo72b6mkl2egezxhvy"},
            "mimeType": "image/png",
            "size": len(data),
        }
        return SimpleNamespace(blob=blob)

    def like(self, uri, cid):
        self.likes.append((uri, cid))
        return SimpleNamespace(uri="at://did:plc:me/app.bsky.feed.like/l1", cid="cid-like")

    def repost(self, uri, cid):
        self.reposts.append((uri, cid))
        return SimpleNamespace(uri="at://did:plc:me/app.bsky.feed.repost/r1", cid="cid-repost")

    def delete_post(self, uri):
        self.deleted.append(uri)
        return True

    # -- reads ---------------------------------------------------------
    def get_posts(self, uris):
        return SimpleNamespace(posts=[self.posts[u] for u in uris if u in self.posts])

    def resolve_handle(self, handle):
        if handle.startswith("unknown"):
            raise RuntimeError("handle not found")
        return SimpleNamespace(did=f"did:plc:{handle.split('.')[0]}")

    @property
    def app(self):
        def search_posts(params):
            post = SimpleNamespace(
                uri="at://did:plc:other/app.bsky.feed.post/found",
                cid="cid-found",
                author=SimpleNamespace(handle="other.test"),
                record=SimpleNamespace(text=f"result for {params['q']}", created_at="2026-01-01T00:00:00Z"),
                like_count=1,
                repost_count=2,
            )
            return SimpleNamespace(posts=[post])

        feed = SimpleNamespace(search_posts=search_posts)
        return SimpleNamespace(bsky=SimpleNamespace(feed=feed))


def make_post_view(uri, cid, reply=None):
    """A minimal post view, as get_posts would return it."""
    return SimpleNamespace(uri=uri, cid=cid, record=SimpleNamespace(reply=reply))


@pytest.fixture(autouse=True)
def fake_atproto(monkeypatch):
    FakeAtProto.instances = []
    FakeAtProto.fail_session_login = False
    monkeypatch.setattr(client_mod, "AtProtoClient", FakeAtProto)
    return FakeAtProto


@pytest.fixture
def client():
    return BskyClient(AccountConfig("personal", "me.test", "app-pass"))


@pytest.fixture
def manager():
    return BskyManager.from_dict({
        "personal": ("me.test", "app-pass"),
        "work": ("work.test", "app-pass"),
    })
