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

from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

_T = TypeVar("_T")


def required(value: _T | None, name: str) -> _T:
    """A configured part of the spec, or a clear error (typed narrowing for renderers)."""
    if value is None:
        raise ValueError(f"[targets.{name}] is not configured in the deployment spec")
    return value


KNOWN_KINDS = {"tick", "post.send", "task.run", "task.approve", "plan.execute"}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AppSpec(_Strict):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,20}[a-z0-9]$", description="Resource prefix; lowercase, 3-22 chars.")
    image: str


class RuntimeSpec(_Strict):
    model: str = "anthropic:claude-opus-5-5"
    log_format: Literal["json", "text", "auto"] = "json"
    delayed_delivery: bool = True  # use the platform's native one-shot scheduling fast path
    search: str = Field(
        "gdelt://",
        description="Web/news search for agents: gdelt:// (no key), brave://, "
        "tavily:// (put the API key in the secrets map) or none.",
    )


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
    poll_minutes: int = Field(
        1,
        ge=1,
        le=60,
        description="Where the worker drains the queue on a schedule "
        "(Azure Container Apps + Storage Queues): how often.",
    )


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

    def payload(self) -> dict:
        """
        The Invocation a target delivers. Scheduled agent runs carry their
        cron slot so retries and overlapping executions share one write-ledger
        key (skyctl.platform.cron.previous_fire).
        """
        if self.invoke.get("kind") == "task.run":
            return {**self.invoke, "slot": {"cron": self.cron, "timezone": self.timezone}}
        return dict(self.invoke)

    @field_validator("cron")
    @classmethod
    def _cron(cls, v: str) -> str:
        from apscheduler.triggers.cron import CronTrigger

        if len(v.split()) != 5:
            raise ValueError("cron must have 5 fields (m h dom mon dow)")
        CronTrigger.from_crontab(v)
        return v


class StateSpec(_Strict):
    """
    The state bucket: one JSON document for scheduled posts (queue.json, CAS),
    plus sessions/, runs/, ledger/ and config/. Every cloud target creates it.
    """

    versioning: bool = True
    retention_days: int = Field(90, ge=1, description="runs/ transcripts and ledger/ entries expire after this.")
    protect: bool = Field(
        True,
        description="Terraform prevent_destroy on the bucket: the job document is the "
        "source of truth, so `terraform destroy` must not take it with the compute.",
    )


# ----------------------------------------------------------------------
# Targets
# ----------------------------------------------------------------------


class ClusterSpec(_Strict):
    """An existing Kubernetes cluster to run on (skyctl does not create clusters)."""

    namespace: str = "skyctl"
    keda: bool = False  # scale the worker to zero with KEDA (must be installed)
    keda_namespace: str = "keda"
    ingress_host: str | None = None
    ingress_class: str | None = None


class EksSpec(ClusterSpec):
    cluster_name: str
    identity: Literal["pod-identity", "irsa"] = "pod-identity"
    oidc_provider_arn: str | None = None  # irsa only
    oidc_issuer: str | None = None  # irsa only, e.g. oidc.eks.us-east-1.amazonaws.com/id/ABC123
    keda_operator_role_arn: str | None = None  # lets the KEDA operator assume the app role to read SQS depth


class AksSpec(ClusterSpec):
    cluster_name: str
    cluster_resource_group: str  # needs OIDC issuer + workload identity enabled


class GkeSpec(ClusterSpec):
    pass  # needs GKE Workload Identity enabled


K8S_COMPUTE = {"eks", "aks", "gke"}


class AwsTarget(_Strict):
    region: str
    compute: Literal["ecs", "lambda", "eks"] = "ecs"
    kubernetes: EksSpec | None = None
    model: str | None = None
    vpc_id: str | None = None
    subnet_ids: list[str] = []
    security_group_ids: list[str] = []
    assign_public_ip: bool = True
    certificate_arn: str | None = None  # HTTPS listener for the service

    @model_validator(mode="after")
    def _k8s(self):
        if self.compute == "eks" and self.kubernetes is None:
            raise ValueError('compute = "eks" needs a [targets.aws.kubernetes] table (cluster_name, ...)')
        return self


class AzureTarget(_Strict):
    location: str
    compute: Literal["container-apps", "aks"] = "container-apps"
    kubernetes: AksSpec | None = None
    model: str | None = None
    openai_endpoint: str | None = None  # https://<resource>.openai.azure.com
    openai_resource_id: str | None = None  # grants the identity "Cognitive Services OpenAI User"
    operators: list[str] = []  # Entra object ids allowed to run-cloud-task (bucket + queue)

    @model_validator(mode="after")
    def _k8s(self):
        if self.compute == "aks" and self.kubernetes is None:
            raise ValueError(
                'compute = "aks" needs a [targets.azure.kubernetes] table (cluster_name, cluster_resource_group)'
            )
        return self


class GcpTarget(_Strict):
    project: str
    region: str
    operators: list[str] = []  # IAM members (user:…, group:…) allowed to run-cloud-task
    compute: Literal["cloud-run", "gke"] = "cloud-run"
    kubernetes: GkeSpec | None = None
    model: str | None = None

    @model_validator(mode="after")
    def _k8s(self):
        if self.compute == "gke" and self.kubernetes is None:
            raise ValueError('compute = "gke" needs a [targets.gcp.kubernetes] table')
        return self


class KubernetesTarget(_Strict):
    """Any cluster, bring-your-own services: nothing cloud-side is provisioned."""

    namespace: str = "skyctl"
    cloud: Literal["aws", "azure", "gcp", "none"] = "none"
    identity: str | None = None  # IAM role ARN / UAMI client id / GSA email
    state_url: str  # an existing bucket: s3://, az://, gs://
    queue_url: str | None = None
    delayed_url: str | None = None
    secret_name: str = "skyctl-secrets"  # existing k8s Secret holding the BSKY_* keys
    model: str | None = None
    keda: bool = False
    ingress_host: str | None = None
    ingress_class: str | None = None
    extra_env: dict[str, str] = {}


class ComposeTarget(_Strict):
    port: int = 8080
    model: str | None = None


class Targets(_Strict):
    aws: AwsTarget | None = None
    azure: AzureTarget | None = None
    gcp: GcpTarget | None = None
    kubernetes: KubernetesTarget | None = None
    compose: ComposeTarget | None = None

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
    def _check(self) -> DeploySpec:
        names = [s.name for s in self.schedules]
        if len(names) != len(set(names)):
            raise ValueError("schedule names must be unique")
        if "tick" not in names:
            # Every deployment needs the reconciler; add it rather than fail.
            self.schedules.insert(
                0, ScheduleSpec(name="tick", cron="*/5 * * * *", invoke={"kind": "tick"}, timeout_s=300)
            )
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

    def compute_for(self, target: str) -> str:
        t = getattr(self.targets, target)
        return getattr(t, "compute", None) or {"kubernetes": "kubernetes", "compose": "compose"}[target]

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
    return json.dumps(str(v))  # a TOML basic string is JSON-compatible


def load_spec(path: str | Path) -> DeploySpec:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return DeploySpec.model_validate(data)
