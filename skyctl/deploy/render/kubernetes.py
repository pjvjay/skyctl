"""
skyctl.deploy.render.kubernetes
===============================
DeploySpec → Kubernetes manifests (a v1 List, as JSON: `kubectl apply -f`).
The portable tier: the same manifests run on EKS, AKS, GKE or any cluster;
the only cloud-specific parts are the ServiceAccount annotation for workload
identity and the KEDA trigger type.

    intent          Kubernetes
    --------------  -----------------------------------------------------------------
    identity        ServiceAccount + workload identity annotation for `cloud`
                      aws   eks.amazonaws.com/role-arn (IRSA; Pod Identity needs none)
                      azure azure.workload.identity/client-id (+ pod label use=true)
                      gcp   iam.gke.io/gcp-service-account
    secrets         native injection: envFrom an existing Secret (or sync one with the
                    Secrets Store CSI Driver / External Secrets Operator)
    state/queue     existing cloud buckets/queues by URL (this target provisions none)
    worker          Deployment, or KEDA ScaledJob (scale to zero) when keda = true
    schedules       CronJob with concurrencyPolicy: Forbid and spec.timeZone
    HTTP service    Deployment + Service (+ Ingress when public)
"""

from __future__ import annotations

import json

from ..spec import DeploySpec
from .base import Artifact, RenderedSchedule, Workload, common_env, invoke_args

SA_ANNOTATION = {
    "aws": "eks.amazonaws.com/role-arn",
    "azure": "azure.workload.identity/client-id",
    "gcp": "iam.gke.io/gcp-service-account",
}
KEDA = {
    "sqs": ("aws-sqs-queue", "aws"),
    "servicebus": ("azure-servicebus", "azure-workload"),
    "pubsub": ("gcp-pubsub", "gcp"),
}


def render_kubernetes(spec: DeploySpec) -> Artifact:
    k = spec.targets.kubernetes
    app = spec.app.name
    ns = k.namespace
    art = Artifact("kubernetes")
    labels = {"app.kubernetes.io/name": app, "app.kubernetes.io/managed-by": "skyctl-deploy"}
    env = common_env(
        spec, "kubernetes", platform="kubernetes", compute="deployment", secrets="env",
        state_url=k.state_url, queue_url=k.queue_url or "none", delayed_url=k.delayed_url or "none",
        extra={**({"SKYCTL_TASKS_FILE": "/etc/skyctl/tasks.toml"} if spec.tasks else {}), **k.extra_env},
    )
    art.env = env
    items: list[dict] = [{"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": labels}}]

    sa_meta = {"name": app, "namespace": ns, "labels": labels}
    if k.cloud in SA_ANNOTATION:
        sa_meta["annotations"] = {SA_ANNOTATION[k.cloud]: k.identity}
    items.append({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": sa_meta})
    if spec.tasks:
        items.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{app}-tasks", "namespace": ns, "labels": labels},
                      "data": {"tasks.toml": spec.tasks_toml()}})

    def pod(args: list[str], compute: str, restart: str = "Always", cpu: str = "250m", memory: str = "512Mi") -> dict:
        pod_labels = dict(labels)
        if k.cloud == "azure":
            pod_labels["azure.workload.identity/use"] = "true"
        volumes = [{"name": "tmp", "emptyDir": {}}]
        mounts = [{"name": "tmp", "mountPath": "/tmp"}]
        if spec.tasks:
            volumes.append({"name": "tasks", "configMap": {"name": f"{app}-tasks"}})
            mounts.append({"name": "tasks", "mountPath": "/etc/skyctl", "readOnly": True})
        return {
            "metadata": {"labels": pod_labels},
            "spec": {
                "serviceAccountName": app,
                "restartPolicy": restart,
                "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                "containers": [{
                    "name": "skyctl", "image": spec.app.image, "args": args,
                    "envFrom": [{"secretRef": {"name": k.secret_name}}],
                    "env": [{"name": n, "value": v} for n, v in {**env, "SKYCTL_COMPUTE": compute}.items()],
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

    # -- HTTP service ---------------------------------------------------------
    if spec.service.enabled:
        svc = spec.service
        tpl = pod(["serve"], "deployment", cpu=f"{int(svc.cpu * 1000)}m", memory=f"{svc.memory_mb}Mi")
        c = tpl["spec"]["containers"][0]
        c["ports"] = [{"containerPort": 8080, "name": "http"}]
        c["readinessProbe"] = {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 10}
        c["livenessProbe"] = {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 30}
        items.append(deployment("http", max(svc.min_instances, 1), tpl))
        sel = {**labels, "app.kubernetes.io/component": "http"}
        items.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": f"{app}-http", "namespace": ns, "labels": labels},
                      "spec": {"selector": sel, "ports": [{"port": 80, "targetPort": "http", "name": "http"}]}})
        if svc.public and k.ingress_host:
            ing = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                   "metadata": {"name": f"{app}-http", "namespace": ns, "labels": labels},
                   "spec": {"tls": [{"hosts": [k.ingress_host], "secretName": f"{app}-tls"}],
                            "rules": [{"host": k.ingress_host, "http": {"paths": [{
                                "path": "/", "pathType": "Prefix",
                                "backend": {"service": {"name": f"{app}-http", "port": {"name": "http"}}}}]}}]}}
            if k.ingress_class:
                ing["spec"]["ingressClassName"] = k.ingress_class
            items.append(ing)
            art.public_endpoints.append(f"ingress:{k.ingress_host}")
        art.workloads.append(Workload("http", "service", "deployment", ["serve"]))

    # -- worker ---------------------------------------------------------------
    if spec.worker.enabled and k.queue_url:
        if k.keda:
            scheme = k.queue_url.split("://", 1)[0]
            trigger_type, provider = KEDA[scheme]
            metadata = _keda_metadata(scheme, k.queue_url, k.extra_env)
            items.append({"apiVersion": "keda.sh/v1alpha1", "kind": "TriggerAuthentication",
                          "metadata": {"name": f"{app}-keda", "namespace": ns},
                          "spec": {"podIdentity": {"provider": provider}}})
            job_pod = pod(["worker", "--once"], "keda", restart="Never")
            items.append({"apiVersion": "keda.sh/v1alpha1", "kind": "ScaledJob",
                          "metadata": {"name": f"{app}-worker", "namespace": ns, "labels": labels},
                          "spec": {"jobTargetRef": {"backoffLimit": 0, "template": job_pod},
                                   "pollingInterval": 30, "maxReplicaCount": spec.worker.max_concurrency,
                                   "triggers": [{"type": trigger_type, "metadata": metadata,
                                                 "authenticationRef": {"name": f"{app}-keda"}}]}})
            art.workloads.append(Workload("worker", "worker", "keda", ["worker", "--once"]))
        else:
            items.append(deployment("worker", 1, pod(["worker"], "deployment")))
            art.workloads.append(Workload("worker", "worker", "deployment", ["worker"]))

    # -- schedules -------------------------------------------------------------
    for s in spec.schedules:
        job_pod = pod(invoke_args(s.invoke), "cronjob", restart="Never")
        items.append({"apiVersion": "batch/v1", "kind": "CronJob",
                      "metadata": {"name": f"{app}-{s.name}", "namespace": ns, "labels": labels},
                      "spec": {"schedule": s.cron, "timeZone": s.timezone, "concurrencyPolicy": "Forbid",
                               "startingDeadlineSeconds": 300, "successfulJobsHistoryLimit": 1, "failedJobsHistoryLimit": 3,
                               "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": s.timeout_s,
                                                        "template": job_pod}}}})
        art.schedules.append(RenderedSchedule(s.name, s.cron, s.timezone, s.invoke, overlap_guard="platform"))

    art.files["manifests.json"] = json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2) + "\n"
    art.notes.append(f"Create the Secret first: kubectl -n {ns} create secret generic {k.secret_name} --from-env-file=.env")
    return art


def _keda_metadata(scheme: str, url: str, extra_env: dict) -> dict:
    from urllib.parse import parse_qs, urlparse

    p = urlparse(url)
    if scheme == "sqs":
        return {"queueURL": f"https://{p.netloc}{p.path}", "queueLength": "1",
                "awsRegion": extra_env.get("AWS_REGION", p.netloc.split(".")[1] if p.netloc.count(".") > 2 else "")}
    if scheme == "servicebus":
        return {"queueName": p.path.strip("/"), "namespace": p.netloc.split(".")[0], "messageCount": "1"}
    sub = parse_qs(p.query).get("subscription", [f"{p.path.strip('/')}-worker"])[0]
    return {"subscriptionName": f"projects/{p.netloc}/subscriptions/{sub}", "mode": "SubscriptionSize", "value": "1"}
