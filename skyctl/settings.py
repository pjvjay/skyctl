"""
skyctl.settings
===============
The single configuration contract shared by every entry point and cloud.

Everything is an environment variable; URL-style values choose adapters:

    SKYCTL_CLOUD       local | aws | azure | gcp   (auto-detected if unset)
    SKYCTL_SECRETS     env | aws-sm://<name> | azure-kv://<vault>/<secret> | gcp-sm://<project>/<secret>
                       (comma-separated list allowed; earlier entries win)
    SKYCTL_STATE_URL   file://./.skyctl | s3://bucket/prefix | az://container/prefix | gs://bucket/prefix
    SKYCTL_AGENT_MODEL provider:model (see skyctl.llm / skyctl.agent.chat)
    SKYCTL_HTTP_TOKEN  bearer token protecting /mcp and /runs
    PORT               HTTP port (8080)

    SKYCTL_PLATFORM    local | compose | aws | azure | gcp | kubernetes (alias: SKYCTL_CLOUD)
    SKYCTL_COMPUTE     compute profile, e.g. ecs | lambda | container-apps[-job] | cloud-run[-job] | deployment
    SKYCTL_QUEUE_URL   memory://x | sqs://… | servicebus://<ns>/<queue> | pubsub://<project>/<topic>?subscription=…
    SKYCTL_DELAYED_URL none | queue | eventbridge://<group>?target=…&role=… | cloudtasks://<project>/<loc>/<queue>?url=…&sa=…
    SKYCTL_AUTH        inbound auth chain for HTTP, e.g. "bearer,oidc" (see skyctl.platform.auth)

Task-agent policy defaults (CLI flags override):

    SKYCTL_AGENT_ALLOW_WRITES  false
    SKYCTL_AGENT_ALLOW_DELETE  false
    SKYCTL_AGENT_MAX_WRITES    5
    SKYCTL_AGENT_MAX_TURNS     25
    SKYCTL_AGENT_MAX_SECONDS   600   (wall-clock cap per run; keep under push/lock deadlines)
    SKYCTL_AGENT_ACCOUNTS      comma list; default: every BSKY_ACCOUNTS alias

`load_settings()` resolves the secret store first and merges its map into
the environment without overriding platform-set variables, so the existing
BSKY_* loader in skyctl.config keeps working unchanged.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_STATE_URL = "file://./.skyctl"
DEFAULT_PORT = 8080


def detect_cloud() -> str:
    """The platform name for this process (see skyctl.platform.detect_platform)."""
    from .platform import detect_platform

    return detect_platform()[0].name


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


@dataclass
class AgentDefaults:
    allow_writes: bool = False
    allow_delete: bool = False
    max_writes: int = 5
    max_turns: int = 25
    max_run_seconds: int = 600              # keep below the platform's delivery deadline
    accounts: Optional[list[str]] = None    # None → every configured alias


@dataclass
class Settings:
    cloud: str = "local"
    secrets_url: str = "env"
    state_url: str = DEFAULT_STATE_URL
    agent_model: Optional[str] = None
    http_token: Optional[str] = None
    port: int = DEFAULT_PORT
    queue_url: Optional[str] = None
    delayed_url: Optional[str] = None
    env_file: str = ".env"
    agent: AgentDefaults = field(default_factory=AgentDefaults)

    @classmethod
    def from_env(cls) -> "Settings":
        accounts_raw = os.getenv("SKYCTL_AGENT_ACCOUNTS", "").strip()
        return cls(
            cloud=detect_cloud(),
            secrets_url=os.getenv("SKYCTL_SECRETS", "").strip() or "env",
            state_url=os.getenv("SKYCTL_STATE_URL", "").strip() or DEFAULT_STATE_URL,
            agent_model=os.getenv("SKYCTL_AGENT_MODEL", "").strip() or None,
            http_token=os.getenv("SKYCTL_HTTP_TOKEN", "").strip() or None,
            port=_int("PORT", DEFAULT_PORT),
            queue_url=os.getenv("SKYCTL_QUEUE_URL", "").strip() or None,
            delayed_url=os.getenv("SKYCTL_DELAYED_URL", "").strip() or None,
            env_file=os.getenv("SKYCTL_ENV_FILE", "").strip() or ".env",
            agent=AgentDefaults(
                allow_writes=_flag("SKYCTL_AGENT_ALLOW_WRITES", False),
                allow_delete=_flag("SKYCTL_AGENT_ALLOW_DELETE", False),
                max_writes=_int("SKYCTL_AGENT_MAX_WRITES", 5),
                max_turns=_int("SKYCTL_AGENT_MAX_TURNS", 25),
                max_run_seconds=_int("SKYCTL_AGENT_MAX_SECONDS", 600),
                accounts=[a.strip().lower() for a in accounts_raw.split(",") if a.strip()] or None,
            ),
        )

    @property
    def secret_urls(self) -> list[str]:
        return [u.strip() for u in self.secrets_url.split(",") if u.strip()] or ["env"]

    @property
    def secrets_are_remote(self) -> bool:
        return any(u != "env" for u in self.secret_urls)


_loaded: Optional[Settings] = None


def load_settings(force: bool = False) -> Settings:
    """
    Resolve settings and pull secrets into the environment (once per
    process). Safe to call from every entry point.
    """
    global _loaded
    if _loaded is not None and not force:
        return _loaded
    settings = Settings.from_env()
    from .adapters import secret_store_for  # local import: adapters import ports

    for url in settings.secret_urls:
        store = secret_store_for(url, env_file=settings.env_file)
        for key, value in store.load().items():
            os.environ.setdefault(key, value)
    # Secrets may carry settings too (e.g. SKYCTL_HTTP_TOKEN); re-read them.
    settings = Settings.from_env()
    _loaded = settings
    return settings
