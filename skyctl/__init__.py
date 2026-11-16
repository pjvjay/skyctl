"""skyctl — multi-account Bluesky automation library and CLI."""

from .client import BskyClient
from .manager import BskyManager
from .models import AccountConfig, PostResult, ScheduledPost
from .richtext import (
    MAX_GRAPHEMES,
    PostTooLongError,
    detect_facets,
    grapheme_len,
    split_thread,
)
from .scheduler import BskyScheduler

__all__ = [
    "AccountConfig",
    "BskyClient",
    "BskyManager",
    "BskyScheduler",
    "MAX_GRAPHEMES",
    "PostResult",
    "PostTooLongError",
    "ScheduledPost",
    "detect_facets",
    "grapheme_len",
    "split_thread",
]
