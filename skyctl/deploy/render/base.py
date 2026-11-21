"""
skyctl.deploy.render.base
=========================
What every renderer shares:

* Artifact: rendered files *plus* structured facts about them (env contract,
  workloads, schedules, public endpoints), so conformance is checked on
  facts, not by parsing HCL.
* common_env(): the ONE place the env-var contract is built. Renderers may
  add provider-specific variables but cannot drop or alter the common ones.
* TfDoc: a minimal Terraform JSON builder (JSON is native Terraform syntax,
  so no HCL generator dependency), and check_references(), which catches
  dangling references without running `terraform validate`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..spec import DeploySpec


@dataclass
class Finding:
    level: str          # "error" | "warning" | "info"
    target: str
    message: str
    remedy: str = ""

    def __str__(self) -> str:
        return f"[{self.level}] {self.target}: {self.message}" + (f" → {self.remedy}" if self.remedy else "")


@dataclass
class Workload:
    name: str
    kind: str                   # service | worker | job
    compute: str                # compute profile it runs under
    command: list[str]


@dataclass
class RenderedSchedule:
    name: str
    expression: str             # in the target's own dialect
    timezone: str
    invoke: dict
    overlap_guard: str          # "platform" (overlap impossible) | "app" (claim protocol + lease)


@dataclass
class Artifact:
    target: str
    files: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    workloads: list[Workload] = field(default_factory=list)
    schedules: list[RenderedSchedule] = field(default_factory=list)
    public_endpoints: list[str] = field(default_factory=list)
    terraform: Optional[dict] = None
    notes: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# The env contract
# ----------------------------------------------------------------------

COMMON_KEYS = (
    "SKYCTL_PLATFORM", "SKYCTL_COMPUTE", "SKYCTL_SECRETS", "SKYCTL_STATE_URL", "SKYCTL_QUEUE_URL",
    "SKYCTL_DELAYED_URL", "SKYCTL_AGENT_MODEL", "SKYCTL_AGENT_ALLOW_WRITES", "SKYCTL_AGENT_ALLOW_DELETE",
    "SKYCTL_AGENT_MAX_WRITES", "SKYCTL_AGENT_MAX_TURNS", "SKYCTL_AGENT_MAX_SECONDS", "SKYCTL_AGENT_ACCOUNTS",
    "SKYCTL_LOG_FORMAT", "SKYCTL_AUTH", "SKYCTL_HTTP_MCP",
)
POLICY_KEYS = tuple(k for k in COMMON_KEYS if k.startswith("SKYCTL_AGENT_") and k != "SKYCTL_AGENT_MODEL")


def common_env(
    spec: DeploySpec,
    target: str,
    *,
    platform: str,
    compute: str,
    secrets: str,
    state_url: str,
    queue_url: str = "none",
    delayed_url: str = "none",
    auth: Optional[list[str]] = None,
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    p = spec.policy
    env = {
        "SKYCTL_PLATFORM": platform,
        "SKYCTL_COMPUTE": compute,
        "SKYCTL_SECRETS": secrets,
        "SKYCTL_STATE_URL": state_url,
        "SKYCTL_QUEUE_URL": queue_url,
        "SKYCTL_DELAYED_URL": delayed_url if spec.runtime.delayed_delivery else "none",
        "SKYCTL_AGENT_MODEL": spec.model_for(target),
        "SKYCTL_AGENT_ALLOW_WRITES": str(p.allow_writes).lower(),
        "SKYCTL_AGENT_ALLOW_DELETE": str(p.allow_delete).lower(),
        "SKYCTL_AGENT_MAX_WRITES": str(p.max_writes),
        "SKYCTL_AGENT_MAX_TURNS": str(p.max_turns),
        "SKYCTL_AGENT_MAX_SECONDS": str(p.max_run_seconds),
        "SKYCTL_AGENT_ACCOUNTS": ",".join(p.accounts),
        "SKYCTL_LOG_FORMAT": spec.runtime.log_format,
        "SKYCTL_AUTH": ",".join(auth if auth is not None else spec.service.auth),
        "SKYCTL_HTTP_MCP": str(spec.service.mcp).lower(),
    }
    for key, value in (extra or {}).items():
        if key in env:
            raise ValueError(f"Renderer for {target} tried to override common env key {key}")
        env[key] = value
    return env


def invoke_args(invoke: dict) -> list[str]:
    """The container command for a schedule: the same on every platform."""
    return ["invoke", json.dumps(invoke, separators=(",", ":"), sort_keys=True)]


# ----------------------------------------------------------------------
# Terraform JSON
# ----------------------------------------------------------------------

def ref(address: str, attr: str) -> str:
    return "${" + address + "." + attr + "}"


class TfDoc:
    def __init__(self, required_providers: dict[str, dict], providers: dict[str, dict]):
        self.doc: dict[str, Any] = {
            "terraform": {"required_version": ">= 1.6", "required_providers": required_providers},
            "provider": providers,
        }

    def _section(self, name: str) -> dict:
        return self.doc.setdefault(name, {})

    def variable(self, name: str, type_: str = "string", *, sensitive: bool = False, default: Any = None,
                 description: str = "") -> str:
        body: dict[str, Any] = {"type": type_}
        if description:
            body["description"] = description
        if sensitive:
            body["sensitive"] = True
        if default is not None:
            body["default"] = default
        self._section("variable")[name] = body
        return "${var." + name + "}"

    def resource(self, type_: str, name: str, body: dict) -> str:
        section = self._section("resource").setdefault(type_, {})
        if name in section:
            raise ValueError(f"duplicate resource {type_}.{name}")
        section[name] = body
        return f"{type_}.{name}"

    def data(self, type_: str, name: str, body: Optional[dict] = None) -> str:
        self._section("data").setdefault(type_, {})[name] = body or {}
        return f"data.{type_}.{name}"

    def output(self, name: str, value: Any, *, sensitive: bool = False) -> None:
        body = {"value": value}
        if sensitive:
            body["sensitive"] = True
        self._section("output")[name] = body

    def to_json(self) -> str:
        return json.dumps(self.doc, indent=2, sort_keys=False) + "\n"


_INTERP = re.compile(r"\$\{([^}]*)\}")
_SIMPLE = re.compile(r"^(var\.\w+|local\.\w+|data\.\w+\.\w+|\w+\.\w+)((\.\w+)|(\[\d+\]))*$")


def check_references(doc: dict) -> list[str]:
    """
    Every ${…} must be a simple reference to something declared in the
    document. Renderers keep interpolations simple on purpose, so this
    check is complete for what they emit.
    """
    declared = set()
    for type_, items in (doc.get("resource") or {}).items():
        declared |= {f"{type_}.{n}" for n in items}
    for type_, items in (doc.get("data") or {}).items():
        declared |= {f"data.{type_}.{n}" for n in items}
    declared |= {f"var.{n}" for n in (doc.get("variable") or {})}
    declared |= {f"local.{n}" for n in (doc.get("locals") or {})}
    problems = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")
        elif isinstance(node, str):
            for expr in _INTERP.findall(node):
                expr = expr.strip()
                m = _SIMPLE.match(expr)
                if not m:
                    problems.append(f"{path}: complex interpolation ${{{expr}}}")
                    continue
                root = m.group(1)
                parts = expr.replace("[", ".[").split(".")
                candidates = {root, ".".join(parts[:2]), ".".join(parts[:3])}
                if not candidates & declared:
                    problems.append(f"{path}: reference to undeclared {root}")

    walk(doc, "$")
    return problems


def iam_json(statements: list[dict]) -> str:
    """An AWS IAM policy document as a JSON string (Terraform interpolates the ${…} inside)."""
    return json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))
