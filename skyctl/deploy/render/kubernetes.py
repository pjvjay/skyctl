"""
skyctl.deploy.render.kubernetes
===============================
The `kubernetes` target: any cluster, bring-your-own services. Nothing is
provisioned cloud-side; state/queue URLs and the identity already exist.

To have skyctl also create the bucket, queue, secrets and the
workload-identity binding on a cloud, use that cloud's target with
compute = "eks" | "aks" | "gke" instead (same manifests, via k8s_common).

    identity        ServiceAccount + workload identity annotation for `cloud`
                      aws   eks.amazonaws.com/role-arn (IRSA; Pod Identity needs none)
                      azure azure.workload.identity/client-id (+ pod label use=true)
                      gcp   iam.gke.io/gcp-service-account
    secrets         native injection: envFrom an existing Secret
    worker          Deployment, or KEDA ScaledJob (scale to zero) when keda = true
    schedules       CronJob with concurrencyPolicy: Forbid and spec.timeZone
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from ..spec import DeploySpec
from .base import Artifact, common_env
from .k8s_common import KedaTrigger, as_list, build_k8s

SA_ANNOTATION = {
    "aws": "eks.amazonaws.com/role-arn",
    "azure": "azure.workload.identity/client-id",
    "gcp": "iam.gke.io/gcp-service-account",
}
KEDA_PROVIDER = {"sqs": "aws", "servicebus": "azure-workload", "azqueue": "azure-workload", "pubsub": "gcp"}


def keda_trigger_for(queue_url: str, region: str = "") -> KedaTrigger:
    """KEDA trigger metadata for a queue URL (shared with the cloud renderers' k8s modes)."""
    p = urlparse(queue_url)
    scheme = p.scheme
    if scheme == "sqs":
        region = region or (p.netloc.split(".")[1] if p.netloc.count(".") > 2 else "")
        return KedaTrigger("aws-sqs-queue", {"queueURL": f"https://{p.netloc}{p.path}", "queueLength": "1", "awsRegion": region},
                           {"provider": "aws"})
    if scheme == "azqueue":
        return KedaTrigger("azure-queue", {"queueName": p.path.strip("/"), "accountName": p.netloc, "queueLength": "1"},
                           {"provider": "azure-workload"})
    if scheme == "servicebus":
        return KedaTrigger("azure-servicebus", {"queueName": p.path.strip("/"), "namespace": p.netloc.split(".")[0],
                                                "messageCount": "1"}, {"provider": "azure-workload"})
    if scheme == "pubsub":
        sub = parse_qs(p.query).get("subscription", [f"{p.path.strip('/')}-worker"])[0]
        return KedaTrigger("gcp-pubsub", {"subscriptionName": f"projects/{p.netloc}/subscriptions/{sub}",
                                          "mode": "SubscriptionSize", "value": "1"}, {"provider": "gcp"})
    raise ValueError(f"No KEDA trigger for queue scheme {scheme!r}")


def render_kubernetes(spec: DeploySpec) -> Artifact:
    k = spec.targets.kubernetes
    art = Artifact("kubernetes")
    env = common_env(
        spec, "kubernetes", platform="kubernetes", compute="deployment", secrets="env",
        state_url=k.state_url, queue_url=k.queue_url or "none", delayed_url=k.delayed_url or "none",
        extra={**({"SKYCTL_TASKS_FILE": "/etc/skyctl/tasks.toml"} if spec.tasks else {}), **k.extra_env},
    )
    art.env = env
    keda = keda_trigger_for(k.queue_url, k.extra_env.get("AWS_REGION", "")) if (k.keda and k.queue_url) else None
    plan = build_k8s(
        spec, namespace=k.namespace, env=env,
        sa_annotations={SA_ANNOTATION[k.cloud]: k.identity} if k.cloud in SA_ANNOTATION else None,
        pod_labels={"azure.workload.identity/use": "true"} if k.cloud == "azure" else None,
        secret_name=k.secret_name, tasks_configmap=True, keda=keda, has_queue=bool(k.queue_url),
        ingress_host=k.ingress_host, ingress_class=k.ingress_class,
    )
    art.workloads, art.schedules, art.public_endpoints = plan.workloads, plan.schedules, plan.public_endpoints
    art.files["manifests.json"] = json.dumps(as_list(plan.bind_items + plan.items), indent=2) + "\n"
    art.notes.append(f"Create the Secret first: kubectl -n {k.namespace} create secret generic {k.secret_name} --from-env-file=.env")
    return art
