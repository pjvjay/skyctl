"""
skyctl.client
=============
A thin, ergonomic wrapper around the atproto Client for a single account.
All methods return typed result objects rather than raw API dicts.
"""

from __future__ import annotations

import html as html_lib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

from atproto import Client as AtProtoClient
from atproto import models as at_models

from .models import AccountConfig, PostResult
from .richtext import detect_facets, split_thread, validate_length

if TYPE_CHECKING:  # pragma: no cover
    from .ports import ObjectStore, SessionStore

MAX_IMAGES = 4
MAX_THUMB_BYTES = 1_000_000

_BSKY_URL_RE = re.compile(
    r"^https?://bsky\.app/profile/([^/]+)/post/([^/?#]+)"
)


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


def _meta_content(page: str, prop: str) -> str:
    """Pull an <meta property|name=prop content=...> value out of HTML."""
    for pattern in (
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop)}["\'][^>]*content=["\']([^"\']*)["\']',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name)=["\']{re.escape(prop)}["\']',
    ):
        m = re.search(pattern, page, re.IGNORECASE)
        if m:
            return html_lib.unescape(m.group(1)).strip()
    return ""


def fetch_link_metadata(url: str) -> dict:
    """
    Fetch title, description and preview image bytes for a link card.
    Never raises: on any failure the card falls back to the bare URL.
    """
    meta = {"title": url, "description": "", "thumb": None}
    try:
        import httpx

        headers = {"User-Agent": "skyctl-linkcard"}
        resp = httpx.get(url, follow_redirects=True, timeout=10, headers=headers)
        page = resp.text[:300_000]
        title = _meta_content(page, "og:title")
        if not title:
            m = re.search(r"<title[^>]*>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
            title = html_lib.unescape(m.group(1)).strip() if m else ""
        meta["title"] = title or url
        meta["description"] = _meta_content(page, "og:description") or _meta_content(
            page, "description"
        )
        image_url = _meta_content(page, "og:image")
        if image_url:
            image_url = str(httpx.URL(str(resp.url)).join(image_url))
            img = httpx.get(image_url, follow_redirects=True, timeout=10, headers=headers)
            if img.status_code == 200 and len(img.content) <= MAX_THUMB_BYTES:
                meta["thumb"] = img.content
    except Exception:
        pass
    return meta


class BskyClient:
    """
    Single-account Bluesky client.

    Usage:
        client = BskyClient(config)
        result = client.post("Hello, Bluesky!")
        result = client.reply("Nice post!", parent_uri="at://...")

    Pass `session_dir` to cache the login session on disk so later runs
    reuse it instead of logging in with the app password again.
    """

    def __init__(
        self,
        config: AccountConfig,
        session_dir: Optional[Union[str, Path]] = None,
        session_store: Optional["SessionStore"] = None,
        media_store: Optional["ObjectStore"] = None,
    ):
        """
        Args:
            config: Account credentials.
            session_dir: Directory for cached sessions (legacy; wraps a
                FileSessionStore). Ignored when session_store is given.
            session_store: Any SessionStore (see skyctl.ports). In the cloud
                this is backed by the shared state bucket.
            media_store: Where image keys are read from when an image path
                is not a local file (e.g. "media/kite.jpg" in the state bucket).
        """
        self.config = config
        self.alias = config.alias
        if session_store is None and session_dir is not None:
            from .adapters.local import FileSessionStore

            session_store = FileSessionStore(session_dir)
        self.session_store = session_store
        self.media_store = media_store
        self._client = self._new_client()
        self._profile = None
        self._logged_in = False

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _new_client(self):
        if self.config.pds_url:
            return AtProtoClient(self.config.pds_url)
        return AtProtoClient()

    @property
    def did(self) -> str:
        """The DID of the logged-in account."""
        self._ensure_logged_in()
        return self._client.me.did

    def login(self) -> "BskyClient":
        """
        Authenticate with Bluesky. Returns self for chaining.

        Reuses a cached session when one exists and is still valid;
        otherwise logs in with the app password and caches the new session.
        """
        cached = self.session_store.get(self.alias) if self.session_store else None
        if cached:
            try:
                self._profile = self._client.login(session_string=cached)
                self._after_login()
                return self
            except Exception:
                # Expired or corrupt session: start clean and use the password.
                self._client = self._new_client()

        self._profile = self._client.login(
            self.config.handle, self.config.password
        )
        self._after_login()
        return self

    def _after_login(self):
        self._logged_in = True
        self._save_session()
        on_change = getattr(self._client, "on_session_change", None)
        if on_change is not None and self.session_store is not None:
            on_change(lambda *_: self._save_session())

    def _save_session(self):
        if self.session_store is None:
            return
        try:
            self.session_store.set(self.alias, self._client.export_session_string())
        except Exception:
            pass  # Session caching is best-effort.

    def logout(self) -> None:
        """Forget the cached session for this account."""
        if self.session_store is not None:
            try:
                self.session_store.clear(self.alias)
            except Exception:
                pass
        self._logged_in = False

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
        image_alts: Optional[list[str]] = None,
        quote_uri: Optional[str] = None,
        link_card: Optional[str] = None,
    ) -> PostResult:
        """
        Create a new top-level post.

        Links, @mentions and #hashtags in the text are made clickable.

        Args:
            text: Post text (max 300 graphemes).
            image_paths: Optional list of local image file paths (max 4).
            langs: Language tags e.g. ["en", "fr"]. Defaults to ["en"].
            image_alts: Alt text per image, in the same order as image_paths.
            quote_uri: AT URI or bsky.app URL of a post to quote.
            link_card: URL to attach as a link preview card.

        Returns:
            PostResult with uri, cid, and url.
        """
        return self._send(
            text,
            image_paths=image_paths,
            image_alts=image_alts,
            langs=langs,
            quote_uri=quote_uri,
            link_card=link_card,
        )

    def reply(
        self,
        text: str,
        parent_uri: str,
        parent_cid: Optional[str] = None,
        image_paths: Optional[list[str]] = None,
        langs: Optional[list[str]] = None,
        image_alts: Optional[list[str]] = None,
        root_uri: Optional[str] = None,
        root_cid: Optional[str] = None,
        quote_uri: Optional[str] = None,
        link_card: Optional[str] = None,
    ) -> PostResult:
        """
        Reply to an existing post.

        Args:
            text: Reply text.
            parent_uri: AT URI (or bsky.app URL) of the post to reply to.
            parent_cid: CID of the parent post (fetched automatically if omitted).
            image_paths: Optional images to attach.
            langs: Language tags.
            image_alts: Alt text per image.
            root_uri / root_cid: Thread root, if already known. Otherwise it
                is looked up from the parent so nested replies thread correctly.
        """
        return self._send(
            text,
            image_paths=image_paths,
            image_alts=image_alts,
            langs=langs,
            quote_uri=quote_uri,
            link_card=link_card,
            reply_to=(parent_uri, parent_cid, root_uri, root_cid),
        )

    def thread(
        self,
        text: Union[str, list[str]],
        image_paths: Optional[list[str]] = None,
        image_alts: Optional[list[str]] = None,
        langs: Optional[list[str]] = None,
    ) -> list[PostResult]:
        """
        Post a thread. A string is split into parts of at most 300
        graphemes; a list is posted as given. Images go on the first post.

        Stops at the first failure. Returns one PostResult per attempted post.
        """
        parts = split_thread(text) if isinstance(text, str) else list(text)
        if not parts:
            return [self._error("Thread text is empty.")]
        for part in parts:
            try:
                validate_length(part)
            except ValueError as e:
                return [self._error(str(e))]

        results = [
            self.post(
                parts[0], image_paths=image_paths, image_alts=image_alts, langs=langs
            )
        ]
        root = results[0]
        for part in parts[1:]:
            prev = results[-1]
            if not prev.success:
                break
            results.append(
                self.reply(
                    part,
                    parent_uri=prev.uri,
                    parent_cid=prev.cid,
                    root_uri=root.uri,
                    root_cid=root.cid,
                    langs=langs,
                )
            )
        return results

    def repost(self, uri: str, cid: Optional[str] = None) -> PostResult:
        """Repost (boost) an existing post."""
        self._ensure_logged_in()
        try:
            uri = self._normalize_uri(uri)
            if cid is None:
                cid = self._fetch_cid(uri)
            resp = self._client.repost(uri=uri, cid=cid)
            return self._ok(resp)
        except Exception as e:
            return self._error(str(e))

    def like(self, uri: str, cid: Optional[str] = None) -> PostResult:
        """Like a post."""
        self._ensure_logged_in()
        try:
            uri = self._normalize_uri(uri)
            if cid is None:
                cid = self._fetch_cid(uri)
            resp = self._client.like(uri=uri, cid=cid)
            return self._ok(resp)
        except Exception as e:
            return self._error(str(e))

    def delete_post(self, uri: str) -> bool:
        """Delete one of your own posts. Returns True on success."""
        self._ensure_logged_in()
        try:
            self._client.delete_post(self._normalize_uri(uri))
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

    def get_post(self, uri: str) -> dict:
        """Fetch one post by AT URI or bsky.app URL."""
        self._ensure_logged_in()
        uri = self._normalize_uri(uri)
        post = self._fetch_post(uri)
        data = self._flatten_post_record(post)
        reply = getattr(post.record, "reply", None)
        data["reply_root"] = reply.root.uri if reply is not None else None
        data["reply_parent"] = reply.parent.uri if reply is not None else None
        data["replies"] = getattr(post, "reply_count", None)
        return data

    def list_posts(self, actor: Optional[str] = None, limit: int = 30) -> list[dict]:
        """
        List an account's recent posts (newest first) straight from its
        repo on the PDS. `actor` is a handle or DID; defaults to this account.

        Unlike the timeline and search, this needs no AppView, so it also
        works on a self-hosted PDS that is not connected to the network.
        """
        self._ensure_logged_in()
        repo = actor or self.did
        resp = self._client.com.atproto.repo.list_records(
            {"repo": repo, "collection": "app.bsky.feed.post", "limit": limit}
        )
        return [self._flatten_repo_record(r, repo) for r in resp.records]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_repo_record(record, author: str) -> dict:
        def field(obj, *names):
            for name in names:
                value = (
                    obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
                )
                if value is not None:
                    return value
            return None

        def ref(obj):
            if obj is None:
                return None
            return {"uri": field(obj, "uri"), "cid": field(obj, "cid")}

        value = record.value
        reply = field(value, "reply")
        return {
            "uri": record.uri,
            "cid": record.cid,
            "author": author,
            "text": field(value, "text") or "",
            "created_at": field(value, "created_at", "createdAt") or "",
            "reply_parent": ref(field(reply, "parent")) if reply else None,
            "reply_root": ref(field(reply, "root")) if reply else None,
            "url": _uri_to_url(record.uri),
        }

    def _ok(self, resp) -> PostResult:
        return PostResult(
            account=self.alias,
            uri=resp.uri,
            cid=resp.cid,
            url=_uri_to_url(resp.uri),
        )

    def _error(self, message: str) -> PostResult:
        return PostResult(
            account=self.alias, uri="", cid="", url="", error=message
        )

    def _send(
        self,
        text: str,
        image_paths=None,
        image_alts=None,
        langs=None,
        quote_uri=None,
        link_card=None,
        reply_to=None,
    ) -> PostResult:
        # Validate everything we can before touching the network.
        try:
            validate_length(text)
            self._check_media_args(image_paths, image_alts, link_card)
        except ValueError as e:
            return self._error(str(e))

        try:
            self._ensure_logged_in()
            reply_ref = self._build_reply_ref(*reply_to) if reply_to else None
            embed = self._build_embed(image_paths, image_alts, quote_uri, link_card)
            resp = self._client.send_post(
                text=text,
                reply_to=reply_ref,
                embed=embed,
                langs=langs or ["en"],
                facets=self._build_facets(text) or None,
            )
            return self._ok(resp)
        except Exception as e:
            return self._error(str(e))

    def _check_media_args(self, image_paths, image_alts, link_card):
        image_paths = image_paths or []
        image_alts = image_alts or []
        if len(image_paths) > MAX_IMAGES:
            raise ValueError(f"At most {MAX_IMAGES} images per post.")
        if len(image_alts) > len(image_paths):
            raise ValueError("More alt texts than images.")
        if image_paths and link_card:
            raise ValueError("A post cannot have both images and a link card.")
        for path in image_paths:
            if not self._image_exists(path):
                raise ValueError(f"Image not found: {path}")

    def _image_exists(self, path: str) -> bool:
        if Path(path).is_file():
            return True
        return bool(self.media_store is not None and self.media_store.exists(path))

    def _read_image(self, path: str) -> bytes:
        if Path(path).is_file():
            return Path(path).read_bytes()
        if self.media_store is None:
            raise FileNotFoundError(path)
        return self.media_store.read(path)

    def _normalize_uri(self, uri: str) -> str:
        """Accept an AT URI or a bsky.app post URL; return an AT URI."""
        m = _BSKY_URL_RE.match(uri)
        if not m:
            return uri
        actor, rkey = m.groups()
        if not actor.startswith("did:"):
            actor = self._client.resolve_handle(actor).did
        return f"at://{actor}/app.bsky.feed.post/{rkey}"

    def _fetch_post(self, uri: str):
        resp = self._client.get_posts([uri])
        if not resp.posts:
            raise ValueError(f"Post not found: {uri}")
        return resp.posts[0]

    def _fetch_cid(self, uri: str) -> str:
        """Resolve an AT URI to its CID."""
        return self._fetch_post(uri).cid

    def _build_reply_ref(self, parent_uri, parent_cid, root_uri, root_cid):
        """
        Build the reply reference. The root must be the first post of the
        thread, not the parent, so it is read from the parent's own reply
        reference when the parent is itself a reply.
        """
        StrongRef = at_models.ComAtprotoRepoStrongRef.Main
        parent_uri = self._normalize_uri(parent_uri)

        if parent_cid and root_uri and root_cid:
            parent = StrongRef(uri=parent_uri, cid=parent_cid)
            root = StrongRef(uri=root_uri, cid=root_cid)
        else:
            parent_post = self._fetch_post(parent_uri)
            parent = StrongRef(uri=parent_uri, cid=parent_cid or parent_post.cid)
            parent_reply = getattr(parent_post.record, "reply", None)
            if parent_reply is not None:
                root = StrongRef(
                    uri=parent_reply.root.uri, cid=parent_reply.root.cid
                )
            else:
                root = parent
        return at_models.AppBskyFeedPost.ReplyRef(parent=parent, root=root)

    def _build_facets(self, text: str) -> list:
        """Turn detected links, mentions and hashtags into facet models."""
        F = at_models.AppBskyRichtextFacet
        facets = []
        for f in detect_facets(text):
            if f.kind == "link":
                feature = F.Link(uri=f.value)
            elif f.kind == "tag":
                feature = F.Tag(tag=f.value)
            else:
                try:
                    did = self._client.resolve_handle(f.value).did
                except Exception:
                    continue  # Unknown handle: leave it as plain text.
                feature = F.Mention(did=did)
            facets.append(
                F.Main(
                    index=F.ByteSlice(byte_start=f.byte_start, byte_end=f.byte_end),
                    features=[feature],
                )
            )
        return facets

    def _build_embed(self, image_paths, image_alts, quote_uri, link_card):
        media = None
        if image_paths:
            media = self._build_image_embed(image_paths, image_alts)
        elif link_card:
            media = self._build_link_card(link_card)

        if not quote_uri:
            return media

        quote_uri = self._normalize_uri(quote_uri)
        record = at_models.AppBskyEmbedRecord.Main(
            record=at_models.ComAtprotoRepoStrongRef.Main(
                uri=quote_uri, cid=self._fetch_cid(quote_uri)
            )
        )
        if media is None:
            return record
        return at_models.AppBskyEmbedRecordWithMedia.Main(record=record, media=media)

    def _build_image_embed(
        self, image_paths: list[str], image_alts: Optional[list[str]] = None
    ):
        """Upload images and return an embed object."""
        image_alts = image_alts or []
        images = []
        for i, path_str in enumerate(image_paths[:MAX_IMAGES]):
            path = Path(path_str)
            data = self._read_image(path_str)
            blob = self._client.upload_blob(data)
            alt = image_alts[i] if i < len(image_alts) else path.stem
            images.append(
                at_models.AppBskyEmbedImages.Image(image=blob.blob, alt=alt)
            )
        return at_models.AppBskyEmbedImages.Main(images=images)

    def _build_link_card(self, url: str):
        meta = fetch_link_metadata(url)
        thumb = None
        if meta.get("thumb"):
            try:
                thumb = self._client.upload_blob(meta["thumb"]).blob
            except Exception:
                thumb = None
        return at_models.AppBskyEmbedExternal.Main(
            external=at_models.AppBskyEmbedExternal.External(
                uri=url,
                title=meta.get("title") or url,
                description=meta.get("description") or "",
                thumb=thumb,
            )
        )

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
        author = getattr(post, "author", None)
        record = getattr(post, "record", None)
        return {
            "uri": post.uri,
            "cid": post.cid,
            "author": getattr(author, "handle", None),
            "text": getattr(record, "text", ""),
            "created_at": getattr(record, "created_at", None),
            "likes": getattr(post, "like_count", None),
            "reposts": getattr(post, "repost_count", None),
            "url": _uri_to_url(post.uri),
        }

    def __repr__(self):
        status = "authenticated" if self._logged_in else "unauthenticated"
        return f"<BskyClient alias={self.alias!r} handle={self.config.handle!r} {status}>"
