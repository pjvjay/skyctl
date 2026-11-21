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
skyctl.deploy.validate
======================
Check a DeploySpec against each target's *capabilities and limits* before
anything is rendered. This is where "generalise without the lowest common
denominator" is enforced: a spec may use any feature, and the targets that
cannot honour it say so precisely, with a remedy.
"""

from __future__ import annotations

import re

from ..platform import Cap, get_platform
from ..platform.cron import CronError, CronSpec, to_eventbridge
from .render.base import Finding
from .spec import DeploySpec, required

# Which compute profile runs scheduled work, by (target, compute).
SCHEDULE_PROFILE = {
    ("aws", "ecs"): ("aws", "ecs"),
    ("aws", "lambda"): ("aws", "lambda"),
    ("aws", "eks"): ("kubernetes", "cronjob"),
    ("azure", "container-apps"): ("azure", "container-apps-job"),
    ("azure", "aks"): ("kubernetes", "cronjob"),
    ("gcp", "cloud-run"): ("gcp", "cloud-run-job"),
    ("gcp", "gke"): ("kubernetes", "cronjob"),
    ("kubernetes", "kubernetes"): ("kubernetes", "cronjob"),
    ("compose", "compose"): ("compose", "compose"),
}
# The longest a queued/pushed invocation may run before the platform redelivers it
# (and the worker heartbeat cannot extend it).
DELIVERY_DEADLINE = {
    ("gcp", "cloud-run"): (600, "Pub/Sub push ack deadline"),
    ("aws", "lambda"): (900, "Lambda timeout"),
}
_ECR = re.compile(r"^\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(\.cn)?/")


def _schedule_profile(target: str, spec: DeploySpec):
    name, compute = SCHEDULE_PROFILE[(target, spec.compute_for(target))]
    platform = get_platform(name)
    return platform, platform.profile(compute)


def validate(spec: DeploySpec, target: str) -> list[Finding]:
    out: list[Finding] = []
    cfg = getattr(spec.targets, target)
    if cfg is None:
        return [Finding("error", target, "target is not configured in [targets]")]
    platform, profile = _schedule_profile(target, spec)
    caps = platform.capabilities(profile.name)

    def add(level, message, remedy=""):
        out.append(Finding(level, target, message, remedy))

    # -- schedules -----------------------------------------------------------
    if Cap.SCHEDULED_JOBS not in caps:
        add("info", "no platform scheduler; schedules run inside `skyctl daemon` (capability substitution)")
    for s in spec.schedules:
        cron = CronSpec(s.cron, s.timezone)
        if not cron.is_utc and Cap.SCHEDULE_TIMEZONE not in caps:
            add(
                "error",
                f"schedule {s.name!r} uses time zone {s.timezone!r}, but {platform.name}/{profile.name} "
                "evaluates cron in UTC only",
                "Express it in UTC (and accept DST drift), or run it on a target with time-zone support.",
            )
        limit = profile.limits.job_timeout_s
        if limit is not None and s.timeout_s > limit:
            add(
                "error",
                f"schedule {s.name!r} needs {s.timeout_s}s but {profile.name} jobs stop at {limit}s",
                'Lower timeout_s or use a compute profile without the cap (e.g. aws compute = "ecs").',
            )
        if cron.min_interval_seconds() < profile.limits.min_schedule_interval_s:
            add("error", f"schedule {s.name!r} fires more often than {profile.limits.min_schedule_interval_s}s")
        if target == "aws":
            try:
                to_eventbridge(cron)
            except CronError as e:
                add("error", f"schedule {s.name!r}: {e}")

    # -- delivery deadlines vs run time -----------------------------------
    key = (target, spec.compute_for(target))
    if key in DELIVERY_DEADLINE:
        deadline, what = DELIVERY_DEADLINE[key]
        if spec.policy.max_run_seconds > deadline - 30:
            add(
                "error",
                f"policy.max_run_seconds={spec.policy.max_run_seconds} exceeds the {what} ({deadline}s) "
                "less a safety margin; a long run would be redelivered and start again in parallel",
                f"Set policy.max_run_seconds ≤ {deadline - 30}.",
            )
    compute = spec.compute_for(target)
    if target == "azure":
        add("info", "one-time posts more than 7 days ahead wait for the tick catch-up (Storage Queue delay limit)")
        if compute == "container-apps" and spec.worker.enabled:
            add(
                "info",
                f"the worker drains the Storage Queue every {spec.worker.poll_minutes} min; "
                f"queued agent runs start within that time",
            )
    if compute == "gke" and spec.runtime.delayed_delivery:
        add(
            "info",
            "no native delayed delivery on GKE (Cloud Tasks needs a reachable endpoint); "
            "one-time posts are sent by the tick catch-up",
        )
    if compute in ("eks", "aks", "gke"):
        cluster = getattr(spec.targets, target).kubernetes
        if spec.service.enabled and spec.service.public and not cluster.ingress_host:
            add("warning", "service.public on Kubernetes without kubernetes.ingress_host: no Ingress is rendered")
        if compute == "eks":
            if cluster.identity == "irsa" and not (cluster.oidc_provider_arn and cluster.oidc_issuer):
                add(
                    "error",
                    'identity = "irsa" needs oidc_provider_arn and oidc_issuer',
                    'Or use identity = "pod-identity" (needs the EKS Pod Identity Agent add-on).',
                )
            if cluster.keda and not cluster.keda_operator_role_arn:
                add(
                    "warning",
                    "keda = true without keda_operator_role_arn: the KEDA operator must be able to "
                    "assume the app role to read queue depth",
                )
        if compute == "aks":
            add("info", "AKS needs the OIDC issuer and workload identity enabled on the cluster")
        if compute == "gke":
            add("info", "GKE needs Workload Identity enabled on the cluster and node pools")

    # -- HTTP service --------------------------------------------------------
    svc = spec.service
    if svc.enabled and target == "aws" and required(spec.targets.aws, "aws").compute == "lambda":
        add(
            "error",
            'the HTTP service is not rendered for compute = "lambda"',
            'Use compute = "ecs", or run `skyctl serve` behind the Lambda Web Adapter yourself.',
        )
    if (
        svc.enabled
        and svc.public
        and svc.auth == ["platform"]
        and (target, spec.compute_for(target)) != ("gcp", "cloud-run")
    ):
        add(
            "error",
            'public service with auth = ["platform"], but this renderer configures no platform '
            "authentication for that target: anyone could call it",
            'Add "bearer" or an OIDC method to service.auth.',
        )
    if svc.enabled and target == "aws" and required(spec.targets.aws, "aws").compute == "ecs":
        aws = required(spec.targets.aws, "aws")
        missing = [n for n in ("vpc_id", "certificate_arn") if not getattr(aws, n)] + (
            [] if aws.subnet_ids else ["subnet_ids"]
        )
        if missing and aws.compute == "ecs":
            add("error", f"the HTTP service on ECS needs {', '.join(missing)} (ALB with an HTTPS listener)")
    if svc.enabled and svc.public and target == "compose":
        add("warning", "public = true binds port 0.0.0.0 on the host; put a TLS proxy in front")

    # -- target specifics ---------------------------------------------------
    if target == "aws":
        aws = required(spec.targets.aws, "aws")
        if (spec.schedules or spec.worker.enabled) and not aws.subnet_ids and aws.compute == "ecs":
            add("error", "ECS tasks need subnet_ids (and usually security_group_ids)")
        if aws.compute == "lambda" and not _ECR.match(spec.app.image):
            add(
                "error",
                f"Lambda only runs container images from ECR, not {spec.app.image!r}",
                "Push the image to ECR (replicate from GHCR in CI) and set app.image to the ECR URI.",
            )
    if (
        target == "azure"
        and spec.model_for("azure").startswith("azure-openai:")
        and not required(spec.targets.azure, "azure").openai_endpoint
    ):
        add("error", "azure-openai model without targets.azure.openai_endpoint")
    if target == "kubernetes":
        k = required(spec.targets.kubernetes, "kubernetes")
        scheme = k.state_url.split("://", 1)[0]
        if scheme not in ("s3", "az", "gs"):
            add("error", f"state_url {k.state_url!r} must be a bucket (s3://, az://, gs://) shared by all pods")
        if k.cloud != "none" and not k.identity:
            add("error", f"cloud = {k.cloud!r} needs identity (role ARN / client id / GSA email) for workload identity")
        if k.keda and not k.queue_url:
            add("error", "keda = true needs queue_url")
        if spec.worker.enabled and not k.queue_url:
            add("warning", "worker enabled without queue_url; agent runs will use in-process threads")
    if target == "compose" and spec.runtime.delayed_delivery:
        add("info", "no delayed-delivery service: one-time posts are sent by the tick reconciler")
    return out


def validate_all(spec: DeploySpec) -> list[Finding]:
    return [f for t in spec.targets.names() for f in validate(spec, t)]
