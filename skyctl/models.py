"""
skyctl.models
=============
Shared dataclasses used across the library.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AccountConfig:
    """Credentials and identity for a single Bluesky account."""
    alias: str          # Short name used in env vars / CLI (e.g. "personal")
    handle: str         # Full handle (e.g. "omnidion.bsky.social")
    password: str       # App password (never your main password)
    pds_url: Optional[str] = None   # Custom PDS, e.g. "http://localhost:2583" (default: bsky.social)


@dataclass
class PostResult:
    """Result returned after a post or reply action."""
    account: str
    uri: str
    cid: str
    url: str
    created_at: datetime = field(default_factory=_utcnow)
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return {
            "account": self.account,
            "success": self.success,
            "uri": self.uri,
            "cid": self.cid,
            "url": self.url,
            "created_at": self.created_at.isoformat(),
            "error": self.error,
        }


@dataclass
class ScheduledPost:
    """A post queued for future delivery."""
    id: str
    account: str                     # Account alias to post from
    text: str
    run_at: datetime                 # UTC time to send (creation time for cron jobs)
    reply_to_uri: Optional[str] = None   # Optional: reply to this post URI
    image_paths: list[str] = field(default_factory=list)
    status: str = "pending"          # pending | retrying | sent | failed | recurring
    result: Optional[PostResult] = None
    cron: Optional[str] = None       # Crontab expression for recurring jobs
    attempts: int = 0                # Failed attempts so far
    last_error: Optional[str] = None
