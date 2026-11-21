"""
skyctl.deploy.render.azure
==========================
DeploySpec → Terraform JSON for Azure.

    intent              Azure
    ------------------  --------------------------------------------------------------
    identity            one user-assigned managed identity (AZURE_CLIENT_ID selects it)
    secrets             Key Vault, RBAC mode (runtime fetch); HTTP token as a Key Vault
                        reference on the Container App (native injection)
    state               Blob Storage, Entra-only (shared keys disabled), versioned,
                        lifecycle on runs/
    worker              Container Apps Job, event trigger (KEDA azure-servicebus scaler),
                        `skyctl worker --once`
    schedules           Container Apps Jobs, schedule trigger (cron is UTC only)
    delayed delivery    Service Bus scheduled messages on the work queue
    HTTP service        Container App with ingress (external only when public)
    model               Azure OpenAI via the same identity ("Cognitive Services OpenAI User")

Notes:
* The KEDA scale rule authenticates with a listen-only SAS rule: it only
  counts messages; the app itself uses Entra (Data Sender/Receiver roles).
* Service Bus locks cap at 5 minutes; the worker renews them while a run is
  in progress (skyctl.platform.invocation heartbeat).
* Pinned to azurerm 3.x attribute names.
"""

from __future__ import annotations

import re

from ..spec import DeploySpec
from .base import Artifact, RenderedSchedule, TfDoc, Workload, common_env, invoke_args, ref

ROLE_SB_SEND = "Azure Service Bus Data Sender"
ROLE_SB_RECEIVE = "Azure Service Bus Data Receiver"


def _memory_for(cpu: float) -> str:
    return f"{cpu * 2:g}Gi"      # Container Apps requires memory = 2 × cpu (GiB)


def render_azure(spec: DeploySpec) -> Artifact:
    t = spec.targets.azure
    app = spec.app.name
    compact = re.sub(r"[^a-z0-9]", "", app)
    art = Artifact("azure")
    tf = TfDoc(
        {"azurerm": {"source": "hashicorp/azurerm", "version": "~> 3.110"},
         "random": {"source": "hashicorp/random", "version": ">= 3.5"}},
        {"azurerm": {"features": {}, "storage_use_azuread": True}},
    )
    image = tf.variable("image", default=spec.app.image)
    tf.variable("secrets_json", sensitive=True, description="The BSKY_* / API-key map as JSON or dotenv text.")
    bearer = spec.service.enabled and "bearer" in spec.service.auth
    if bearer:
        tf.variable("http_token", sensitive=True)
    client = tf.data("azurerm_client_config", "current")
    suffix = tf.resource("random_string", "suffix", {"length": 6, "upper": False, "special": False, "numeric": True})
    sfx = ref(suffix, "result")

    rg = tf.resource("azurerm_resource_group", "main", {"name": f"{app}-rg", "location": t.location})
    loc = {"location": t.location, "resource_group_name": ref(rg, "name")}
    uami = tf.resource("azurerm_user_assigned_identity", "app", {"name": f"{app}-id", **loc})
    principal = ref(uami, "principal_id")
    identity_block = {"type": "UserAssigned", "identity_ids": [ref(uami, "id")]}

    def grant(name: str, scope: str, role: str, who: str = principal, **extra) -> str:
        return tf.resource("azurerm_role_assignment", name, {"scope": scope, "role_definition_name": role, "principal_id": who, **extra})

    # -- secrets ---------------------------------------------------------
    kv = tf.resource("azurerm_key_vault", "main", {
        "name": f"{compact[:12]}-kv-{sfx}", **loc, "tenant_id": ref(client, "tenant_id"), "sku_name": "standard",
        "enable_rbac_authorization": True, "purge_protection_enabled": False, "soft_delete_retention_days": 7,
    })
    officer = grant("kv_deployer", ref(kv, "id"), "Key Vault Secrets Officer", ref(client, "object_id"))
    grant("kv_app", ref(kv, "id"), "Key Vault Secrets User")
    tf.resource("azurerm_key_vault_secret", "config", {
        "name": "skyctl-config", "value": "${var.secrets_json}", "key_vault_id": ref(kv, "id"), "depends_on": [officer],
    })
    token_secret = None
    if bearer:
        token_secret = tf.resource("azurerm_key_vault_secret", "http_token", {
            "name": "skyctl-http-token", "value": "${var.http_token}", "key_vault_id": ref(kv, "id"), "depends_on": [officer],
        })

    # -- state -------------------------------------------------------------
    sa = tf.resource("azurerm_storage_account", "state", {
        "name": f"{compact[:16]}{sfx}", **loc, "account_tier": "Standard", "account_replication_type": "LRS",
        "min_tls_version": "TLS1_2", "allow_nested_items_to_be_public": False, "shared_access_key_enabled": False,
        "blob_properties": {"versioning_enabled": spec.state.versioning},
    })
    blob_role = grant("blob_app", ref(sa, "id"), "Storage Blob Data Contributor")
    deployer_blob = grant("blob_deployer", ref(sa, "id"), "Storage Blob Data Contributor", ref(client, "object_id"))
    container = tf.resource("azurerm_storage_container", "state", {
        "name": "skyctl", "storage_account_name": ref(sa, "name"), "container_access_type": "private",
        "depends_on": [deployer_blob],
    })
    tf.resource("azurerm_storage_management_policy", "state", {
        "storage_account_id": ref(sa, "id"),
        "rule": [{
            "name": "expire-run-transcripts", "enabled": True,
            "filters": {"prefix_match": ["skyctl/skyctl/runs/"], "blob_types": ["blockBlob"]},
            "actions": {"base_blob": {"delete_after_days_since_modification_greater_than": spec.state.retention_days},
                        "version": {"delete_after_days_since_creation": 30}},
        }],
    })
    if spec.tasks:
        tf.resource("azurerm_storage_blob", "tasks", {
            "name": "skyctl/config/tasks.toml", "storage_account_name": ref(sa, "name"),
            "storage_container_name": ref(container, "name"), "type": "Block",
            "source_content": spec.tasks_toml(), "content_type": "application/toml",
        })

    # -- queue (+ delayed delivery via scheduled messages) -----------------
    sb = tf.resource("azurerm_servicebus_namespace", "main", {"name": f"{app}-sb-{sfx}", **loc, "sku": "Standard"})
    queue = tf.resource("azurerm_servicebus_queue", "work", {
        "name": f"{app}-work", "namespace_id": ref(sb, "id"), "lock_duration": "PT5M",
        "max_delivery_count": spec.worker.max_attempts, "dead_lettering_on_message_expiration": True,
    })
    grant("sb_send", ref(queue, "id"), ROLE_SB_SEND)
    grant("sb_receive", ref(queue, "id"), ROLE_SB_RECEIVE)
    listen = tf.resource("azurerm_servicebus_queue_authorization_rule", "keda", {
        "name": "keda-listen", "queue_id": ref(queue, "id"), "listen": True, "send": False, "manage": False,
    })

    # -- model -------------------------------------------------------------
    extra = {
        "AZURE_CLIENT_ID": ref(uami, "client_id"),
        "AZURE_STORAGE_ACCOUNT_NAME": ref(sa, "name"),
        "AZURE_TENANT_ID": ref(client, "tenant_id"),
    }
    if t.openai_endpoint:
        extra["AZURE_OPENAI_ENDPOINT"] = t.openai_endpoint
    if t.openai_resource_id:
        grant("openai_user", t.openai_resource_id, "Cognitive Services OpenAI User")

    env = common_env(
        spec, "azure", platform="azure", compute="container-apps-job",
        secrets=f"azure-kv://{ref(kv, 'name')}/skyctl-config", state_url="az://skyctl/skyctl",
        queue_url=f"servicebus://{ref(sb, 'name')}.servicebus.windows.net/{app}-work", delayed_url="queue",
        extra=extra,
    )
    art.env = env

    logs = tf.resource("azurerm_log_analytics_workspace", "main", {"name": f"{app}-logs", **loc, "sku": "PerGB2018", "retention_in_days": 30})
    cae = tf.resource("azurerm_container_app_environment", "main", {
        "name": f"{app}-env", **loc, "log_analytics_workspace_id": ref(logs, "id"),
    })
    depends = [blob_role, "azurerm_role_assignment.kv_app", "azurerm_role_assignment.sb_send", "azurerm_role_assignment.sb_receive"]

    def env_list(compute: str, with_token: bool = False) -> list[dict]:
        items = [{"name": k, "value": v} for k, v in {**env, "SKYCTL_COMPUTE": compute}.items()]
        if with_token:
            items.append({"name": "SKYCTL_HTTP_TOKEN", "secret_name": "http-token"})
        return items

    def container_block(args: list[str], compute: str, cpu: float = 0.5, with_token: bool = False) -> dict:
        return {"name": "skyctl", "image": image, "cpu": cpu, "memory": _memory_for(cpu), "args": args,
                "env": env_list(compute, with_token)}

    # -- HTTP service --------------------------------------------------------
    if spec.service.enabled:
        svc = spec.service
        body = {
            "name": f"{app}-http"[:32], "container_app_environment_id": ref(cae, "id"), "resource_group_name": ref(rg, "name"),
            "revision_mode": "Single", "identity": identity_block,
            "ingress": {"external_enabled": svc.public, "target_port": 8080, "transport": "auto",
                        "traffic_weight": [{"percentage": 100, "latest_revision": True}]},
            "template": {
                "min_replicas": svc.min_instances, "max_replicas": svc.max_instances,
                "container": [{**container_block(["serve"], "container-apps", svc.cpu, bool(token_secret)),
                               "liveness_probe": {"transport": "HTTP", "port": 8080, "path": "/healthz"}}],
            },
            "depends_on": depends,
        }
        if token_secret:
            body["secret"] = [{"name": "http-token", "key_vault_secret_id": ref(token_secret, "versionless_id"),
                               "identity": ref(uami, "id")}]
        http = tf.resource("azurerm_container_app", "http", body)
        tf.output("service_url", f"https://{ref(http, 'latest_revision_fqdn')}")
        art.workloads.append(Workload("http", "service", "container-apps", ["serve"]))
        if svc.public:
            art.public_endpoints.append("container-app-ingress:443")

    # -- worker: event-driven job -------------------------------------------
    if spec.worker.enabled:
        tf.resource("azurerm_container_app_job", "worker", {
            "name": f"{app}-worker"[:32], **loc, "container_app_environment_id": ref(cae, "id"),
            "replica_timeout_in_seconds": spec.worker.visibility_seconds + spec.policy.max_run_seconds,
            "replica_retry_limit": 0, "identity": identity_block,
            "secret": [{"name": "sb-listen", "value": ref(listen, "primary_connection_string")}],
            "event_trigger_config": {
                "parallelism": 1, "replica_completion_count": 1,
                "scale": {
                    "min_executions": 0, "max_executions": spec.worker.max_concurrency, "polling_interval_in_seconds": 30,
                    "rules": [{
                        "name": "servicebus", "custom_rule_type": "azure-servicebus",
                        "metadata": {"queueName": f"{app}-work", "namespace": ref(sb, "name"), "messageCount": "1"},
                        "authentication": [{"secret_name": "sb-listen", "trigger_parameter": "connection"}],
                    }],
                },
            },
            "template": {"container": [container_block(["worker", "--once"], "container-apps-job")]},
            "depends_on": depends,
        })
        art.workloads.append(Workload("worker", "worker", "container-apps-job", ["worker", "--once"]))

    # -- schedules -------------------------------------------------------------
    for s in spec.schedules:
        tf.resource("azurerm_container_app_job", f"schedule_{s.name.replace('-', '_')}", {
            "name": f"{app}-{s.name}"[:32], **loc, "container_app_environment_id": ref(cae, "id"),
            "replica_timeout_in_seconds": s.timeout_s, "replica_retry_limit": 0, "identity": identity_block,
            "schedule_trigger_config": {"cron_expression": s.cron, "parallelism": 1, "replica_completion_count": 1},
            "template": {"container": [container_block(invoke_args(s.invoke), "container-apps-job")]},
            "depends_on": depends,
        })
        # Executions of a schedule job can overlap if one outlasts the interval:
        # correctness comes from the claim protocol, not the platform.
        art.schedules.append(RenderedSchedule(s.name, s.cron, "UTC", s.invoke, overlap_guard="app"))

    tf.output("env", env)
    tf.output("key_vault", ref(kv, "name"))
    tf.output("storage_account", ref(sa, "name"))
    art.terraform = tf.doc
    art.files["main.tf.json"] = tf.to_json()
    art.notes.append("Provider azurerm 4.x renames some attributes (e.g. storage_account_id on containers); this output targets 3.x.")
    return art
