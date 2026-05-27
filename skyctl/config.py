"""
skyctl.config
=============
Loads multi-account credentials from environment variables.

Expected format in your .env file:

    BSKY_ACCOUNTS=personal,work,news
    BSKY_PERSONAL_HANDLE=omnidion.bsky.social
    BSKY_PERSONAL_PASSWORD=xxxx-xxxx-xxxx-xxxx
    BSKY_WORK_HANDLE=mywork.bsky.social
    BSKY_WORK_PASSWORD=xxxx-xxxx-xxxx-xxxx

Aliases are case-insensitive; env vars are always uppercased.
"""

import os
from dotenv import load_dotenv
from .models import AccountConfig


def load_accounts(env_file: str = ".env") -> dict[str, AccountConfig]:
    """
    Parse all BSKY_* env vars and return a dict of {alias: AccountConfig}.
    Raises ValueError if BSKY_ACCOUNTS is missing or any account is incomplete.
    """
    load_dotenv(env_file, override=False)

    raw = os.getenv("BSKY_ACCOUNTS", "")
    if not raw.strip():
        raise ValueError(
            "BSKY_ACCOUNTS environment variable is not set. "
            "Add it to your .env file, e.g.:\n  BSKY_ACCOUNTS=personal,work"
        )

    aliases = [a.strip().lower() for a in raw.split(",") if a.strip()]
    accounts: dict[str, AccountConfig] = {}

    for alias in aliases:
        prefix = f"BSKY_{alias.upper()}_"
        handle = os.getenv(f"{prefix}HANDLE", "").strip()
        password = os.getenv(f"{prefix}PASSWORD", "").strip()

        if not handle or not password:
            raise ValueError(
                f"Account '{alias}' is incomplete. "
                f"Expected env vars: {prefix}HANDLE and {prefix}PASSWORD"
            )

        accounts[alias] = AccountConfig(alias=alias, handle=handle, password=password)

    return accounts


def load_single_account(alias: str, env_file: str = ".env") -> AccountConfig:
    """Load just one account by alias. Raises KeyError if not found."""
    accounts = load_accounts(env_file)
    alias = alias.lower()
    if alias not in accounts:
        raise KeyError(
            f"Account '{alias}' not found. "
            f"Available accounts: {', '.join(accounts)}"
        )
    return accounts[alias]
