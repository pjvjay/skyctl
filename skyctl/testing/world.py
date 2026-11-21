"""
skyctl.testing.world
====================
An in-memory Bluesky network for evaluations and tests. It implements the
slice of the atproto client API that skyctl.client uses (login, posting,
likes, reposts, deletes, get_posts, timeline, search, notifications,
list_records, profiles), shared across accounts like a real PDS + AppView.

    world = World()
    work = world.add_account("work.test")
    q = world.mention("work.test", "fan.test", "Is there a student discount?", key="q1")
    manager = BskyManager(accounts, atproto_factory=world.client_factory)

Every write is appended to `world.writes`, so a harness can assert that an
evaluation proposed plans but executed nothing.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Optional


@dataclass
class _Post:
    uri: str
    cid: str
    did: str
    handle: str
    text: str
    created_at: str
    reply_parent: Optional[str] = None
    reply_root: Optional[str] = None
    likes: int = 0


class World:
    def __init__(self, now: Optional[datetime] = None):
        self.now = now or datetime.now(timezone.utc)
        self.dids: dict[str, str] = {}                 # handle -> did
        self.posts: dict[str, _Post] = {}
        self.notifications: dict[str, list[dict]] = {}  # did -> notifications (newest first)
        self.timelines: dict[str, list[str]] = {}       # did -> post uris (newest first)
        self.keys: dict[str, str] = {}                  # scenario key -> uri
        self.writes: list[dict] = []
        self._n = itertools.count(1)

    # -- building -----------------------------------------------------------

    def add_account(self, handle: str) -> str:
        if handle not in self.dids:
            self.dids[handle] = f"did:plc:{handle.split('.')[0]}{len(self.dids)}"
            self.notifications[self.dids[handle]] = []
            self.timelines[self.dids[handle]] = []
        return self.dids[handle]

    def add_post(self, handle: str, text: str, key: Optional[str] = None, reply_to: Optional[str] = None,
                 minutes_ago: int = 30) -> str:
        did = self.add_account(handle)
        n = next(self._n)
        uri = f"at://{did}/app.bsky.feed.post/{n:05d}"
        created = (self.now - timedelta(minutes=minutes_ago)).isoformat()
        parent = self.posts.get(reply_to) if reply_to else None
        root = (parent.reply_root or parent.uri) if parent else None
        self.posts[uri] = _Post(uri, f"cid{n}", did, handle, text, created, reply_to, root)
        if key:
            self.keys[key] = uri
        return uri

    def mention(self, to_handle: str, from_handle: str, text: str, key: Optional[str] = None,
                reason: str = "mention", minutes_ago: int = 30) -> str:
        if f"@{to_handle}" not in text and reason == "mention":
            text = f"@{to_handle} {text}"
        uri = self.add_post(from_handle, text, key=key, minutes_ago=minutes_ago)
        to = self.add_account(to_handle)
        self.notifications[to].insert(0, {"reason": reason, "uri": uri, "author": from_handle})
        return uri

    def on_timeline(self, of_handle: str, uri: str) -> None:
        self.timelines[self.add_account(of_handle)].insert(0, uri)

    def uri(self, key: str) -> str:
        return self.keys[key]

    def client_factory(self, base_url: Optional[str] = None) -> "WorldClient":
        return WorldClient(self)

    # -- views --------------------------------------------------------------------

    def view(self, uri: str) -> SimpleNamespace:
        p = self.posts[uri]
        reply = None
        if p.reply_parent:
            parent, root = self.posts[p.reply_parent], self.posts[p.reply_root]
            reply = SimpleNamespace(parent=SimpleNamespace(uri=parent.uri, cid=parent.cid),
                                    root=SimpleNamespace(uri=root.uri, cid=root.cid))
        return SimpleNamespace(
            uri=p.uri, cid=p.cid, author=SimpleNamespace(handle=p.handle, did=p.did),
            record=SimpleNamespace(text=p.text, created_at=p.created_at, reply=reply),
            like_count=p.likes, repost_count=0,
            reply_count=sum(1 for q in self.posts.values() if q.reply_parent == uri),
        )


class WorldClient:
    """One account's connection to the World (the atproto.Client surface skyctl uses)."""

    def __init__(self, world: World):
        self.world = world
        self.me = None

    # -- auth ---------------------------------------------------------------------
    def login(self, login=None, password=None, session_string=None):
        handle = login or (session_string or "").split(":", 1)[-1]
        self.me = SimpleNamespace(did=self.world.add_account(handle), handle=handle)
        return SimpleNamespace(handle=handle)

    def export_session_string(self):
        return f"session:{self.me.handle}"

    def on_session_change(self, callback):
        pass

    # -- writes -------------------------------------------------------------------
    def send_post(self, text, reply_to=None, embed=None, langs=None, facets=None):
        parent = reply_to.parent.uri if reply_to is not None else None
        uri = self.world.add_post(self.me.handle, text, reply_to=parent, minutes_ago=0)
        self.world.writes.append({"op": "post", "by": self.me.handle, "text": text, "reply_to": parent, "uri": uri})
        return SimpleNamespace(uri=uri, cid=self.world.posts[uri].cid)

    def upload_blob(self, data):
        self.world.writes.append({"op": "upload", "by": self.me.handle, "bytes": len(data)})
        return SimpleNamespace(blob={"$type": "blob", "ref": {"$link": "bafkreiworld"}, "mimeType": "image/png", "size": len(data)})

    def like(self, uri, cid):
        self.world.posts[uri].likes += 1
        self.world.writes.append({"op": "like", "by": self.me.handle, "uri": uri})
        return SimpleNamespace(uri=f"at://{self.me.did}/app.bsky.feed.like/{next(self.world._n)}", cid="cid-like")

    def repost(self, uri, cid):
        self.world.writes.append({"op": "repost", "by": self.me.handle, "uri": uri})
        return SimpleNamespace(uri=f"at://{self.me.did}/app.bsky.feed.repost/{next(self.world._n)}", cid="cid-repost")

    def delete_post(self, uri):
        self.world.writes.append({"op": "delete", "by": self.me.handle, "uri": uri})
        self.world.posts.pop(uri, None)
        return True

    # -- reads ----------------------------------------------------------------------
    def get_posts(self, uris):
        return SimpleNamespace(posts=[self.world.view(u) for u in uris if u in self.world.posts])

    def resolve_handle(self, handle):
        if handle not in self.world.dids:
            raise RuntimeError(f"unknown handle {handle}")
        return SimpleNamespace(did=self.world.dids[handle])

    def get_timeline(self, limit=20):
        uris = [u for u in self.world.timelines.get(self.me.did, []) if u in self.world.posts][:limit]
        return SimpleNamespace(feed=[SimpleNamespace(post=self.world.view(u)) for u in uris])

    def get_profile(self, handle):
        did = self.world.add_account(handle)
        n_posts = sum(1 for p in self.world.posts.values() if p.did == did)
        return SimpleNamespace(model_dump=lambda: {"did": did, "handle": handle, "display_name": handle.split(".")[0].title(),
                                                   "description": "", "followers_count": 0, "follows_count": 0,
                                                   "posts_count": n_posts})

    @property
    def app(self):
        world, me = self.world, self.me

        def search_posts(params):
            q = params["q"].lower()
            hits = [p.uri for p in sorted(world.posts.values(), key=lambda p: p.created_at, reverse=True)
                    if any(w in p.text.lower() for w in q.split())]
            return SimpleNamespace(posts=[world.view(u) for u in hits[: params.get("limit", 25)]])

        def list_notifications(params):
            items = []
            for n in world.notifications.get(me.did, [])[: params.get("limit", 20)]:
                p = world.posts.get(n["uri"])
                if p is None:
                    continue
                doc = {"reason": n["reason"], "author": {"handle": p.handle}, "record": {"text": p.text},
                       "uri": p.uri, "cid": p.cid, "reason_subject": None, "indexed_at": p.created_at, "is_read": False}
                items.append(SimpleNamespace(model_dump=lambda d=doc: d))
            return SimpleNamespace(notifications=items)

        return SimpleNamespace(bsky=SimpleNamespace(feed=SimpleNamespace(search_posts=search_posts),
                                                    notification=SimpleNamespace(list_notifications=list_notifications)))

    @property
    def com(self):
        world = self.world

        def list_records(params):
            did = params["repo"] if params["repo"].startswith("did:") else world.dids.get(params["repo"])
            mine = sorted((p for p in world.posts.values() if p.did == did), key=lambda p: p.created_at, reverse=True)
            recs = [SimpleNamespace(uri=p.uri, cid=p.cid, value={"text": p.text, "created_at": p.created_at,
                                                                 "reply": None}) for p in mine[: params.get("limit", 30)]]
            return SimpleNamespace(records=recs)

        return SimpleNamespace(atproto=SimpleNamespace(repo=SimpleNamespace(list_records=list_records)))
