"""
skyctl.deploy.render.azure
==========================
DeploySpec → Terraform JSON for Azure.

Backing services (identical for both compute modes, see azure_backing()):
one user-assigned managed identity; Key Vault (RBAC); one storage account,
Entra-only (shared keys disabled), holding both the state container (the
JSON job document + sessions/ runs/ ledger/ config/) and the Storage
Queues (work + poison); prevent_destroy on the account.

    intent            compute = "container-apps"                       compute = "aks" (existing cluster)
    ----------------  -----------------------------------------------  ----------------------------------------
    identity          UAMI attached to every app and job               UAMI + federated credential for the pod SA
    HTTP token        Key Vault reference on the Container App         fetched: …/skyctl-http-token#SKYCTL_HTTP_TOKEN
    worker            Container Apps Job on a schedule: drain the      Deployment, or KEDA ScaledJob (azure-queue,
                      queue every worker.poll_minutes, then exit       azure-workload identity)
    schedules         Container Apps Jobs, schedule trigger (UTC)      CronJob (Forbid, any time zone)
    delayed delivery  Storage Queue initial visibility (≤ 7 days); later posts go to the tick catch-up
    HTTP service      Container App with ingress                       Deployment + Service (+ Ingress)
    model             Azure OpenAI via the same identity ("Cognitive Services OpenAI User")

Why the Container Apps worker polls instead of using a KEDA event trigger:
the azure-queue scaler on Container Apps authenticates with a connection
string, which would mean re-enabling shared-key access on the storage
account. A scheduled drain keeps the account Entra-only, costs a few
seconds of job time per poll, and adds at most poll_minutes of latency to
queued agent runs. On AKS, KEDA uses workload identity, so it is offered.

Pinned to azurerm 3.x attribute names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..spec import DeploySpec
from .base import Artifact, RenderedSchedule, TfDoc, Workload, common_env, invoke_args, ref
from .k8s_common import KedaTrigger, build_k8s, emit_k8s

ROLE_BLOB = "Storage Blob Data Contributor"
ROLE_QUEUE = "Storage Queue Data Contributor"


def _memory_for(cpu: float) -> str:
    return f"{cpu * 2:g}Gi"      # Container Apps requires memory = 2 × cpu (GiB)


@dataclass
class AzureBacking:
    rg: str
    uami: str
    storage: str
    token_secret: str | None
    depends: list
    env_extra: dict
    secrets_url: str
    queue_url: str
    delayed_url: str


def azure_backing(spec: DeploySpec, tf: TfDoc, *, fetch_token: bool) -> AzureBacking:
    t = spec.targets.azure
    app = spec.app.name
    compact = re.sub(r"[^a-z0-9]", "", app)
    client = tf.data("azurerm_client_config", "current")
    deployer = ref(client, "object_id")
    sfx = ref(tf.resource("random_string", "suffix", {"length": 6, "upper": False, "special": False, "numeric": True}), "result")

    rg = tf.resource("azurerm_resource_group", "main", {"name": f"{app}-rg", "location": t.location})
    loc = {"location": t.location, "resource_group_name": ref(rg, "name")}
    uami = tf.resource("azurerm_user_assigned_identity", "app", {"name": f"{app}-id", **loc})
    principal = ref(uami, "principal_id")

    def grant(name: str, scope: str, role: str, who: str = principal, **extra) -> str:
        return tf.resource("azurerm_role_assignment", name, {"scope": scope, "role_definition_name": role, "principal_id": who, **extra})

    # -- secrets -------------------------------------------------------------
    kv = tf.resource("azurerm_key_vault", "main", {
        "name": f"{compact[:12]}-kv-{sfx}", **loc, "tenant_id": ref(client, "tenant_id"), "sku_name": "standard",
        "enable_rbac_authorization": True, "purge_protection_enabled": False, "soft_delete_retention_days": 7,
    })
    officer = grant("kv_deployer", ref(kv, "id"), "Key Vault Secrets Officer", deployer)
    kv_app = grant("kv_app", ref(kv, "id"), "Key Vault Secrets User")
    tf.resource("azurerm_key_vault_secret", "config", {
        "name": "skyctl-config", "value": "${var.secrets_json}", "key_vault_id": ref(kv, "id"), "depends_on": [officer],
    })
    token_secret = None
    if spec.service.enabled and "bearer" in spec.service.auth:
        token_secret = tf.resource("azurerm_key_vault_secret", "http_token", {
            "name": "skyctl-http-token", "value": "${var.http_token}", "key_vault_id": ref(kv, "id"), "depends_on": [officer],
        })
    secrets_url = f"azure-kv://{ref(kv, 'name')}/skyctl-config"
    if token_secret and fetch_token:
        secrets_url += f",azure-kv://{ref(kv, 'name')}/skyctl-http-token#SKYCTL_HTTP_TOKEN"

    # -- one storage account: state container + queues, Entra-only -------------
    sa_body = {
        "name": f"{compact[:16]}{sfx}", **loc, "account_tier": "Standard", "account_replication_type": "LRS",
        "min_tls_version": "TLS1_2", "allow_nested_items_to_be_public": False, "shared_access_key_enabled": False,
        "blob_properties": {"versioning_enabled": spec.state.versioning},
    }
    if spec.state.protect:
        sa_body["lifecycle"] = {"prevent_destroy": True}
    sa = tf.resource("azurerm_storage_account", "state", sa_body)
    # The deployer creates containers/queues through the data plane (shared keys are off).
    blob_deployer = grant("blob_deployer", ref(sa, "id"), ROLE_BLOB, deployer)
    queue_deployer = grant("queue_deployer", ref(sa, "id"), ROLE_QUEUE, deployer)
    container = tf.resource("azurerm_storage_container", "state", {
        "name": "skyctl", "storage_account_name": ref(sa, "name"), "container_access_type": "private",
        "depends_on": [blob_deployer],
    })
    work = tf.resource("azurerm_storage_queue", "work", {"name": f"{app}-work", "storage_account_name": ref(sa, "name"),
                                                         "depends_on": [queue_deployer]})
    poison = tf.resource("azurerm_storage_queue", "poison", {"name": f"{app}-work-poison", "storage_account_name": ref(sa, "name"),
                                                             "depends_on": [queue_deployer]})
    # The workload's grants are scoped to the container and the two queues, not the account.
    blob_app = grant("blob_app", ref(container, "resource_manager_id"), ROLE_BLOB)
    queue_app = grant("queue_app", ref(work, "resource_manager_id"), ROLE_QUEUE)
    poison_app = grant("poison_app", ref(poison, "resource_manager_id"), ROLE_QUEUE)
    tf.resource("azurerm_storage_management_policy", "state", {
        "storage_account_id": ref(sa, "id"),
        "rule": [{
            "name": "expire-runs-and-ledger", "enabled": True,
            "filters": {"prefix_match": ["skyctl/skyctl/runs/", "skyctl/skyctl/ledger/"], "blob_types": ["blockBlob"]},
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

    extra = {
        "AZURE_CLIENT_ID": ref(uami, "client_id"),
        "AZURE_STORAGE_ACCOUNT_NAME": ref(sa, "name"),
        "AZURE_TENANT_ID": ref(client, "tenant_id"),
    }
    if t.openai_endpoint:
        extra["AZURE_OPENAI_ENDPOINT"] = t.openai_endpoint
    if t.openai_resource_id:
        grant("openai_user", t.openai_resource_id, "Cognitive Services OpenAI User")

    return AzureBacking(
        rg=rg, uami=uami, storage=sa, token_secret=token_secret,
        depends=[kv_app, blob_app, queue_app, poison_app], env_extra=extra, secrets_url=secrets_url,
        queue_url=f"azqueue://{ref(sa, 'name')}/{app}-work",
        delayed_url="queue",          # Storage Queue initial visibility, ≤ 7 days
    )


def render_azure(spec: DeploySpec) -> Artifact:
    t = spec.targets.azure
    aks = t.compute == "aks"
    art = Artifact("azure")
    providers = {"azurerm": {"source": "hashicorp/azurerm", "version": "~> 3.110"},
                 "random": {"source": "hashicorp/random", "version": ">= 3.5"}}
    if aks:
        providers["local"] = {"source": "hashicorp/local", "version": ">= 2.4"}
    tf = TfDoc(providers, {"azurerm": {"features": {}, "storage_use_azuread": True}})
    image = tf.variable("image", default=spec.app.image)
    tf.variable("secrets_json", sensitive=True, description="The BSKY_* / API-key map as JSON or dotenv text.")
    if spec.service.enabled and "bearer" in spec.service.auth:
        tf.variable("http_token", sensitive=True)

    backing = azure_backing(spec, tf, fetch_token=aks)
    env = common_env(
        spec, "azure", platform="kubernetes" if aks else "azure",
        compute="deployment" if aks else "container-apps-job", secrets=backing.secrets_url,
        state_url="az://skyctl/skyctl", queue_url=backing.queue_url, delayed_url=backing.delayed_url,
        extra=backing.env_extra,
    )
    art.env = env
    if aks:
        _render_aks(spec, tf, art, env, backing)
    else:
        _render_container_apps(spec, tf, art, image, env, backing)

    tf.output("env", env)
    tf.output("state_url", f"az://skyctl/skyctl (account {ref(backing.storage, 'name')})")
    tf.output("storage_account", ref(backing.storage, "name"))
    art.terraform = tf.doc
    art.files["main.tf.json"] = tf.to_json()
    art.notes.append("Provider azurerm 4.x renames some attributes (e.g. storage_account_id on containers); this output targets 3.x.")
    return art


# ----------------------------------------------------------------------
# Container Apps
# ----------------------------------------------------------------------

def _render_container_apps(spec, tf: TfDoc, art: Artifact, image, env, b: AzureBacking):
    t = spec.targets.azure
    app = spec.app.name
    loc = {"location": t.location, "resource_group_name": ref(b.rg, "name")}
    identity_block = {"type": "UserAssigned", "identity_ids": [ref(b.uami, "id")]}
    logs = tf.resource("azurerm_log_analytics_workspace", "main", {"name": f"{app}-logs", **loc, "sku": "PerGB2018", "retention_in_days": 30})
    cae = tf.resource("azurerm_container_app_environment", "main", {"name": f"{app}-env", **loc, "log_analytics_workspace_id": ref(logs, "id")})

    def env_list(compute: str, with_token: bool = False) -> list[dict]:
        items = [{"name": k, "value": v} for k, v in {**env, "SKYCTL_COMPUTE": compute}.items()]
        if with_token:
            items.append({"name": "SKYCTL_HTTP_TOKEN", "secret_name": "http-token"})
        return items

    def container_block(args: list[str], compute: str, cpu: float = 0.5, with_token: bool = False) -> dict:
        return {"name": "skyctl", "image": image, "cpu": cpu, "memory": _memory_for(cpu), "args": args,
                "env": env_list(compute, with_token)}

    def job(key: str, name: str, cron: str, args: list[str], timeout: int) -> None:
        tf.resource("azurerm_container_app_job", key, {
            "name": f"{app}-{name}"[:32], **loc, "container_app_environment_id": ref(cae, "id"),
            "replica_timeout_in_seconds": timeout, "replica_retry_limit": 0, "identity": identity_block,
            "schedule_trigger_config": {"cron_expression": cron, "parallelism": 1, "replica_completion_count": 1},
            "template": {"container": [container_block(args, "container-apps-job")]},
            "depends_on": b.depends,
        })

    if spec.service.enabled:
        svc = spec.service
        body = {
            "name": f"{app}-http"[:32], "container_app_environment_id": ref(cae, "id"), "resource_group_name": ref(b.rg, "name"),
            "revision_mode": "Single", "identity": identity_block,
            "ingress": {"external_enabled": svc.public, "target_port": 8080, "transport": "auto",
                        "traffic_weight": [{"percentage": 100, "latest_revision": True}]},
            "template": {
                "min_replicas": svc.min_instances, "max_replicas": svc.max_instances,
                "container": [{**container_block(["serve"], "container-apps", svc.cpu, bool(b.token_secret)),
                               "liveness_probe": {"transport": "HTTP", "port": 8080, "path": "/healthz"}}],
            },
            "depends_on": b.depends,
        }
        if b.token_secret:
            body["secret"] = [{"name": "http-token", "key_vault_secret_id": ref(b.token_secret, "versionless_id"),
                               "identity": ref(b.uami, "id")}]
        http = tf.resource("azurerm_container_app", "http", body)
        tf.output("service_url", f"https://{ref(http, 'latest_revision_fqdn')}")
        art.workloads.append(Workload("http", "service", "container-apps", ["serve"]))
        if svc.public:
            art.public_endpoints.append("container-app-ingress:443")

    if spec.worker.enabled:
        poll = spec.worker.poll_minutes
        # Drain and exit; overlapping drains are safe (each message is received by one drainer).
        job("worker", "worker", "* * * * *" if poll == 1 else f"*/{poll} * * * *", ["worker", "--once"],
            spec.worker.visibility_seconds + spec.policy.max_run_seconds)
        art.workloads.append(Workload("worker", "worker", "container-apps-job", ["worker", "--once"]))

    for s in spec.schedules:
        job(f"schedule_{s.name.replace('-', '_')}", s.name, s.cron, invoke_args(s.payload()), s.timeout_s)
        # Executions can overlap if one outlasts the interval: the claim protocol makes that safe.
        art.schedules.append(RenderedSchedule(s.name, s.cron, "UTC", s.payload(), overlap_guard="app"))


# ----------------------------------------------------------------------
# AKS (existing cluster)
# ----------------------------------------------------------------------

def _render_aks(spec, tf: TfDoc, art: Artifact, env, b: AzureBacking):
    k = spec.targets.azure.kubernetes
    app = spec.app.name
    cluster = tf.data("azurerm_kubernetes_cluster", "aks", {"name": k.cluster_name, "resource_group_name": k.cluster_resource_group})

    def federate(key: str, namespace: str, sa: str) -> None:
        tf.resource("azurerm_federated_identity_credential", key, {
            "name": f"{app}-{key}".replace("_", "-"), "resource_group_name": ref(b.rg, "name"), "parent_id": ref(b.uami, "id"),
            "audience": ["api://AzureADTokenExchange"], "issuer": ref(cluster, "oidc_issuer_url"),
            "subject": f"system:serviceaccount:{namespace}:{sa}",
        })

    federate("k8s_app", k.namespace, app)
    keda = None
    if k.keda:
        federate("k8s_keda", k.keda_namespace, "keda-operator")   # KEDA reads queue length as the same identity
        keda = KedaTrigger("azure-queue",
                           {"queueName": f"{app}-work", "accountName": ref(b.storage, "name"), "queueLength": "1"},
                           {"provider": "azure-workload", "identityId": ref(b.uami, "client_id")})
    emit_k8s(tf, art, build_k8s(
        spec, namespace=k.namespace, env=env,
        sa_annotations={"azure.workload.identity/client-id": ref(b.uami, "client_id")},
        pod_labels={"azure.workload.identity/use": "true"}, keda=keda,
        ingress_host=k.ingress_host, ingress_class=k.ingress_class,
    ))
