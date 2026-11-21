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
skyctl.tools.policy
===================
What a tool consumer (an MCP client or the task agent) is allowed to do.
Evaluated in code before every call, so nothing in a prompt can widen it.

    ToolPolicy(
        accounts=["work"],        # only these aliases are visible/usable
        allow_writes=False,       # mutating tools run as dry-run stubs
        allow_delete=False,       # delete_post / cancel_job denied outright
        max_writes=5,             # hard cap on executed mutating calls
        allow_public_writes=True, # False: writes only to accounts on a private PDS
        deny_tools=["thread"],    # hidden and denied
        expose_tags={"read", "plan"},  # profile: what a session may call DIRECTLY
    )

Exposure vs permission: `visible()` decides what a session can call
directly (profiles use tags, e.g. the "plan" profile exposes reads and plan
tools but no direct writes). `permits()` decides what a validated plan may
execute. A direct call must be visible; a plan step need only be permitted,
which is how an agent can propose `post` without being able to call it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .registry import ToolDenied, ToolSpec


@dataclass
class ToolPolicy:
    accounts: list[str] | None = None  # None → every configured alias
    allow_writes: bool = False
    allow_delete: bool = False
    max_writes: int = 5
    allow_public_writes: bool = True
    deny_tools: list[str] = field(default_factory=list)
    allow_tools: list[str] | None = None  # None → everything not denied
    approval: str = "none"  # "writes": record mutating calls for a human instead of sending
    expose_tags: set | None = None  # None → every permitted tool is directly callable

    def __post_init__(self):
        self.accounts = [a.lower() for a in self.accounts] if self.accounts is not None else None
        self.deny_tools = [t.lower() for t in self.deny_tools]
        if self.allow_tools is not None:
            self.allow_tools = [t.lower() for t in self.allow_tools]

    # -- visibility ----------------------------------------------------

    def permits(self, spec: ToolSpec) -> bool:
        """Whether this tool may run at all (directly or as a plan step)."""
        if spec.name in self.deny_tools:
            return False
        if self.allow_tools is not None and spec.name not in self.allow_tools:
            return False
        return not (spec.destructive and not self.allow_delete)

    def visible(self, spec: ToolSpec) -> bool:
        """Whether a session may call (and be shown) this tool directly."""
        if not self.permits(spec):
            return False
        return not (self.expose_tags is not None and not set(spec.tags) & set(self.expose_tags))

    def account_allowed(self, alias: str) -> bool:
        return self.accounts is None or alias.lower() in self.accounts

    def filter_accounts(self, aliases: list[str]) -> list[str]:
        return [a for a in aliases if self.account_allowed(a)]

    # -- enforcement ---------------------------------------------------

    def check(self, spec: ToolSpec, args: dict, ctx) -> None:
        """Raise ToolDenied if this call must not happen."""
        allowed = self.permits(spec) if getattr(ctx, "via_plan", False) else self.visible(spec)
        if not allowed:
            hint = " Propose it as a step in a plan (plan_create) instead." if self.permits(spec) else ""
            raise ToolDenied(f"Tool {spec.name!r} is not permitted by policy.{hint}")
        if spec.account_param and spec.account_param in args:
            alias = str(args[spec.account_param] or "").lower()
            if not self.account_allowed(alias):
                raise ToolDenied(
                    f"Account {alias!r} is outside this run's scope. Allowed: {', '.join(self.accounts or [])}"
                )
            if spec.mutating and self.allow_writes and not self.allow_public_writes:
                client = ctx.manager.client(alias)
                from ..agents import is_public_network

                if is_public_network(client.config.pds_url):
                    raise ToolDenied(
                        f"Writes to {alias!r} are blocked: it is on the public Bluesky "
                        "network and allow_public_writes is off."
                    )
        if spec.mutating and self.allow_writes and ctx.writes_done >= self.max_writes:
            raise ToolDenied(f"Write budget exhausted ({self.max_writes}). Finish with a summary.")

    def write_mode(self, ctx) -> str:
        """How a mutating call is handled: execute | dry_run | approval."""
        if not self.allow_writes or getattr(ctx, "dry_run", False):
            return "dry_run"
        if self.approval == "writes" and not getattr(ctx, "approved", False):
            return "approval"
        return "execute"

    def may_execute_writes(self, ctx) -> bool:
        return self.write_mode(ctx) == "execute"

    def count_write(self, ctx) -> None:
        ctx.writes_done += 1

    def describe(self) -> str:
        if self.accounts is None:
            scope = "all configured accounts"
        else:
            scope = ", ".join(self.accounts) or "no accounts (the request named none that this deployment allows)"
        mode = (
            f"writes ENABLED (max {self.max_writes} per run)"
            if self.allow_writes
            else "writes DISABLED: mutating tools return a dry-run preview"
        )
        if self.allow_writes and self.approval == "writes":
            mode += "; each write is queued for human approval rather than sent"
        delete = "deletion allowed" if self.allow_delete else "deletion not allowed"
        return f"Accounts in scope: {scope}. {mode}; {delete}."
