"""
skyctl.deploy.spec
==================
The cloud-neutral deployment spec (skyctl.deploy.toml). It describes
*intent* — an HTTP service, a worker, schedules (as Invocations), policy
ceilings, state retention, named tasks — and each [targets.<name>] section
adds only what is genuinely provider-specific (region, network, identity).

Secrets never appear here: renderers emit a sensitive variable (Terraform)
or a reference to an existing secret (Kubernetes, Compose).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

KNOWN_KINDS = {"tick", "post.send", "task.run", "task.approve"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppSpec(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,20}[a-z0-9]$", description="Resource prefix; lowercase, 3-22 chars.")
    image: str


class RuntimeSpec(_Strict):
    model: str = "anthropic:claude-opus-5-5"
    log_format: Literal["json", "text", "auto"] = "json"
    delayed_delivery: bool = True        # use the platform's native one-shot scheduling fast path


class PolicySpec(_Strict):
    """Deployment-wide ceilings for the task agent (identical on every target)."""
    allow_writes: bool = False
    allow_delete: bool = False
    max_writes: int = Field(5, ge=0)
    max_turns: int = Field(25, ge=1)
    max_run_seconds: int = Field(540, ge=30)
    accounts: list[str] = []


class ServiceSpec(_Strict):
    enabled: bool = False
    public: bool = False
    min_instances: int = Field(0, ge=0)
    max_instances: int = Field(2, ge=1)
    cpu: float = 0.5
    memory_mb: int = 1024
    auth: list[str] = ["bearer"]
    mcp: bool = True


class WorkerSpec(_Strict):
    enabled: bool = True
    max_concurrency: int = Field(2, ge=1)
    visibility_seconds: int = Field(900, ge=60)
    max_attempts: int = Field(5, ge=1)


class ScheduleSpec(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    cron: str
    timezone: str = "UTC"
    invoke: dict
    timeout_s: int = Field(600, ge=30)

    @field_validator("invoke")
    @classmethod
    def _kind(cls, v: dict) -> dict:
        if v.get("kind") not in KNOWN_KINDS:
            raise ValueError(f"invoke.kind must be one of {sorted(KNOWN_KINDS)}")
        return v

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        from apscheduler.triggers.cron import CronTrigger

        if len(v.split()) != 5:
            raise ValueError("cron must have 5 fields (m h dom mon dow)")
        CronTrigger.from_crontab(v)
        return v


class StateSpec(_Strict):
    versioning: bool = True
    retention_days: int = Field(90, ge=1, description="Run transcripts (runs/) expire after this.")


# ----------------------------------------------------------------------
# Targets
# ----------------------------------------------------------------------

class AwsTarget(_Strict):
    region: str
    compute: Literal["ecs", "lambda"] = "ecs"
    model: Optional[str] = None
    vpc_id: Optional[str] = None
    subnet_ids: list[str] = []
    security_group_ids: list[str] = []
    assign_public_ip: bool = True
    certificate_arn: Optional[str] = None       # HTTPS listener for the service


class AzureTarget(_Strict):
    location: str
    model: Optional[str] = None
    openai_endpoint: Optional[str] = None       # https://<resource>.openai.azure.com
    openai_resource_id: Optional[str] = None    # grants the identity "Cognitive Services OpenAI User"


class GcpTarget(_Strict):
    project: str
    region: str
    model: Optional[str] = None


class KubernetesTarget(_Strict):
    namespace: str = "skyctl"
    cloud: Literal["aws", "azure", "gcp", "none"] = "none"
    identity: Optional[str] = None              # IAM role ARN / UAMI client id / GSA email
    state_url: str                              # an existing bucket: s3://, az://, gs://
    queue_url: Optional[str] = None
    delayed_url: Optional[str] = None
    secret_name: str = "skyctl-secrets"         # existing k8s Secret holding the BSKY_* keys
    model: Optional[str] = None
    keda: bool = False
    ingress_host: Optional[str] = None
    ingress_class: Optional[str] = None
    extra_env: dict[str, str] = {}


class ComposeTarget(_Strict):
    port: int = 8080
    model: Optional[str] = None


class Targets(_Strict):
    aws: Optional[AwsTarget] = None
    azure: Optional[AzureTarget] = None
    gcp: Optional[GcpTarget] = None
    kubernetes: Optional[KubernetesTarget] = None
    compose: Optional[ComposeTarget] = None

    def names(self) -> list[str]:
        return [n for n in ("aws", "azure", "gcp", "kubernetes", "compose") if getattr(self, n) is not None]


class DeploySpec(_Strict):
    app: AppSpec
    runtime: RuntimeSpec = RuntimeSpec()
    policy: PolicySpec = PolicySpec()
    service: ServiceSpec = ServiceSpec()
    worker: WorkerSpec = WorkerSpec()
    schedules: list[ScheduleSpec] = []
    state: StateSpec = StateSpec()
    tasks: dict[str, dict] = {}
    targets: Targets = Targets()

    @model_validator(mode="after")
    def _check(self) -> "DeploySpec":
        names = [s.name for s in self.schedules]
        if len(names) != len(set(names)):
            raise ValueError("schedule names must be unique")
        if "tick" not in names:
            # Every deployment needs the reconciler; add it rather than fail.
            self.schedules.insert(0, ScheduleSpec(name="tick", cron="*/5 * * * *", invoke={"kind": "tick"}, timeout_s=300))
        from ..agent.task import TaskConfigError, TaskSpec

        for name, raw in self.tasks.items():
            try:
                TaskSpec.from_dict({"name": name, **raw})
            except (TaskConfigError, TypeError) as e:
                raise ValueError(f"task {name!r}: {e}") from e
        for s in self.schedules:
            task = s.invoke.get("task") if s.invoke.get("kind") == "task.run" else None
            if isinstance(task, str) and task not in self.tasks:
                raise ValueError(f"schedule {s.name!r} runs unknown task {task!r}")
        return self

    # -- helpers for renderers ------------------------------------------

    def model_for(self, target: str) -> str:
        t = getattr(self.targets, target, None)
        return (getattr(t, "model", None) if t is not None else None) or self.runtime.model

    def tasks_toml(self) -> str:
        """The named tasks as tasks.toml text (uploaded to <state>/config/tasks.toml)."""
        lines = ["# Rendered by skyctl deploy from the deployment spec. Do not edit by hand.", ""]
        for name, raw in self.tasks.items():
            lines.append(f"[tasks.{name}]")
            nested = {}
            for key, value in raw.items():
                if isinstance(value, dict):
                    nested[key] = value
                else:
                    lines.append(f"{key} = {_toml_value(value)}")
            for key, table in nested.items():
                lines.append(f"[tasks.{name}.{key}]")
                lines += [f"{k} = {_toml_value(v)}" for k, v in table.items()]
            lines.append("")
        return "\n".join(lines)


def _toml_value(v) -> str:
    import json

    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps(str(v))            # a TOML basic string is JSON-compatible


def load_spec(path: Union[str, Path]) -> DeploySpec:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return DeploySpec.model_validate(data)
