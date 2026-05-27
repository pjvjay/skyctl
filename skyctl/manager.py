"""
skyctl.manager
==============
Orchestrates actions across multiple Bluesky accounts.
Handles login pooling and fan-out operations.
"""

from __future__ import annotations

from typing import Optional

from .client import BskyClient
from .config import load_accounts
from .models import AccountConfig, PostResult


class BskyManager:
    """
    Manage and operate across multiple Bluesky accounts.

    Usage:
        manager = BskyManager.from_env()
        manager.post_all("Hello from all accounts!")
        manager.post("personal", "Just from my personal account")
        client = manager["work"]  # get a single BskyClient
    """

    def __init__(self, accounts: dict[str, AccountConfig]):
        self._clients: dict[str, BskyClient] = {
            alias: BskyClient(config)
            for alias, config in accounts.items()
        }

    # ------------------------------------------------------------------
    # Constructors
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, env_file: str = ".env") -> "BskyManager":
        """
        Load all accounts from environment variables and return a manager.

        Reads BSKY_ACCOUNTS and corresponding BSKY_<ALIAS>_HANDLE / PASSWORD vars.
        """
        accounts = load_accounts(env_file)
        return cls(accounts)

    @classmethod
    def from_dict(cls, accounts: dict[str, tuple[str, str]]) -> "BskyManager":
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
        return cls(configs)

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

    # ------------------------------------------------------------------
    # Fan-out operations
    # ------------------------------------------------------------------

    def post_all(
        self,
        text: str,
        image_paths: Optional[list[str]] = None,
        exclude: Optional[list[str]] = None,
    ) -> list[PostResult]:
        """
        Post the same text from all accounts (or all except those in `exclude`).

        Returns a list of PostResult objects, one per account.
        """
        exclude = [a.lower() for a in (exclude or [])]
        results = []
        for alias, client in self._clients.items():
            if alias in exclude:
                continue
            if not client._logged_in:
                client.login()
            results.append(client.post(text, image_paths=image_paths))
        return results

    def post(
        self,
        alias: str,
        text: str,
        image_paths: Optional[list[str]] = None,
    ) -> PostResult:
        """Post from a specific account by alias."""
        return self[alias].post(text, image_paths=image_paths)

    def reply(
        self,
        alias: str,
        text: str,
        parent_uri: str,
        parent_cid: Optional[str] = None,
    ) -> PostResult:
        """Reply to a post from a specific account."""
        return self[alias].reply(text, parent_uri=parent_uri, parent_cid=parent_cid)

    def reply_all(
        self,
        text: str,
        parent_uri: str,
        exclude: Optional[list[str]] = None,
    ) -> list[PostResult]:
        """Reply to the same post from all accounts."""
        exclude = [a.lower() for a in (exclude or [])]
        results = []
        for alias, client in self._clients.items():
            if alias in exclude:
                continue
            if not client._logged_in:
                client.login()
            results.append(client.reply(text, parent_uri=parent_uri))
        return results

    def repost(self, alias: str, uri: str, cid: Optional[str] = None) -> PostResult:
        """Repost from a specific account."""
        return self[alias].repost(uri, cid=cid)

    def repost_all(
        self,
        uri: str,
        exclude: Optional[list[str]] = None,
    ) -> list[PostResult]:
        """Repost from all accounts."""
        exclude = [a.lower() for a in (exclude or [])]
        results = []
        for alias, client in self._clients.items():
            if alias in exclude:
                continue
            if not client._logged_in:
                client.login()
            results.append(client.repost(uri))
        return results

    def __repr__(self):
        return f"<BskyManager accounts=[{', '.join(self._clients)}]>"
