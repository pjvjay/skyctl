"""
skyctl.client
=============
A thin, ergonomic wrapper around the atproto Client for a single account.
All methods return typed result objects rather than raw API dicts.
"""

from __future__ import annotations

import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from atproto import Client as AtProtoClient
from atproto import models as at_models

from .models import AccountConfig, PostResult


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uri_to_url(uri: str) -> str:
    """Convert an AT URI to a bsky.app URL."""
    # at://did:plc:xxx/app.bsky.feed.post/yyy
    try:
        parts = uri.replace("at://", "").split("/")
        did = parts[0]
        rkey = parts[2]
        return f"https://bsky.app/profile/{did}/post/{rkey}"
    except Exception:
        return uri


class BskyClient:
    """
    Single-account Bluesky client.

    Usage:
        client = BskyClient(config)
        result = client.post("Hello, Bluesky!")
        result = client.reply("Nice post!", parent_uri="at://...")
    """

    def __init__(self, config: AccountConfig):
        self.config = config
        self.alias = config.alias
        self._client = AtProtoClient()
        self._profile = None
        self._logged_in = False

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def login(self) -> "BskyClient":
        """Authenticate with Bluesky. Returns self for chaining."""
        self._profile = self._client.login(
            self.config.handle, self.config.password
        )
        self._logged_in = True
        return self

    def _ensure_logged_in(self):
        if not self._logged_in:
            self.login()

    # ------------------------------------------------------------------
    # Posting
    # ------------------------------------------------------------------

    def post(
        self,
        text: str,
        image_paths: Optional[list[str]] = None,
        langs: Optional[list[str]] = None,
    ) -> PostResult:
        """
        Create a new top-level post.

        Args:
            text: Post text (max 300 graphemes).
            image_paths: Optional list of local image file paths (max 4).
            langs: Language tags e.g. ["en", "fr"]. Defaults to ["en"].

        Returns:
            PostResult with uri, cid, and url.
        """
        self._ensure_logged_in()
        try:
            embed = self._build_image_embed(image_paths) if image_paths else None
            resp = self._client.send_post(
                text=text,
                embed=embed,
                langs=langs or ["en"],
            )
            return PostResult(
                account=self.alias,
                uri=resp.uri,
                cid=resp.cid,
                url=_uri_to_url(resp.uri),
            )
        except Exception as e:
            return PostResult(
                account=self.alias, uri="", cid="", url="", error=str(e)
            )

    def reply(
        self,
        text: str,
        parent_uri: str,
        parent_cid: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
        langs: Optional[list[str]] = None,
    ) -> PostResult:
        """
        Reply to an existing post.

        Args:
            text: Reply text.
            parent_uri: AT URI of the post to reply to.
            parent_cid: CID of the parent post (fetched automatically if omitted).
            image_paths: Optional images to attach.
            langs: Language tags.
        """
        self._ensure_logged_in()
        try:
            if parent_cid is None:
                parent_cid = self._fetch_cid(parent_uri)

            ref = at_models.ComAtprotoRepoStrongRef.Main(
                uri=parent_uri, cid=parent_cid
            )
            reply_ref = at_models.AppBskyFeedPost.ReplyRef(parent=ref, root=ref)
            embed = self._build_image_embed(image_paths) if image_paths else None

            resp = self._client.send_post(
                text=text,
                reply_to=reply_ref,
                embed=embed,
                langs=langs or ["en"],
            )
            return PostResult(
                account=self.alias,
                uri=resp.uri,
                cid=resp.cid,
                url=_uri_to_url(resp.uri),
            )
        except Exception as e:
            return PostResult(
                account=self.alias, uri="", cid="", url="", error=str(e)
            )

    def repost(self, uri: str, cid: Optional[str] = None) -> PostResult:
        """Repost (boost) an existing post."""
        self._ensure_logged_in()
        try:
            if cid is None:
                cid = self._fetch_cid(uri)
            resp = self._client.repost(uri=uri, cid=cid)
            return PostResult(
                account=self.alias,
                uri=resp.uri,
                cid=resp.cid,
                url=_uri_to_url(resp.uri),
            )
        except Exception as e:
            return PostResult(
                account=self.alias, uri="", cid="", url="", error=str(e)
            )

    def like(self, uri: str, cid: Optional[str] = None) -> PostResult:
        """Like a post."""
        self._ensure_logged_in()
        try:
            if cid is None:
                cid = self._fetch_cid(uri)
            resp = self._client.like(uri=uri, cid=cid)
            return PostResult(
                account=self.alias,
                uri=resp.uri,
                cid=resp.cid,
                url=_uri_to_url(resp.uri),
            )
        except Exception as e:
            return PostResult(
                account=self.alias, uri="", cid="", url="", error=str(e)
            )

    def delete_post(self, uri: str) -> bool:
        """Delete one of your own posts. Returns True on success."""
        self._ensure_logged_in()
        try:
            self._client.delete_post(uri)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Feeds & Profiles
    # ------------------------------------------------------------------

    def get_timeline(self, limit: int = 20) -> list[dict]:
        """Fetch the home timeline. Returns a list of post dicts."""
        self._ensure_logged_in()
        resp = self._client.get_timeline(limit=limit)
        return [self._flatten_post(item) for item in resp.feed]

    def get_notifications(self, limit: int = 20) -> list[dict]:
        """Fetch unread notifications."""
        self._ensure_logged_in()
        resp = self._client.app.bsky.notification.list_notifications(
            {"limit": limit}
        )
        return [n.model_dump() for n in resp.notifications]

    def get_profile(self, handle: Optional[str] = None) -> dict:
        """Get profile info. Defaults to the logged-in account."""
        self._ensure_logged_in()
        handle = handle or self.config.handle
        resp = self._client.get_profile(handle)
        return resp.model_dump()

    def search_posts(self, query: str, limit: int = 25) -> list[dict]:
        """Search posts by keyword."""
        self._ensure_logged_in()
        resp = self._client.app.bsky.feed.search_posts(
            {"q": query, "limit": limit}
        )
        return [self._flatten_post_record(p) for p in resp.posts]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_cid(self, uri: str) -> str:
        """Resolve an AT URI to its CID."""
        resp = self._client.get_posts([uri])
        if not resp.posts:
            raise ValueError(f"Post not found: {uri}")
        return resp.posts[0].cid

    def _build_image_embed(self, image_paths: list[str]):
        """Upload images and return an embed object."""
        images = []
        for path_str in image_paths[:4]:  # Bluesky max is 4 images
            path = Path(path_str)
            mime, _ = mimetypes.guess_type(path_str)
            mime = mime or "image/jpeg"
            with open(path, "rb") as f:
                data = f.read()
            blob = self._client.upload_blob(data, mime)
            images.append(
                at_models.AppBskyEmbedImages.Image(
                    image=blob.blob, alt=path.stem
                )
            )
        return at_models.AppBskyEmbedImages.Main(images=images)

    def _flatten_post(self, feed_item) -> dict:
        post = feed_item.post
        return {
            "uri": post.uri,
            "cid": post.cid,
            "author": post.author.handle,
            "text": post.record.text,
            "created_at": post.record.created_at,
            "likes": post.like_count,
            "reposts": post.repost_count,
            "replies": post.reply_count,
            "url": _uri_to_url(post.uri),
        }

    def _flatten_post_record(self, post) -> dict:
        return {
            "uri": post.uri,
            "cid": post.cid,
            "author": post.author.handle,
            "text": post.record.text,
            "created_at": post.record.created_at,
            "likes": post.like_count,
            "reposts": post.repost_count,
            "url": _uri_to_url(post.uri),
        }

    def __repr__(self):
        status = "authenticated" if self._logged_in else "unauthenticated"
        return f"<BskyClient alias={self.alias!r} handle={self.config.handle!r} {status}>"
