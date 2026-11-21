"""
skyctl.deploy.render.gcp
========================
DeploySpec → Terraform JSON for Google Cloud.

    intent              Google Cloud
    ------------------  -------------------------------------------------------------------
    identity            runtime service account; a separate invoker service account that
                        Pub/Sub, Cloud Tasks and Cloud Scheduler use to mint OIDC tokens
    secrets             Secret Manager (runtime fetch); HTTP token as a secret env ref
    state               GCS (CAS via generation preconditions), versioned, lifecycle on runs/
    worker              the Cloud Run service itself: Pub/Sub PUSH → POST /invoke with an
                        OIDC token; dead-letter topic after max attempts
    schedules           Cloud Scheduler → Cloud Run Jobs (`skyctl invoke '<json>'`), any time zone
    delayed delivery    Cloud Tasks HTTP tasks → POST /invoke (30-day horizon)
    HTTP service        the same Cloud Run service; IAM-only invocation unless public
    model               Vertex AI ("roles/aiplatform.user")

Notes:
* The service must know its own URL (Cloud Tasks target, OIDC audience).
  Referencing the service's `uri` from its own env would be a cycle, so the
  deterministic form https://<name>-<project-number>.<region>.run.app is used.
* Pub/Sub push ack deadline is 600s: validate() keeps policy.max_run_seconds
  below it so a long run is not redelivered mid-flight.
* Cloud Scheduler may start a job while the previous execution still runs:
  the claim protocol makes that harmless.
"""

from __future__ import annotations

from ..spec import DeploySpec
from .base import Artifact, RenderedSchedule, TfDoc, Workload, common_env, invoke_args, ref

APIS = ("run.googleapis.com", "cloudscheduler.googleapis.com", "secretmanager.googleapis.com",
        "pubsub.googleapis.com", "cloudtasks.googleapis.com", "aiplatform.googleapis.com", "iam.googleapis.com")


def render_gcp(spec: DeploySpec) -> Artifact:
    t = spec.targets.gcp
    app = spec.app.name
    art = Artifact("gcp")
    tf = TfDoc({"google": {"source": "hashicorp/google", "version": ">= 5.40"}},
               {"google": {"project": t.project, "region": t.region}})
    image = tf.variable("image", default=spec.app.image)
    tf.variable("secrets_json", sensitive=True, description="The BSKY_* / API-key map as JSON or dotenv text.")
    bearer = spec.service.enabled and "bearer" in spec.service.auth
    if bearer:
        tf.variable("http_token", sensitive=True)
    project = tf.data("google_project", "this")
    number = ref(project, "number")
    apis = []
    for api in APIS:
        apis.append(tf.resource("google_project_service", api.split(".")[0], {"service": api, "disable_on_destroy": False}))

    runtime_sa = tf.resource("google_service_account", "runtime", {"account_id": f"{app}-run"[:30], "display_name": "skyctl runtime"})
    invoker_sa = tf.resource("google_service_account", "invoker", {"account_id": f"{app}-invoker"[:30], "display_name": "skyctl invoker"})
    runtime_member = f"serviceAccount:{ref(runtime_sa, 'email')}"
    invoker_member = f"serviceAccount:{ref(invoker_sa, 'email')}"
    pubsub_agent = f"serviceAccount:service-{number}@gcp-sa-pubsub.iam.gserviceaccount.com"

    # -- secrets -------------------------------------------------------------
    secret = tf.resource("google_secret_manager_secret", "config", {
        "secret_id": f"{app}-config", "replication": {"auto": {}}, "depends_on": apis})
    tf.resource("google_secret_manager_secret_version", "config", {"secret": ref(secret, "id"), "secret_data": "${var.secrets_json}"})
    tf.resource("google_secret_manager_secret_iam_member", "config", {
        "secret_id": ref(secret, "id"), "role": "roles/secretmanager.secretAccessor", "member": runtime_member})
    token_secret = None
    if bearer:
        token_secret = tf.resource("google_secret_manager_secret", "http_token", {
            "secret_id": f"{app}-http-token", "replication": {"auto": {}}, "depends_on": apis})
        tf.resource("google_secret_manager_secret_version", "http_token", {"secret": ref(token_secret, "id"), "secret_data": "${var.http_token}"})
        tf.resource("google_secret_manager_secret_iam_member", "http_token", {
            "secret_id": ref(token_secret, "id"), "role": "roles/secretmanager.secretAccessor", "member": runtime_member})

    # -- state -----------------------------------------------------------------
    bucket = tf.resource("google_storage_bucket", "state", {
        "name": f"{t.project}-{app}-state", "location": t.region, "uniform_bucket_level_access": True,
        "public_access_prevention": "enforced", "versioning": {"enabled": spec.state.versioning},
        "lifecycle_rule": [
            {"condition": {"age": spec.state.retention_days, "matches_prefix": ["skyctl/runs/"]}, "action": {"type": "Delete"}},
            {"condition": {"days_since_noncurrent_time": 30}, "action": {"type": "Delete"}},
        ],
    })
    tf.resource("google_storage_bucket_iam_member", "runtime", {"bucket": ref(bucket, "name"), "role": "roles/storage.objectAdmin", "member": runtime_member})
    if spec.tasks:
        tf.resource("google_storage_bucket_object", "tasks", {
            "name": "skyctl/config/tasks.toml", "bucket": ref(bucket, "name"),
            "content": spec.tasks_toml(), "content_type": "application/toml"})

    # -- the service (HTTP surface and push worker) ---------------------------
    svc_name = f"{app}-svc"
    url = f"https://{svc_name}-{number}.{t.region}.run.app"
    needs_service = spec.service.enabled or spec.worker.enabled or spec.runtime.delayed_delivery

    # -- queue: Pub/Sub push with dead-lettering -------------------------------
    topic = tf.resource("google_pubsub_topic", "work", {"name": f"{app}-work", "depends_on": apis})
    dlq = tf.resource("google_pubsub_topic", "dlq", {"name": f"{app}-work-dlq", "depends_on": apis})
    tf.resource("google_pubsub_topic_iam_member", "runtime_publish", {"topic": ref(topic, "name"), "role": "roles/pubsub.publisher", "member": runtime_member})
    tf.resource("google_pubsub_topic_iam_member", "agent_dlq", {"topic": ref(dlq, "name"), "role": "roles/pubsub.publisher", "member": pubsub_agent})
    tf.resource("google_service_account_iam_member", "agent_token", {
        "service_account_id": ref(invoker_sa, "name"), "role": "roles/iam.serviceAccountTokenCreator", "member": pubsub_agent})
    sub_name = f"{app}-work-push"
    if spec.worker.enabled:
        sub = tf.resource("google_pubsub_subscription", "push", {
            "name": sub_name, "topic": ref(topic, "id"), "ack_deadline_seconds": 600,
            "push_config": {"push_endpoint": f"{url}/invoke",
                            "oidc_token": {"service_account_email": ref(invoker_sa, "email"), "audience": f"{url}/invoke"}},
            "dead_letter_policy": {"dead_letter_topic": ref(dlq, "id"), "max_delivery_attempts": max(5, spec.worker.max_attempts)},
            "retry_policy": {"minimum_backoff": "10s", "maximum_backoff": "600s"},
            "depends_on": ["google_cloud_run_v2_service.svc"],
        })
        tf.resource("google_pubsub_subscription_iam_member", "agent_ack", {
            "subscription": ref(sub, "name"), "role": "roles/pubsub.subscriber", "member": pubsub_agent})
        art.workloads.append(Workload("worker", "worker", "cloud-run", ["POST /invoke (Pub/Sub push)"]))

    # -- delayed delivery: Cloud Tasks ----------------------------------------
    delayed_url = "none"
    if spec.runtime.delayed_delivery:
        tq = tf.resource("google_cloud_tasks_queue", "delayed", {
            "name": f"{app}-delayed", "location": t.region, "retry_config": {"max_attempts": 5}, "depends_on": apis})
        tf.resource("google_cloud_tasks_queue_iam_member", "runtime_enqueue", {
            "name": ref(tq, "name"), "location": t.region, "role": "roles/cloudtasks.enqueuer", "member": runtime_member})
        tf.resource("google_service_account_iam_member", "runtime_actas_invoker", {
            "service_account_id": ref(invoker_sa, "name"), "role": "roles/iam.serviceAccountUser", "member": runtime_member})
        delayed_url = f"cloudtasks://{t.project}/{t.region}/{app}-delayed?url={url}/invoke&sa={ref(invoker_sa, 'email')}"

    if spec.model_for("gcp").startswith("vertex:"):
        tf.resource("google_project_iam_member", "vertex", {"project": t.project, "role": "roles/aiplatform.user", "member": runtime_member})

    auth = ["google-oidc"] + [a for a in spec.service.auth if a != "google-oidc"]
    env = common_env(
        spec, "gcp", platform="gcp", compute="cloud-run", secrets=f"gcp-sm://{t.project}/{app}-config",
        state_url=f"gs://{ref(bucket, 'name')}/skyctl", queue_url=f"pubsub://{t.project}/{app}-work?subscription={sub_name}",
        delayed_url=delayed_url, auth=auth,
        extra={
            "GOOGLE_CLOUD_PROJECT": t.project, "CLOUD_ML_REGION": t.region,
            "SKYCTL_OIDC_AUDIENCE": f"{url},{url}/invoke",
            "SKYCTL_OIDC_ALLOWED": ref(invoker_sa, "email"),
        },
    )
    art.env = env

    def env_blocks(compute: str, with_token: bool = False) -> list[dict]:
        items = [{"name": k, "value": v} for k, v in {**env, "SKYCTL_COMPUTE": compute}.items()]
        if with_token:
            items.append({"name": "SKYCTL_HTTP_TOKEN",
                          "value_source": {"secret_key_ref": {"secret": ref(token_secret, "secret_id"), "version": "latest"}}})
        return items

    if needs_service:
        svc = spec.service
        public = svc.enabled and svc.public
        service = tf.resource("google_cloud_run_v2_service", "svc", {
            "name": svc_name, "location": t.region, "deletion_protection": False,
            # Internal ingress still admits Pub/Sub push, Cloud Tasks and Cloud Scheduler from this project.
            "ingress": "INGRESS_TRAFFIC_ALL" if public else "INGRESS_TRAFFIC_INTERNAL_ONLY",
            "template": {
                "service_account": ref(runtime_sa, "email"),
                "timeout": f"{min(3600, spec.policy.max_run_seconds + 60)}s",
                "scaling": {"min_instance_count": svc.min_instances, "max_instance_count": svc.max_instances},
                "containers": [{
                    "image": image, "args": ["serve"], "env": env_blocks("cloud-run", bool(token_secret)),
                    "ports": {"container_port": 8080},
                    "resources": {"limits": {"cpu": str(max(1, int(svc.cpu))), "memory": f"{svc.memory_mb}Mi"}},
                }],
            },
            "depends_on": apis,
        })
        # Invocation is IAM-gated: only the invoker account; never allUsers.
        tf.resource("google_cloud_run_v2_service_iam_member", "invoker", {
            "name": ref(service, "name"), "location": t.region, "role": "roles/run.invoker", "member": invoker_member})
        tf.output("service_url", url)
        art.workloads.append(Workload("http", "service", "cloud-run", ["serve"]))
        if public:
            art.public_endpoints.append("cloud-run:443 (IAM + app auth)")

    # -- schedules: Cloud Scheduler → Cloud Run Jobs -----------------------------
    for s in spec.schedules:
        key = s.name.replace("-", "_")
        job = tf.resource("google_cloud_run_v2_job", key, {
            "name": f"{app}-{s.name}", "location": t.region, "deletion_protection": False,
            "template": {"task_count": 1, "template": {
                "service_account": ref(runtime_sa, "email"), "timeout": f"{s.timeout_s}s", "max_retries": 0,
                "containers": [{"image": image, "args": invoke_args(s.invoke), "env": env_blocks("cloud-run-job")}],
            }},
            "depends_on": apis,
        })
        tf.resource("google_cloud_run_v2_job_iam_member", f"{key}_invoker", {
            "name": ref(job, "name"), "location": t.region, "role": "roles/run.invoker", "member": invoker_member})
        tf.resource("google_cloud_scheduler_job", key, {
            "name": f"{app}-{s.name}", "region": t.region, "schedule": s.cron, "time_zone": s.timezone,
            "attempt_deadline": "180s",
            "http_target": {
                "http_method": "POST",
                "uri": f"https://run.googleapis.com/v2/projects/{t.project}/locations/{t.region}/jobs/{ref(job, 'name')}:run",
                "oauth_token": {"service_account_email": ref(invoker_sa, "email")},
            },
            "depends_on": apis,
        })
        art.schedules.append(RenderedSchedule(s.name, s.cron, s.timezone, s.invoke, overlap_guard="app"))
    if spec.schedules:
        art.workloads.append(Workload("job", "job", "cloud-run-job", ["invoke", "<schedule payload>"]))

    tf.output("env", env)
    tf.output("state_bucket", ref(bucket, "name"))
    art.terraform = tf.doc
    art.files["main.tf.json"] = tf.to_json()
    return art
