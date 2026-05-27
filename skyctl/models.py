"""
skyctl.models
=============
Shared dataclasses used across the library.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class AccountConfig:
    """Credentials and identity for a single Bluesky account."""
    alias: str          # Short name used in env vars / CLI (e.g. "personal")
    handle: str         # Full handle (e.g. "omnidion.bsky.social")
    password: str       # App password (never your main password)


@dataclass
class PostResult:
    """Result returned after a post or reply action."""
    account: str
    uri: str
    cid: str
    url: str
    created_at: datetime = field(default_factory=datetime.utcnow)
    error: Optional[str] = None

    @property
    def success(self) -> bool:
        return self.error is None


@dataclass
class ScheduledPost:
    """A post queued for future delivery."""
    id: str
    account: str                     # Account alias to post from
    text: str
    run_at: datetime                 # UTC time to send
    reply_to_uri: Optional[str] = None   # Optional: reply to this post URI
    image_paths: list[str] = field(default_factory=list)
    status: str = "pending"          # pending | sent | failed
    result: Optional[PostResult] = None
