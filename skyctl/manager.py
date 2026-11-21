"""
skyctl.manager
==============
Orchestrates actions across multiple Bluesky accounts.
Handles login pooling and fan-out operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

from .client import BskyClient
from .config import load_accounts
from .models import AccountConfig, PostResult

if TYPE_CHECKING:  # pragma: no cover
    from .ports import ObjectStore, SessionStore


DEFAULT_SESSION_DIR = Path(".skyctl_sessions")


class BskyManager:
    """
    Manage and operate across multiple Bluesky accounts.

    Usage:
        manager = BskyManager.from_env()
        manager.post_all("Hello from all accounts!")
        manager.post("personal", "Just from my personal account")
        client = manager["work"]  # get a single BskyClient
    """

    def __init__(
        self,
        accounts: dict[str, AccountConfig],
        session_dir: Optional[Union[str, Path]] = None,
        session_store: Optional["SessionStore"] = None,
        media_store: Optional["ObjectStore"] = None,
        atproto_factory=None,
    ):
        self.accounts = dict(accounts)
        self._clients: dict[str, BskyClient] = {
            alias: BskyClient(
                config,
                session_dir=session_dir,
                session_store=session_store,
                media_store=media_store,
                atproto_factory=atproto_factory,
            )
            for alias, config in accounts.items()
        }

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        env_file: str = ".env",
        session_dir: Optional[Union[str, Path]] = DEFAULT_SESSION_DIR,
        **kwargs,
    ) -> "BskyManager":
        """
        Load all accounts from environment variables and return a manager.

        Reads BSKY_ACCOUNTS and corresponding BSKY_<ALIAS>_HANDLE / PASSWORD vars.
        Login sessions are cached in `session_dir` and reused on later runs;
        pass session_dir=None to log in with the app password every time.
        """
        accounts = load_accounts(env_file)
        return cls(accounts, session_dir=session_dir, **kwargs)

    @classmethod
    def from_settings(cls, settings=None) -> "BskyManager":
        """
        Build a manager for the current deployment: secrets are pulled from
        SKYCTL_SECRETS, sessions and media live in SKYCTL_STATE_URL. This is
        what the cloud entry points (serve, tick, mcp, agent) use.
        """
        from .platform.runtime import get_runtime

        runtime = get_runtime(settings)
        settings = runtime.settings
        accounts = load_accounts(settings.env_file if not settings.secrets_are_remote else "")
        return cls(
            accounts,
            session_store=runtime.session_store(),
            media_store=runtime.object_store(),
        )

    @classmethod
    def from_dict(
        cls,
        accounts: dict[str, tuple[str, str]],
        session_dir: Optional[Union[str, Path]] = None,
    ) -> "BskyManager":
        """
        Build a manager from a plain dict.

        Args:
            accounts: {alias: (handle, password)}

        Example:
            manager = BskyManager.from_dict({
                "personal": ("omnidion.bsky.social", "app-password"),
                "work": ("work.bsky.social", "app-password"),
            })
        """
        configs = {
            alias: AccountConfig(alias=alias, handle=handle, password=pw)
            for alias, (handle, pw) in accounts.items()
        }
        return cls(configs, session_dir=session_dir)

    # ------------------------------------------------------------------
    # Account access
    # ------------------------------------------------------------------

    def __getitem__(self, alias: str) -> BskyClient:
        """Get a single client by alias. Logs in automatically on first use."""
        alias = alias.lower()
        if alias not in self._clients:
            raise KeyError(
                f"Account '{alias}' not found. "
                f"Available: {', '.join(self._clients)}"
            )
        client = self._clients[alias]
        if not client._logged_in:
            client.login()
        return client

    def aliases(self) -> list[str]:
        """Return all registered account aliases."""
        return list(self._clients.keys())

    def login_all(self) -> "BskyManager":
        """
        Authenticate all accounts eagerly.
        Useful to catch bad credentials at startup rather than lazily.
        """
        for client in self._clients.values():
            if not client._logged_in:
                client.login()
        return self

    def client(self, alias: str) -> BskyClient:
        """Get a client by alias without logging in (it logs in on first use)."""
        alias = alias.lower()
        if alias not in self._clients:
            raise KeyError(
                f"Account '{alias}' not found. "
                f"Available: {', '.join(self._clients)}"
            )
        return self._clients[alias]

    def _selected(self, exclude: Optional[list[str]]) -> list[BskyClient]:
        skip = [a.lower() for a in (exclude or [])]
        return [c for alias, c in self._clients.items() if alias not in skip]

    # ------------------------------------------------------------------
    # Fan-out operations
    # ------------------------------------------------------------------

    def post_all(
        self,
        text: str,
        image_paths: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
        **kwargs,
    ) -> list[PostResult]:
        """
        Post the same text from all accounts (or all except those in `exclude`).
        Extra keyword arguments are passed to BskyClient.post.

        Returns a list of PostResult objects, one per account.
        """
        return [
            c.post(text, image_paths=image_paths, **kwargs)
            for c in self._selected(exclude)
        ]

    def post(
        self,
        alias: str,
        text: str,
        image_paths: Optional[list[str]] = None,
        **kwargs,
    ) -> PostResult:
        """Post from a specific account by alias."""
        return self.client(alias).post(text, image_paths=image_paths, **kwargs)

    def thread(self, alias: str, text, **kwargs) -> list[PostResult]:
        """Post a thread from a specific account."""
        return self.client(alias).thread(text, **kwargs)

    def reply(
        self,
        alias: str,
        text: str,
        parent_uri: str,
        parent_cid: Optional[str] = None,
        **kwargs,
    ) -> PostResult:
        """Reply to a post from a specific account."""
        return self.client(alias).reply(
            text, parent_uri=parent_uri, parent_cid=parent_cid, **kwargs
        )

    def reply_all(
        self,
        text: str,
        parent_uri: str,
        exclude: Optional[list[str]] = None,
    ) -> list[PostResult]:
        """Reply to the same post from all accounts."""
        return [
            c.reply(text, parent_uri=parent_uri) for c in self._selected(exclude)
        ]

    def repost(self, alias: str, uri: str, cid: Optional[str] = None) -> PostResult:
        """Repost from a specific account."""
        return self.client(alias).repost(uri, cid=cid)

    def repost_all(
        self,
        uri: str,
        exclude: Optional[list[str]] = None,
    ) -> list[PostResult]:
        """Repost from all accounts."""
        return [c.repost(uri) for c in self._selected(exclude)]

    def like(self, alias: str, uri: str, cid: Optional[str] = None) -> PostResult:
        """Like a post from a specific account."""
        return self.client(alias).like(uri, cid=cid)

    def delete_post(self, alias: str, uri: str) -> bool:
        """Delete one of an account's own posts."""
        return self.client(alias).delete_post(uri)

    def __repr__(self):
        return f"<BskyManager accounts=[{', '.join(self._clients)}]>"
