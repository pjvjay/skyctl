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

"""skyctl — multi-account Bluesky automation library and CLI."""

__version__ = "0.2.0"

from .agents import (
    AgentAction,
    AgentGroup,
    GroupConfig,
    Persona,
    Personality,
    load_agents_config,
)
from .client import BskyClient
from .llm import LLMError, get_provider, register_provider
from .manager import BskyManager
from .models import AccountConfig, PostResult, ScheduledPost
from .richtext import (
    MAX_GRAPHEMES,
    PostTooLongError,
    detect_facets,
    grapheme_len,
    split_thread,
)
from .scheduler import BskyScheduler, TickResult
from .settings import Settings, load_settings
from .tools import ToolContext, ToolPolicy, ToolRegistry
from .tools import registry as tool_registry

__all__ = [
    "MAX_GRAPHEMES",
    "AccountConfig",
    "AgentAction",
    "AgentGroup",
    "BskyClient",
    "BskyManager",
    "BskyScheduler",
    "GroupConfig",
    "LLMError",
    "Persona",
    "Personality",
    "PostResult",
    "PostTooLongError",
    "ScheduledPost",
    "Settings",
    "TickResult",
    "ToolContext",
    "ToolPolicy",
    "ToolRegistry",
    "detect_facets",
    "get_provider",
    "grapheme_len",
    "load_agents_config",
    "load_settings",
    "register_provider",
    "split_thread",
    "tool_registry",
]
