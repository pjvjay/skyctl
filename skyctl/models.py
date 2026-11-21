# Copyright 2026 The skyctl Authors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
skyctl.models
=============
Shared dataclasses used across the library.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AccountConfig:
    """Credentials and identity for a single Bluesky account."""

    alias: str  # Short name used in env vars / CLI (e.g. "personal")
    handle: str  # Full handle (e.g. "omnidion.bsky.social")
    password: str  # App password (never your main password)
    pds_url: str | None = None  # Custom PDS, e.g. "http://localhost:2583" (default: bsky.social)


@dataclass
class PostResult:
    """Result returned after a post or reply action."""

    account: str
    uri: str
    cid: str
    url: str
    created_at: datetime = field(default_factory=_utcnow)
    error: str | None = None

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
    account: str  # Account alias to post from
    text: str
    run_at: datetime  # UTC time to send (creation time for cron jobs)
    reply_to_uri: str | None = None  # Optional: reply to this post URI
    image_paths: list[str] = field(default_factory=list)
    status: str = "pending"  # pending | retrying | sent | failed | recurring
    result: PostResult | None = None
    cron: str | None = None  # Crontab expression for recurring jobs
    attempts: int = 0  # Failed attempts so far
    last_error: str | None = None
    last_run: datetime | None = None  # When a cron job last fired (tick mode)
    retry_at: datetime | None = None  # Next retry time after a failure (tick mode)
    claimed_by: str | None = None  # Worker currently sending this job ("sending" status)
    claimed_at: datetime | None = None
    claimed_until: datetime | None = None  # After this, the claim is stale and recoverable
