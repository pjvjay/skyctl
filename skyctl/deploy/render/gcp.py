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
skyctl.deploy.render.gcp
========================
DeploySpec → Terraform JSON for Google Cloud.

Backing services (shared by both compute modes, see gcp_backing()): the
runtime service account; Secret Manager; the state bucket (GCS, CAS via
generation preconditions, versioned, lifecycle on runs/ and ledger/,
prevent_destroy); a Pub/Sub work topic with a dead-letter topic.

    intent            compute = "cloud-run"                          compute = "gke" (existing cluster)
    ----------------  ---------------------------------------------  ---------------------------------------------
    identity          runtime SA on the service and jobs             runtime SA via GKE Workload Identity
    HTTP token        Secret Manager env ref (native injection)      fetched: …/<app>-http-token#SKYCTL_HTTP_TOKEN
    worker            the Cloud Run service: Pub/Sub PUSH → /invoke  pull subscription; Deployment or KEDA
                      (OIDC token, DLQ)                              ScaledJob (gcp-pubsub)
    schedules         Cloud Scheduler → Cloud Run Jobs (any TZ)      CronJob (Forbid, any time zone)
    delayed delivery  Cloud Tasks → POST /invoke (30-day horizon)    none: the tick catch-up (Cloud Tasks needs a
                                                                     reachable HTTPS endpoint)
    HTTP service      Cloud Run, IAM-only unless public              Deployment + Service (+ Ingress)
    model             Vertex AI ("roles/aiplatform.user")

Cloud Run notes: the service must know its own URL (Cloud Tasks target,
OIDC audience); referencing its own `uri` would be a cycle, so the
deterministic https://<name>-<project-number>.<region>.run.app is used.
Pub/Sub push ack deadline is 600 s (validate() keeps runs below it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..spec import DeploySpec, required
from .base import Artifact, RenderedSchedule, TfDoc, Workload, common_env, connection_output, invoke_args, ref
from .k8s_common import KedaTrigger, build_k8s, emit_k8s

APIS = (
    "run.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "pubsub.googleapis.com",
    "cloudtasks.googleapis.com",
    "aiplatform.googleapis.com",
    "iam.googleapis.com",
)


@dataclass
class GcpBacking:
    apis: list
    number: str
    runtime_sa: str
    runtime_member: str
    topic: str
    dlq: str
    bucket: str
    token_secret: str | None
    secrets_url: str
    queue_url: str
    subscription: str


def gcp_backing(spec: DeploySpec, tf: TfDoc, *, consumer: str) -> GcpBacking:
    """consumer = "push" (Cloud Run /invoke) or "pull" (in-cluster workers)."""
    t = required(spec.targets.gcp, "gcp")
    app = spec.app.name
    number = ref(tf.data("google_project", "this"), "number")
    apis = [
        tf.resource("google_project_service", api.split(".")[0], {"service": api, "disable_on_destroy": False})
        for api in APIS
    ]
    runtime_sa = tf.resource(
        "google_service_account", "runtime", {"account_id": f"{app}-run"[:30], "display_name": "skyctl runtime"}
    )
    runtime_member = f"serviceAccount:{ref(runtime_sa, 'email')}"

    # -- secrets ---------------------------------------------------------------
    secret = tf.resource(
        "google_secret_manager_secret",
        "config",
        {"secret_id": f"{app}-config", "replication": {"auto": {}}, "depends_on": apis},
    )
    tf.resource(
        "google_secret_manager_secret_version",
        "config",
        {"secret": ref(secret, "id"), "secret_data": "${var.secrets_json}"},
    )
    tf.resource(
        "google_secret_manager_secret_iam_member",
        "config",
        {"secret_id": ref(secret, "id"), "role": "roles/secretmanager.secretAccessor", "member": runtime_member},
    )
    token_secret = None
    if spec.service.enabled and "bearer" in spec.service.auth:
        token_secret = tf.resource(
            "google_secret_manager_secret",
            "http_token",
            {"secret_id": f"{app}-http-token", "replication": {"auto": {}}, "depends_on": apis},
        )
        tf.resource(
            "google_secret_manager_secret_version",
            "http_token",
            {"secret": ref(token_secret, "id"), "secret_data": "${var.http_token}"},
        )
        tf.resource(
            "google_secret_manager_secret_iam_member",
            "http_token",
            {
                "secret_id": ref(token_secret, "id"),
                "role": "roles/secretmanager.secretAccessor",
                "member": runtime_member,
            },
        )
    secrets_url = f"gcp-sm://{t.project}/{app}-config"
    if token_secret and consumer == "pull":
        secrets_url += f",gcp-sm://{t.project}/{app}-http-token#SKYCTL_HTTP_TOKEN"

    # -- state bucket: one JSON document (queue.json) + sessions/ runs/ ledger/ config/ ----
    bucket_body = {
        "name": f"{t.project}-{app}-state",
        "location": t.region,
        "uniform_bucket_level_access": True,
        "public_access_prevention": "enforced",
        "versioning": {"enabled": spec.state.versioning},
        "lifecycle_rule": [
            {
                "condition": {"age": spec.state.retention_days, "matches_prefix": ["skyctl/runs/", "skyctl/ledger/"]},
                "action": {"type": "Delete"},
            },
            {"condition": {"days_since_noncurrent_time": 30}, "action": {"type": "Delete"}},
        ],
    }
    if spec.state.protect:
        bucket_body["lifecycle"] = {"prevent_destroy": True}
    bucket = tf.resource("google_storage_bucket", "state", bucket_body)
    tf.resource(
        "google_storage_bucket_iam_member",
        "runtime",
        {"bucket": ref(bucket, "name"), "role": "roles/storage.objectAdmin", "member": runtime_member},
    )
    if spec.tasks:
        tf.resource(
            "google_storage_bucket_object",
            "tasks",
            {
                "name": "skyctl/config/tasks.toml",
                "bucket": ref(bucket, "name"),
                "content": spec.tasks_toml(),
                "content_type": "application/toml",
            },
        )

    # -- queue: Pub/Sub topic + dead-letter topic ------------------------------
    pubsub_agent = f"serviceAccount:service-{number}@gcp-sa-pubsub.iam.gserviceaccount.com"
    topic = tf.resource("google_pubsub_topic", "work", {"name": f"{app}-work", "depends_on": apis})
    dlq = tf.resource("google_pubsub_topic", "dlq", {"name": f"{app}-work-dlq", "depends_on": apis})
    tf.resource(
        "google_pubsub_topic_iam_member",
        "runtime_publish",
        {"topic": ref(topic, "name"), "role": "roles/pubsub.publisher", "member": runtime_member},
    )
    tf.resource(
        "google_pubsub_topic_iam_member",
        "agent_dlq",
        {"topic": ref(dlq, "name"), "role": "roles/pubsub.publisher", "member": pubsub_agent},
    )
    subscription = f"{app}-work-{consumer}"

    # Operators (people running `skyctl run-cloud-task`): state read/write + publish.
    for i, operator in enumerate(t.operators):
        tf.resource(
            "google_storage_bucket_iam_member",
            f"operator_{i}",
            {"bucket": ref(bucket, "name"), "role": "roles/storage.objectUser", "member": operator},
        )
        tf.resource(
            "google_pubsub_topic_iam_member",
            f"operator_{i}",
            {"topic": ref(topic, "name"), "role": "roles/pubsub.publisher", "member": operator},
        )

    if spec.model_for("gcp").startswith("vertex:"):
        tf.resource(
            "google_project_iam_member",
            "vertex",
            {"project": t.project, "role": "roles/aiplatform.user", "member": runtime_member},
        )

    return GcpBacking(
        apis=apis,
        number=number,
        runtime_sa=runtime_sa,
        runtime_member=runtime_member,
        topic=topic,
        dlq=dlq,
        bucket=bucket,
        token_secret=token_secret,
        secrets_url=secrets_url,
        queue_url=f"pubsub://{t.project}/{app}-work?subscription={subscription}",
        subscription=subscription,
    )


def _subscription(spec, tf: TfDoc, b: GcpBacking, push: dict | None) -> str:
    body = {
        "name": b.subscription,
        "topic": ref(b.topic, "id"),
        "ack_deadline_seconds": 600,
        "dead_letter_policy": {
            "dead_letter_topic": ref(b.dlq, "id"),
            "max_delivery_attempts": max(5, spec.worker.max_attempts),
        },
        "retry_policy": {"minimum_backoff": "10s", "maximum_backoff": "600s"},
    }
    if push:
        body["push_config"] = push
        body["depends_on"] = ["google_cloud_run_v2_service.svc"]
    sub = tf.resource("google_pubsub_subscription", "work", body)
    pubsub_agent = f"serviceAccount:service-{b.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
    # The Pub/Sub service agent must be able to ack on the subscription to dead-letter messages.
    tf.resource(
        "google_pubsub_subscription_iam_member",
        "agent_ack",
        {"subscription": ref(sub, "name"), "role": "roles/pubsub.subscriber", "member": pubsub_agent},
    )
    return sub


def render_gcp(spec: DeploySpec) -> Artifact:
    t = required(spec.targets.gcp, "gcp")
    gke = t.compute == "gke"
    art = Artifact("gcp")
    providers = {"google": {"source": "hashicorp/google", "version": ">= 5.40"}}
    if gke:
        providers["local"] = {"source": "hashicorp/local", "version": ">= 2.4"}
    tf = TfDoc(providers, {"google": {"project": t.project, "region": t.region}})
    image = tf.variable("image", default=spec.app.image)
    tf.variable("secrets_json", sensitive=True, description="The BSKY_* / API-key map as JSON or dotenv text.")
    if spec.service.enabled and "bearer" in spec.service.auth:
        tf.variable("http_token", sensitive=True)

    b = gcp_backing(spec, tf, consumer="pull" if gke else "push")
    if gke:
        _render_gke(spec, tf, art, b)
    else:
        _render_cloud_run(spec, tf, art, image, b)

    tf.output("env", art.env)
    tf.output("state_url", f"gs://{ref(b.bucket, 'name')}/skyctl")
    tf.output("skyctl_connection", connection_output("gcp", art.env, {"GOOGLE_CLOUD_PROJECT": t.project}))
    art.terraform = tf.doc
    art.files["main.tf.json"] = tf.to_json()
    return art


# ----------------------------------------------------------------------
# Cloud Run
# ----------------------------------------------------------------------


def _render_cloud_run(spec, tf: TfDoc, art: Artifact, image, b: GcpBacking):
    t = required(spec.targets.gcp, "gcp")
    app = spec.app.name
    invoker_sa = tf.resource(
        "google_service_account", "invoker", {"account_id": f"{app}-invoker"[:30], "display_name": "skyctl invoker"}
    )
    invoker_member = f"serviceAccount:{ref(invoker_sa, 'email')}"
    pubsub_agent = f"serviceAccount:service-{b.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
    tf.resource(
        "google_service_account_iam_member",
        "agent_token",
        {
            "service_account_id": ref(invoker_sa, "name"),
            "role": "roles/iam.serviceAccountTokenCreator",
            "member": pubsub_agent,
        },
    )

    svc_name = f"{app}-svc"
    url = f"https://{svc_name}-{b.number}.{t.region}.run.app"
    if spec.worker.enabled:
        _subscription(
            spec,
            tf,
            b,
            {
                "push_endpoint": f"{url}/invoke",
                "oidc_token": {"service_account_email": ref(invoker_sa, "email"), "audience": f"{url}/invoke"},
            },
        )
        art.workloads.append(Workload("worker", "worker", "cloud-run", ["POST /invoke (Pub/Sub push)"]))

    delayed_url = "none"
    if spec.runtime.delayed_delivery:
        tq = tf.resource(
            "google_cloud_tasks_queue",
            "delayed",
            {"name": f"{app}-delayed", "location": t.region, "retry_config": {"max_attempts": 5}, "depends_on": b.apis},
        )
        tf.resource(
            "google_cloud_tasks_queue_iam_member",
            "runtime_enqueue",
            {
                "name": ref(tq, "name"),
                "location": t.region,
                "role": "roles/cloudtasks.enqueuer",
                "member": b.runtime_member,
            },
        )
        tf.resource(
            "google_service_account_iam_member",
            "runtime_actas_invoker",
            {
                "service_account_id": ref(invoker_sa, "name"),
                "role": "roles/iam.serviceAccountUser",
                "member": b.runtime_member,
            },
        )
        delayed_url = (
            f"cloudtasks://{t.project}/{t.region}/{app}-delayed?url={url}/invoke&sa={ref(invoker_sa, 'email')}"
        )

    auth = ["google-oidc"] + [a for a in spec.service.auth if a != "google-oidc"]
    env = common_env(
        spec,
        "gcp",
        platform="gcp",
        compute="cloud-run",
        secrets=b.secrets_url,
        state_url=f"gs://{ref(b.bucket, 'name')}/skyctl",
        queue_url=b.queue_url,
        delayed_url=delayed_url,
        auth=auth,
        extra={
            "GOOGLE_CLOUD_PROJECT": t.project,
            "CLOUD_ML_REGION": t.region,
            "SKYCTL_OIDC_AUDIENCE": f"{url},{url}/invoke",
            "SKYCTL_OIDC_ALLOWED": ref(invoker_sa, "email"),
        },
    )
    art.env = env

    def env_blocks(compute: str, with_token: bool = False) -> list[dict]:
        items: list[dict[str, Any]] = [{"name": k, "value": v} for k, v in {**env, "SKYCTL_COMPUTE": compute}.items()]
        if with_token and b.token_secret:
            items.append(
                {
                    "name": "SKYCTL_HTTP_TOKEN",
                    "value_source": {
                        "secret_key_ref": {"secret": ref(b.token_secret, "secret_id"), "version": "latest"}
                    },
                }
            )
        return items

    if spec.service.enabled or spec.worker.enabled or spec.runtime.delayed_delivery:
        svc = spec.service
        public = svc.enabled and svc.public
        service = tf.resource(
            "google_cloud_run_v2_service",
            "svc",
            {
                "name": svc_name,
                "location": t.region,
                "deletion_protection": False,
                # Internal ingress still admits Pub/Sub push, Cloud Tasks and Cloud Scheduler from this project.
                "ingress": "INGRESS_TRAFFIC_ALL" if public else "INGRESS_TRAFFIC_INTERNAL_ONLY",
                "template": {
                    "service_account": ref(b.runtime_sa, "email"),
                    "timeout": f"{min(3600, spec.policy.max_run_seconds + 60)}s",
                    "scaling": {"min_instance_count": svc.min_instances, "max_instance_count": svc.max_instances},
                    "containers": [
                        {
                            "image": image,
                            "args": ["serve"],
                            "env": env_blocks("cloud-run", bool(b.token_secret)),
                            "ports": {"container_port": 8080},
                            "resources": {"limits": {"cpu": str(max(1, int(svc.cpu))), "memory": f"{svc.memory_mb}Mi"}},
                        }
                    ],
                },
                "depends_on": b.apis,
            },
        )
        # Invocation is IAM-gated: only the invoker account; never allUsers.
        tf.resource(
            "google_cloud_run_v2_service_iam_member",
            "invoker",
            {"name": ref(service, "name"), "location": t.region, "role": "roles/run.invoker", "member": invoker_member},
        )
        tf.output("service_url", url)
        art.workloads.append(Workload("http", "service", "cloud-run", ["serve"]))
        if public:
            art.public_endpoints.append("cloud-run:443 (IAM + app auth)")

    for s in spec.schedules:
        key = s.name.replace("-", "_")
        job = tf.resource(
            "google_cloud_run_v2_job",
            key,
            {
                "name": f"{app}-{s.name}",
                "location": t.region,
                "deletion_protection": False,
                "template": {
                    "task_count": 1,
                    "template": {
                        "service_account": ref(b.runtime_sa, "email"),
                        "timeout": f"{s.timeout_s}s",
                        "max_retries": 0,
                        "containers": [
                            {"image": image, "args": invoke_args(s.payload()), "env": env_blocks("cloud-run-job")}
                        ],
                    },
                },
                "depends_on": b.apis,
            },
        )
        tf.resource(
            "google_cloud_run_v2_job_iam_member",
            f"{key}_invoker",
            {"name": ref(job, "name"), "location": t.region, "role": "roles/run.invoker", "member": invoker_member},
        )
        tf.resource(
            "google_cloud_scheduler_job",
            key,
            {
                "name": f"{app}-{s.name}",
                "region": t.region,
                "schedule": s.cron,
                "time_zone": s.timezone,
                "attempt_deadline": "180s",
                "http_target": {
                    "http_method": "POST",
                    "uri": f"https://run.googleapis.com/v2/projects/{t.project}/locations/{t.region}/jobs/{ref(job, 'name')}:run",
                    "oauth_token": {"service_account_email": ref(invoker_sa, "email")},
                },
                "depends_on": b.apis,
            },
        )
        art.schedules.append(RenderedSchedule(s.name, s.cron, s.timezone, s.payload(), overlap_guard="app"))
    if spec.schedules:
        art.workloads.append(Workload("job", "job", "cloud-run-job", ["invoke", "<schedule payload>"]))


# ----------------------------------------------------------------------
# GKE (existing cluster)
# ----------------------------------------------------------------------


def _render_gke(spec, tf: TfDoc, art: Artifact, b: GcpBacking):
    t = required(spec.targets.gcp, "gcp")
    k = required(t.kubernetes, "kubernetes")
    app = spec.app.name
    pool = f"{t.project}.svc.id.goog"
    if spec.worker.enabled:
        sub = _subscription(spec, tf, b, None)
        tf.resource(
            "google_pubsub_subscription_iam_member",
            "runtime_pull",
            {"subscription": ref(sub, "name"), "role": "roles/pubsub.subscriber", "member": b.runtime_member},
        )
    # GKE Workload Identity: the pod's Kubernetes SA impersonates the runtime service account.
    tf.resource(
        "google_service_account_iam_member",
        "workload_identity",
        {
            "service_account_id": ref(b.runtime_sa, "name"),
            "role": "roles/iam.workloadIdentityUser",
            "member": f"serviceAccount:{pool}[{k.namespace}/{app}]",
        },
    )
    keda = None
    if k.keda:
        # KEDA reads subscription backlog from Cloud Monitoring as its own federated identity.
        tf.resource(
            "google_project_iam_member",
            "keda_metrics",
            {
                "project": t.project,
                "role": "roles/monitoring.viewer",
                "member": f"principal://iam.googleapis.com/projects/{b.number}/locations/global/workloadIdentityPools/{pool}"
                f"/subject/ns/{k.keda_namespace}/sa/keda-operator",
            },
        )
        keda = KedaTrigger(
            "gcp-pubsub",
            {
                "subscriptionName": f"projects/{t.project}/subscriptions/{b.subscription}",
                "mode": "SubscriptionSize",
                "value": "1",
            },
            {"provider": "gcp"},
        )
    env = common_env(
        spec,
        "gcp",
        platform="kubernetes",
        compute="deployment",
        secrets=b.secrets_url,
        state_url=f"gs://{ref(b.bucket, 'name')}/skyctl",
        queue_url=b.queue_url,
        delayed_url="none",  # Cloud Tasks needs a reachable HTTPS endpoint; tick covers one-time posts
        extra={"GOOGLE_CLOUD_PROJECT": t.project, "CLOUD_ML_REGION": t.region},
    )
    art.env = env
    emit_k8s(
        tf,
        art,
        build_k8s(
            spec,
            namespace=k.namespace,
            env=env,
            sa_annotations={"iam.gke.io/gcp-service-account": ref(b.runtime_sa, "email")},
            keda=keda,
            ingress_host=k.ingress_host,
            ingress_class=k.ingress_class,
        ),
    )
