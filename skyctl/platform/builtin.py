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
skyctl.platform.builtin
=======================
The built-in platforms: local, compose, aws, azure, gcp, kubernetes.

Every provider-specific fact the runtime relies on (detection markers,
compute profiles, limits, log-correlation fields, trace headers, toolsets)
is in this one file, so reviewing "what do we assume about Azure?" means
reading one class. Limits reflect provider documentation at the time of
writing; re-check them when upgrading (docs/CLOUD_FRAMEWORK.md §11).
"""

from __future__ import annotations

import os

from .base import Platform, Toolset, TraceContext, _header, register_platform
from .capabilities import Cap, ComputeProfile, Limits

_P = ComputeProfile

# ----------------------------------------------------------------------
# Local and compose
# ----------------------------------------------------------------------


class LocalPlatform(Platform):
    name = "local"
    display_name = "Local process"
    default_compute = "local"
    compute_profiles = {
        "local": _P(
            "local",
            "daemon",
            frozenset({Cap.LONG_RUNNING, Cap.BACKGROUND_AFTER_RESPONSE}),
            description="A developer machine or a plain VM.",
        ),
    }
    caps = frozenset({Cap.SCHEDULE_TIMEZONE})
    toolset = Toolset(
        python_packages=("skyctl",),
        credential_chain=".env / process environment",
        emulators=("moto", "Azurite", "fake-gcs-server", "Pub/Sub emulator"),
    )

    def detect(self, env):
        return "local"


class ComposePlatform(Platform):
    name = "compose"
    display_name = "Docker Compose / single VM"
    default_compute = "compose"
    compute_profiles = {
        "compose": _P(
            "compose",
            "daemon",
            frozenset({Cap.LONG_RUNNING, Cap.BACKGROUND_AFTER_RESPONSE}),
            description="Long-lived containers; schedules run in the APScheduler daemon.",
        ),
    }
    caps = frozenset({Cap.SCHEDULE_TIMEZONE, Cap.SECRETS_NATIVE_INJECTION})
    toolset = Toolset(iac=("docker compose",), cli="docker", registry="any OCI registry")


# ----------------------------------------------------------------------
# AWS
# ----------------------------------------------------------------------


class AwsPlatform(Platform):
    name = "aws"
    display_name = "Amazon Web Services"
    default_compute = "ecs"
    service_schemes = ("s3", "sqs", "aws-sm", "eventbridge")
    compute_profiles = {
        "ecs": _P(
            "ecs",
            "service",
            frozenset(
                {
                    Cap.LONG_RUNNING,
                    Cap.BACKGROUND_AFTER_RESPONSE,
                    Cap.SCHEDULED_JOBS,
                    Cap.EVENT_JOBS,
                    Cap.SECRETS_NATIVE_INJECTION,
                    Cap.HTTP_PLATFORM_AUTH,
                }
            ),
            Limits(
                job_timeout_s=None,
                request_timeout_s=4000,
                secret_bytes=65_536,
                note="Fargate tasks have no run-time cap; ALB idle timeout max 4000s; "
                "cold start of a RunTask is typically 30-60s.",
            ),
            "ECS on Fargate: a service for HTTP, RunTask (EventBridge Scheduler / Pipes) for jobs.",
        ),
        "lambda": _P(
            "lambda",
            "function",
            frozenset({Cap.SCALE_TO_ZERO, Cap.SCHEDULED_JOBS, Cap.EVENT_JOBS, Cap.HTTP_PLATFORM_AUTH}),
            Limits(
                job_timeout_s=900,
                request_timeout_s=900,
                secret_bytes=65_536,
                note="15-minute cap; the execution environment is frozen after the response, "
                "so no background work; secrets are not injected natively (fetch at init).",
            ),
            "Lambda from the same container image (awslambdaric, or Lambda Web Adapter for HTTP).",
        ),
    }
    caps = frozenset({Cap.WORKLOAD_IDENTITY, Cap.SCHEDULE_TIMEZONE, Cap.SCHEDULE_ONE_SHOT, Cap.STATE_ATOMIC_CAS})
    toolset = Toolset(
        python_packages=("boto3", "s3fs (fsspec fallback only)"),
        credential_chain="botocore default chain: env → web identity (IRSA) → ECS/EKS container "
        "credentials → instance profile",
        iac=("Terraform/OpenTofu hashicorp/aws", "AWS CDK", "CloudFormation"),
        cli="aws",
        emulators=("moto (in-process; API shape, not concurrency)", "LocalStack"),
        ci_auth="aws-actions/configure-aws-credentials (GitHub OIDC → IAM role)",
        observability=("CloudWatch Logs", "Embedded Metric Format", "ADOT collector → X-Ray"),
        registry="ECR",
        extra="Lambda Web Adapter lets `skyctl serve` run unchanged on Lambda.",
    )

    def detect(self, env):
        if env.get("AWS_LAMBDA_FUNCTION_NAME"):
            return "lambda"
        if env.get("ECS_CONTAINER_METADATA_URI_V4") or env.get("ECS_CONTAINER_METADATA_URI"):
            return "ecs"
        return None

    def trace_from_headers(self, headers):
        # X-Amzn-Trace-Id: Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;Sampled=1
        raw = _header(headers, "x-amzn-trace-id") or os.getenv("_X_AMZN_TRACE_ID", "")
        if raw:
            parts = dict(p.split("=", 1) for p in raw.split(";") if "=" in p)
            if parts.get("Root"):
                return TraceContext(parts["Root"], parts.get("Parent"), parts.get("Sampled") == "1")
        return super().trace_from_headers(headers)

    def log_fields(self, level, trace):
        fields = {"level": level}
        if trace.trace_id:
            fields["xray_trace_id"] = trace.trace_id
        return fields

    def identity(self, env):
        return {
            "region": env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION"),
            "mechanism": (
                "irsa"
                if env.get("AWS_WEB_IDENTITY_TOKEN_FILE")
                else "container-credentials"
                if env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI") or env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
                else "lambda-role"
                if env.get("AWS_LAMBDA_FUNCTION_NAME")
                else "default-chain"
            ),
        }


# ----------------------------------------------------------------------
# Azure
# ----------------------------------------------------------------------


class AzurePlatform(Platform):
    name = "azure"
    display_name = "Microsoft Azure"
    default_compute = "container-apps"
    service_schemes = ("az", "azqueue", "azure-kv")
    compute_profiles = {
        "container-apps": _P(
            "container-apps",
            "service",
            frozenset({Cap.SCALE_TO_ZERO, Cap.LONG_RUNNING, Cap.SECRETS_NATIVE_INJECTION, Cap.HTTP_PLATFORM_AUTH}),
            Limits(
                request_timeout_s=240,
                secret_bytes=25_600,
                note="Ingress request timeout is short (minutes): long work must be async. "
                "Replicas can be scaled in after responding, so background work is not guaranteed.",
            ),
            "Container Apps app with HTTP ingress; Easy Auth can front it with Entra ID.",
        ),
        "container-apps-job": _P(
            "container-apps-job",
            "job",
            frozenset({Cap.SCHEDULED_JOBS, Cap.EVENT_JOBS, Cap.SECRETS_NATIVE_INJECTION}),
            Limits(
                job_timeout_s=None,
                secret_bytes=25_600,
                note="replicaTimeout is configurable; schedule triggers evaluate cron in UTC. skyctl drains "
                "Storage Queues with a scheduled job (KEDA's azure-queue scaler here would need shared keys).",
            ),
            "Container Apps Job: manual, schedule or event (KEDA) trigger.",
        ),
    }
    caps = frozenset({Cap.WORKLOAD_IDENTITY, Cap.SCHEDULE_ONE_SHOT, Cap.STATE_ATOMIC_CAS})
    toolset = Toolset(
        python_packages=(
            "azure-identity",
            "azure-storage-blob",
            "azure-storage-queue",
            "azure-keyvault-secrets",
            "azure-monitor-opentelemetry",
        ),
        credential_chain="DefaultAzureCredential: env → workload identity (AKS) → managed identity "
        "(AZURE_CLIENT_ID picks a user-assigned one) → Azure CLI",
        iac=("Terraform/OpenTofu hashicorp/azurerm (+ azure/azapi)", "Bicep", "azd"),
        cli="az",
        emulators=("Azurite (Blob and Queue)",),
        ci_auth="azure/login (GitHub OIDC → federated credential on an app registration)",
        observability=("Log Analytics (ContainerAppConsoleLogs)", "Application Insights via OTel distro"),
        registry="ACR",
    )

    def detect(self, env):
        if env.get("CONTAINER_APP_JOB_NAME"):
            return "container-apps-job"
        if env.get("CONTAINER_APP_NAME"):
            return "container-apps"
        return None

    def log_fields(self, level, trace):
        fields = {"level": level}
        if trace.trace_id:
            fields["operation_Id"] = trace.trace_id  # Application Insights correlation
            if trace.span_id:
                fields["operation_ParentId"] = trace.span_id
        return fields

    def identity(self, env):
        return {
            "mechanism": "workload-identity"
            if env.get("AZURE_FEDERATED_TOKEN_FILE")
            else "user-assigned-managed-identity"
            if env.get("AZURE_CLIENT_ID")
            else "system-managed-identity-or-cli",
            "client_id": env.get("AZURE_CLIENT_ID"),
        }


# ----------------------------------------------------------------------
# Google Cloud
# ----------------------------------------------------------------------


class GcpPlatform(Platform):
    name = "gcp"
    display_name = "Google Cloud"
    default_compute = "cloud-run"
    service_schemes = ("gs", "pubsub", "gcp-sm", "cloudtasks")
    compute_profiles = {
        "cloud-run": _P(
            "cloud-run",
            "service",
            frozenset({Cap.SCALE_TO_ZERO, Cap.SECRETS_NATIVE_INJECTION, Cap.HTTP_PLATFORM_AUTH}),
            Limits(
                request_timeout_s=3600,
                secret_bytes=65_536,
                note="With request-based billing CPU is throttled outside requests: no background work.",
            ),
            "Cloud Run service, request-based billing (the default).",
        ),
        "cloud-run-instance": _P(
            "cloud-run-instance",
            "service",
            frozenset(
                {
                    Cap.SCALE_TO_ZERO,
                    Cap.SECRETS_NATIVE_INJECTION,
                    Cap.HTTP_PLATFORM_AUTH,
                    Cap.BACKGROUND_AFTER_RESPONSE,
                    Cap.LONG_RUNNING,
                }
            ),
            Limits(
                request_timeout_s=3600,
                secret_bytes=65_536,
                note="Instance-based billing (CPU always allocated): background work is allowed.",
            ),
            "Cloud Run service with instance-based billing.",
        ),
        "cloud-run-job": _P(
            "cloud-run-job",
            "job",
            frozenset({Cap.SCHEDULED_JOBS, Cap.SECRETS_NATIVE_INJECTION}),
            Limits(
                job_timeout_s=86_400,
                secret_bytes=65_536,
                note="Triggered by Cloud Scheduler (cron, time zones). Per-message work goes to the "
                "service via Pub/Sub push or Cloud Tasks instead.",
            ),
            "Cloud Run job.",
        ),
    }
    caps = frozenset({Cap.WORKLOAD_IDENTITY, Cap.SCHEDULE_TIMEZONE, Cap.SCHEDULE_ONE_SHOT, Cap.STATE_ATOMIC_CAS})
    toolset = Toolset(
        python_packages=(
            "google-cloud-storage",
            "google-cloud-secret-manager",
            "google-cloud-pubsub",
            "google-cloud-tasks",
            "google-auth",
        ),
        credential_chain="google.auth.default(): GOOGLE_APPLICATION_CREDENTIALS → gcloud ADC → "
        "metadata server (Cloud Run / GKE Workload Identity)",
        iac=("Terraform/OpenTofu hashicorp/google (+ google-beta)", "Infrastructure Manager", "Config Connector"),
        cli="gcloud",
        emulators=("fake-gcs-server", "Pub/Sub emulator", "Firestore emulator"),
        ci_auth="google-github-actions/auth (Workload Identity Federation)",
        observability=("Cloud Logging structured JSON", "Cloud Trace (OTLP)", "Managed Prometheus"),
        registry="Artifact Registry",
    )

    def detect(self, env):
        if env.get("CLOUD_RUN_JOB"):
            return "cloud-run-job"
        if env.get("K_SERVICE"):
            return "cloud-run"
        return None

    def trace_from_headers(self, headers):
        # X-Cloud-Trace-Context: TRACE_ID/SPAN_ID;o=1   (span id is decimal)
        raw = _header(headers, "x-cloud-trace-context")
        if raw:
            trace_id, _, rest = raw.partition("/")
            span, _, opts = rest.partition(";")
            return TraceContext(trace_id or None, span or None, opts == "o=1")
        return super().trace_from_headers(headers)

    def log_fields(self, level, trace):
        severity = {
            "debug": "DEBUG",
            "info": "INFO",
            "warning": "WARNING",
            "error": "ERROR",
            "critical": "CRITICAL",
        }.get(level, "DEFAULT")
        fields = {"severity": severity}
        project = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
        if trace.trace_id and project:
            fields["logging.googleapis.com/trace"] = f"projects/{project}/traces/{trace.trace_id}"
            if trace.span_id:
                fields["logging.googleapis.com/spanId"] = trace.span_id
            if trace.sampled is not None:
                fields["logging.googleapis.com/trace_sampled"] = trace.sampled
        return fields

    def identity(self, env):
        return {"mechanism": "service-account (metadata server)", "project": env.get("GOOGLE_CLOUD_PROJECT")}


# ----------------------------------------------------------------------
# Kubernetes (any cluster; the portable tier)
# ----------------------------------------------------------------------


class KubernetesPlatform(Platform):
    name = "kubernetes"
    display_name = "Kubernetes (EKS / AKS / GKE / any)"
    default_compute = "deployment"
    compute_profiles = {
        "deployment": _P(
            "deployment",
            "service",
            frozenset({Cap.LONG_RUNNING, Cap.BACKGROUND_AFTER_RESPONSE, Cap.SECRETS_NATIVE_INJECTION}),
            Limits(note="No platform caps; your ingress sets request timeouts."),
            "Deployment + Service (+ Ingress).",
        ),
        "cronjob": _P(
            "cronjob",
            "job",
            frozenset({Cap.SCHEDULED_JOBS, Cap.SECRETS_NATIVE_INJECTION}),
            Limits(note="CronJob with concurrencyPolicy: Forbid; timeZone is GA since 1.27."),
            "CronJob.",
        ),
        "keda": _P(
            "keda",
            "job",
            frozenset({Cap.EVENT_JOBS, Cap.SCALE_TO_ZERO, Cap.SECRETS_NATIVE_INJECTION}),
            Limits(note="KEDA ScaledJob on SQS / Service Bus / Pub/Sub."),
            "KEDA ScaledJob per queue message.",
        ),
    }
    caps = frozenset({Cap.WORKLOAD_IDENTITY, Cap.SCHEDULE_TIMEZONE, Cap.STATE_ATOMIC_CAS})
    toolset = Toolset(
        python_packages=("(none: services are chosen by URL)",),
        credential_chain="the cloud's workload identity: EKS Pod Identity / IRSA, Azure Workload "
        "Identity, GKE Workload Identity",
        iac=("plain manifests / Kustomize / Helm", "Secrets Store CSI Driver or External Secrets Operator", "KEDA"),
        cli="kubectl",
        emulators=("kind", "k3d"),
        ci_auth="the hosting cloud's OIDC action, then kubectl",
        observability=("stdout → cluster log agent", "OpenTelemetry Collector"),
        registry="any OCI registry",
    )

    def detect(self, env):
        return "deployment" if env.get("KUBERNETES_SERVICE_HOST") else None

    def identity(self, env):
        if env.get("AWS_WEB_IDENTITY_TOKEN_FILE") or env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI"):
            return {"cloud": "aws", "mechanism": "irsa-or-pod-identity", "role": env.get("AWS_ROLE_ARN")}
        if env.get("AZURE_FEDERATED_TOKEN_FILE"):
            return {"cloud": "azure", "mechanism": "workload-identity", "client_id": env.get("AZURE_CLIENT_ID")}
        return {"cloud": "unknown-or-gke", "mechanism": "metadata server / none"}


for _cls in (LocalPlatform, ComposePlatform, AwsPlatform, AzurePlatform, GcpPlatform, KubernetesPlatform):
    register_platform(_cls())
