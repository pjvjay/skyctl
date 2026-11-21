"""
skyctl.deploy.render.k8s_common
===============================
The one Kubernetes manifest builder, shared by:

* the `kubernetes` target (any cluster, bring-your-own services), and
* the Kubernetes compute mode of each cloud target (aws compute = "eks",
  azure compute = "aks", gcp compute = "gke"), where the cloud renderer also
  provisions the backing services and the workload-identity binding.

Objects whose content depends on values that exist only after
`terraform apply` (bucket and queue names, role ARN / client id / service
account email) are returned separately as `bind_items`. Cloud renderers
write them to `bind.json` through a Terraform `local_file`, so Terraform
never needs cluster credentials:

    terraform apply                     # creates cloud resources and writes bind.json
    kubectl apply -f bind.json          # Namespace, ServiceAccount, env ConfigMap, KEDA objects
    kubectl apply -f manifests.json     # Deployments, Service, Ingress, CronJobs

For the bring-your-own target everything is static, so bind items are
merged into manifests.json.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..spec import DeploySpec
from .base import RenderedSchedule, Workload, invoke_args


@dataclass
class KedaTrigger:
    type: str                       # aws-sqs-queue | azure-queue | gcp-pubsub | azure-servicebus
    metadata: dict
    pod_identity: dict              # TriggerAuthentication spec.podIdentity


@dataclass
class K8sPlan:
    items: list = field(default_factory=list)
    bind_items: list = field(default_factory=list)
    workloads: list = field(default_factory=list)
    schedules: list = field(default_factory=list)
    public_endpoints: list = field(default_factory=list)


def build_k8s(
    spec: DeploySpec,
    *,
    namespace: str,
    env: dict,
    sa_annotations: Optional[dict] = None,
    pod_labels: Optional[dict] = None,
    secret_name: Optional[str] = None,
    tasks_configmap: bool = False,
    keda: Optional[KedaTrigger] = None,
    has_queue: bool = True,
    ingress_host: Optional[str] = None,
    ingress_class: Optional[str] = None,
) -> K8sPlan:
    app = spec.app.name
    ns = namespace
    plan = K8sPlan()
    labels = {"app.kubernetes.io/name": app, "app.kubernetes.io/managed-by": "skyctl-deploy"}
    env_cm = f"{app}-env"

    plan.bind_items.append({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": labels}})
    sa_meta = {"name": app, "namespace": ns, "labels": labels}
    if sa_annotations:
        sa_meta["annotations"] = sa_annotations
    plan.bind_items.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": sa_meta})
    # The whole env contract lives in one ConfigMap; pods only override SKYCTL_COMPUTE.
    plan.bind_items.append({"apiVersion": "v1", "kind": "ConfigMap",
                            "metadata": {"name": env_cm, "namespace": ns, "labels": labels}, "data": dict(env)})
    if tasks_configmap and spec.tasks:
        plan.items.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{app}-tasks", "namespace": ns, "labels": labels},
                           "data": {"tasks.toml": spec.tasks_toml()}})

    def pod(args: list[str], compute: str, restart: str = "Always", cpu: str = "250m", memory: str = "512Mi") -> dict:
        volumes = [{"name": "tmp", "emptyDir": {}}]
        mounts = [{"name": "tmp", "mountPath": "/tmp"}]
        if tasks_configmap and spec.tasks:
            volumes.append({"name": "tasks", "configMap": {"name": f"{app}-tasks"}})
            mounts.append({"name": "tasks", "mountPath": "/etc/skyctl", "readOnly": True})
        env_from = [{"configMapRef": {"name": env_cm}}]
        if secret_name:
            env_from.append({"secretRef": {"name": secret_name}})
        return {
            "metadata": {"labels": {**labels, **(pod_labels or {})}},
            "spec": {
                "serviceAccountName": app,
                "restartPolicy": restart,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "skyctl", "image": spec.app.image, "args": args,
                    "envFrom": env_from,
                    "env": [{"name": "SKYCTL_COMPUTE", "value": compute}],
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}},
                    "resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"memory": memory}},
                    "volumeMounts": mounts,
                }],
                "volumes": volumes,
            },
        }

    def deployment(name: str, replicas: int, template: dict) -> dict:
        sel = {**labels, "app.kubernetes.io/component": name}
        template["metadata"]["labels"].update(sel)
        return {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": f"{app}-{name}", "namespace": ns, "labels": sel},
                "spec": {"replicas": replicas, "selector": {"matchLabels": sel}, "template": template}}

    # -- HTTP service ----------------------------------------------------------
    if spec.service.enabled:
        svc = spec.service
        tpl = pod(["serve"], "deployment", cpu=f"{int(svc.cpu * 1000)}m", memory=f"{svc.memory_mb}Mi")
        c = tpl["spec"]["containers"][0]
        c["ports"] = [{"containerPort": 8080, "name": "http"}]
        c["readinessProbe"] = {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 10}
        c["livenessProbe"] = {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 30}
        plan.items.append(deployment("http", max(svc.min_instances, 1), tpl))
        sel = {**labels, "app.kubernetes.io/component": "http"}
        plan.items.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": f"{app}-http", "namespace": ns, "labels": labels},
                           "spec": {"selector": sel, "ports": [{"port": 80, "targetPort": "http", "name": "http"}]}})
        if svc.public and ingress_host:
            ing = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                   "metadata": {"name": f"{app}-http", "namespace": ns, "labels": labels},
                   "spec": {"tls": [{"hosts": [ingress_host], "secretName": f"{app}-tls"}],
                            "rules": [{"host": ingress_host, "http": {"paths": [{
                                "path": "/", "pathType": "Prefix",
                                "backend": {"service": {"name": f"{app}-http", "port": {"name": "http"}}}}]}}]}}
            if ingress_class:
                ing["spec"]["ingressClassName"] = ingress_class
            plan.items.append(ing)
            plan.public_endpoints.append(f"ingress:{ingress_host}")
        plan.workloads.append(Workload("http", "service", "deployment", ["serve"]))

    # -- worker ----------------------------------------------------------------
    if spec.worker.enabled and has_queue:
        if keda is not None:
            plan.bind_items.append({"apiVersion": "keda.sh/v1alpha1", "kind": "TriggerAuthentication",
                                    "metadata": {"name": f"{app}-keda", "namespace": ns},
                                    "spec": {"podIdentity": keda.pod_identity}})
            plan.bind_items.append({"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledJob",
                                    "metadata": {"name": f"{app}-worker", "namespace": ns, "labels": labels},
                                    "spec": {"jobTargetRef": {"backoffLimit": 0,
                                                              "template": pod(["worker", "--once"], "keda", restart="Never")},
                                             "pollingInterval": 30, "maxReplicaCount": spec.worker.max_concurrency,
                                             "triggers": [{"type": keda.type, "metadata": keda.metadata,
                                                           "authenticationRef": {"name": f"{app}-keda"}}]}})
            plan.workloads.append(Workload("worker", "worker", "keda", ["worker", "--once"]))
        else:
            plan.items.append(deployment("worker", 1, pod(["worker"], "deployment")))
            plan.workloads.append(Workload("worker", "worker", "deployment", ["worker"]))

    # -- schedules ---------------------------------------------------------------
    for s in spec.schedules:
        payload = s.payload()
        plan.items.append({"apiVersion": "batch/v1", "kind": "CronJob",
                           "metadata": {"name": f"{app}-{s.name}", "namespace": ns, "labels": labels},
                           "spec": {"schedule": s.cron, "timeZone": s.timezone, "concurrencyPolicy": "Forbid",
                                    "startingDeadlineSeconds": 300, "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 3,
                                    "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": s.timeout_s,
                                                             "template": pod(invoke_args(payload), "cronjob", restart="Never")}}}})
        plan.schedules.append(RenderedSchedule(s.name, s.cron, s.timezone, payload, overlap_guard="platform"))
    return plan


def as_list(items: list) -> dict:
    return {"apiVersion": "v1", "kind": "List", "items": items}


def emit_k8s(tf, art, plan: K8sPlan) -> None:
    """Cloud k8s modes: bind.json is written by Terraform (local_file), manifests.json is static."""
    import json

    tf.resource("local_file", "k8s_bind", {
        "filename": "bind.json", "file_permission": "0644",
        "content": json.dumps(as_list(plan.bind_items), indent=2) + "\n",
    })
    art.files["manifests.json"] = json.dumps(as_list(plan.items), indent=2) + "\n"
    art.workloads, art.schedules, art.public_endpoints = plan.workloads, plan.schedules, plan.public_endpoints
    art.notes.append("terraform apply (writes bind.json) → kubectl apply -f bind.json → kubectl apply -f manifests.json")
