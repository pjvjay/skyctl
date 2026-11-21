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
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .registry import ToolDenied, ToolSpec


@dataclass
class ToolPolicy:
    accounts: Optional[list[str]] = None      # None → every configured alias
    allow_writes: bool = False
    allow_delete: bool = False
    max_writes: int = 5
    allow_public_writes: bool = True
    deny_tools: list[str] = field(default_factory=list)
    allow_tools: Optional[list[str]] = None   # None → everything not denied
    approval: str = "none"                    # "writes": record mutating calls for a human instead of sending

    def __post_init__(self):
        self.accounts = [a.lower() for a in self.accounts] if self.accounts is not None else None
        self.deny_tools = [t.lower() for t in self.deny_tools]
        if self.allow_tools is not None:
            self.allow_tools = [t.lower() for t in self.allow_tools]

    # -- visibility ----------------------------------------------------

    def visible(self, spec: ToolSpec) -> bool:
        """Whether a consumer should even be shown this tool."""
        if spec.name in self.deny_tools:
            return False
        if self.allow_tools is not None and spec.name not in self.allow_tools:
            return False
        if spec.destructive and not self.allow_delete:
            return False
        return True

    def account_allowed(self, alias: str) -> bool:
        return self.accounts is None or alias.lower() in self.accounts

    def filter_accounts(self, aliases: list[str]) -> list[str]:
        return [a for a in aliases if self.account_allowed(a)]

    # -- enforcement ---------------------------------------------------

    def check(self, spec: ToolSpec, args: dict, ctx) -> None:
        """Raise ToolDenied if this call must not happen."""
        if not self.visible(spec):
            raise ToolDenied(f"Tool {spec.name!r} is not permitted by policy.")
        if spec.account_param and spec.account_param in args:
            alias = str(args[spec.account_param] or "").lower()
            if not self.account_allowed(alias):
                raise ToolDenied(
                    f"Account {alias!r} is outside this run's scope. "
                    f"Allowed: {', '.join(self.accounts or [])}"
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
            raise ToolDenied(
                f"Write budget exhausted ({self.max_writes}). Finish with a summary."
            )

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
        scope = ", ".join(self.accounts) if self.accounts else "all configured accounts"
        mode = (
            f"writes ENABLED (max {self.max_writes} per run)"
            if self.allow_writes
            else "writes DISABLED: mutating tools return a dry-run preview"
        )
        if self.allow_writes and self.approval == "writes":
            mode += "; each write is queued for human approval rather than sent"
        delete = "deletion allowed" if self.allow_delete else "deletion not allowed"
        return f"Accounts in scope: {scope}. {mode}; {delete}."
