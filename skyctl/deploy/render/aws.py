"""
skyctl.deploy.render.aws
========================
DeploySpec → Terraform JSON for AWS.

Backing services (identical for every compute mode, see aws_backing()):
secrets in Secrets Manager; the state bucket (S3, CAS via If-Match, KMS,
versioned, lifecycle on runs/ and ledger/, prevent_destroy); SQS work queue
+ DLQ; EventBridge Scheduler group for delayed delivery; one IAM policy.

    intent          compute = "ecs"                    "lambda"                          "eks" (existing cluster)
    --------------  ---------------------------------  --------------------------------  -------------------------------
    identity        ECS task role                      Lambda role                       EKS Pod Identity (or IRSA)
    HTTP token      ECS native secret injection        (no HTTP service)                 fetched: …/http-token#SKYCTL_HTTP_TOKEN
    worker          ECS service 0..N on queue depth    SQS event source mapping          Deployment or KEDA ScaledJob
    schedules       EventBridge Scheduler → RunTask    EventBridge Scheduler → Lambda    CronJob (Forbid, timeZone)
    HTTP service    ECS behind an ALB (HTTPS)          —                                 Deployment + Service (+ Ingress)

Gotchas handled here, each worth a comment because each has bitten someone:
* ECS `secrets` are fetched by the *execution* role, not the task role.
* Lambda rejects reserved env keys such as AWS_REGION.
* Lambda only pulls images from ECR (checked in validate).
* SQS visibility must exceed the Lambda timeout (AWS recommends 6×).
* Security groups use the standalone rule resources: inline `ingress` blocks in
  Terraform JSON require every optional attribute to be spelled out.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from ...platform.cron import CronSpec, to_eventbridge
from ..spec import DeploySpec
from .base import Artifact, RenderedSchedule, TfDoc, Workload, common_env, iam_json, invoke_args, ref
from .k8s_common import KedaTrigger, build_k8s, emit_k8s

CONTAINER = "skyctl"


def _assume(service: str) -> str:
    return iam_json([{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}])


@dataclass
class AwsBacking:
    """The backing services every compute mode shares."""
    secrets_url: str
    state_url: str
    queue_url: str
    delayed_url: str
    bucket: str
    queue: str
    token_secret: Optional[str]
    statements: list


def aws_backing(spec: DeploySpec, tf: TfDoc, account_id: str, *, fetch_token: bool, visibility: int) -> AwsBacking:
    t = spec.targets.aws
    app = spec.app.name

    # -- secrets -----------------------------------------------------------
    secret = tf.resource("aws_secretsmanager_secret", "config", {"name": f"{app}/config", "recovery_window_in_days": 7})
    tf.resource("aws_secretsmanager_secret_version", "config", {"secret_id": ref(secret, "id"), "secret_string": "${var.secrets_json}"})
    token_secret = None
    if spec.service.enabled and "bearer" in spec.service.auth:
        token_secret = tf.resource("aws_secretsmanager_secret", "http_token", {"name": f"{app}/http-token", "recovery_window_in_days": 7})
        tf.resource("aws_secretsmanager_secret_version", "http_token", {"secret_id": ref(token_secret, "id"), "secret_string": "${var.http_token}"})
    secrets_url = f"aws-sm://{app}/config"
    if token_secret and fetch_token:
        secrets_url += f",aws-sm://{app}/http-token#SKYCTL_HTTP_TOKEN"

    # -- state bucket: one JSON document (queue.json) + sessions/ runs/ ledger/ config/ ----
    bucket_body = {"bucket_prefix": f"{app}-state-"}
    if spec.state.protect:
        bucket_body["lifecycle"] = {"prevent_destroy": True}
    bucket = tf.resource("aws_s3_bucket", "state", bucket_body)
    tf.resource("aws_s3_bucket_public_access_block", "state", {
        "bucket": ref(bucket, "id"), "block_public_acls": True, "block_public_policy": True,
        "ignore_public_acls": True, "restrict_public_buckets": True,
    })
    tf.resource("aws_s3_bucket_server_side_encryption_configuration", "state", {
        "bucket": ref(bucket, "id"),
        "rule": {"apply_server_side_encryption_by_default": {"sse_algorithm": "aws:kms"}},
    })
    if spec.state.versioning:
        tf.resource("aws_s3_bucket_versioning", "state", {"bucket": ref(bucket, "id"), "versioning_configuration": {"status": "Enabled"}})
    tf.resource("aws_s3_bucket_lifecycle_configuration", "state", {
        "bucket": ref(bucket, "id"),
        "rule": [
            {"id": "expire-run-transcripts", "status": "Enabled", "filter": {"prefix": "skyctl/runs/"},
             "expiration": {"days": spec.state.retention_days}},
            {"id": "expire-write-ledger", "status": "Enabled", "filter": {"prefix": "skyctl/ledger/"},
             "expiration": {"days": spec.state.retention_days}},
            {"id": "expire-old-versions", "status": "Enabled", "filter": {},
             "noncurrent_version_expiration": {"noncurrent_days": 30}},
        ],
    })
    if spec.tasks:
        tf.resource("aws_s3_object", "tasks", {
            "bucket": ref(bucket, "id"), "key": "skyctl/config/tasks.toml",
            "content": spec.tasks_toml(), "content_type": "application/toml",
        })

    # -- queue + delayed delivery -------------------------------------------
    dlq = tf.resource("aws_sqs_queue", "dlq", {"name": f"{app}-work-dlq", "message_retention_seconds": 1_209_600,
                                                "sqs_managed_sse_enabled": True})
    queue = tf.resource("aws_sqs_queue", "work", {
        "name": f"{app}-work",
        "visibility_timeout_seconds": visibility,
        "sqs_managed_sse_enabled": True,
        "redrive_policy": json.dumps({"deadLetterTargetArn": ref(dlq, "arn"), "maxReceiveCount": spec.worker.max_attempts}),
    })
    delayed_url = "none"
    group = f"{app}-delayed"
    if spec.runtime.delayed_delivery:
        tf.resource("aws_scheduler_schedule_group", "delayed", {"name": group})
        delivery = tf.resource("aws_iam_role", "delivery", {"name": f"{app}-delivery", "assume_role_policy": _assume("scheduler.amazonaws.com")})
        tf.resource("aws_iam_role_policy", "delivery", {
            "name": "send-to-work-queue", "role": ref(delivery, "id"),
            "policy": iam_json([{"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": ref(queue, "arn")}]),
        })
        delayed_url = f"eventbridge://{group}?target={ref(queue, 'arn')}&role={ref(delivery, 'arn')}"

    # -- what the workload may do -------------------------------------------
    secret_arns = [ref(secret, "arn")] + ([ref(token_secret, "arn")] if token_secret and fetch_token else [])
    statements = [
        {"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": secret_arns if len(secret_arns) > 1 else secret_arns[0]},
        {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": f"{ref(bucket, 'arn')}/*"},
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": ref(bucket, "arn")},
        {"Effect": "Allow", "Action": ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage",
                                       "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"], "Resource": ref(queue, "arn")},
    ]
    if spec.runtime.delayed_delivery:
        statements += [
            {"Effect": "Allow", "Action": ["scheduler:CreateSchedule", "scheduler:UpdateSchedule", "scheduler:DeleteSchedule"],
             "Resource": f"arn:aws:scheduler:{t.region}:{account_id}:schedule/{group}/*"},
            {"Effect": "Allow", "Action": "iam:PassRole", "Resource": ref("aws_iam_role.delivery", "arn")},
        ]
    if spec.model_for("aws").startswith("bedrock:"):
        # Model and inference-profile ARNs vary by region; narrow this once you pin a model.
        statements.append({"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": "*"})

    return AwsBacking(
        secrets_url=secrets_url, state_url=f"s3://{ref(bucket, 'bucket')}/skyctl",
        queue_url=f"sqs://sqs.{t.region}.amazonaws.com/{account_id}/{app}-work", delayed_url=delayed_url,
        bucket=bucket, queue=queue, token_secret=token_secret, statements=statements,
    )


def render_aws(spec: DeploySpec) -> Artifact:
    t = spec.targets.aws
    art = Artifact("aws")
    providers = {"aws": {"source": "hashicorp/aws", "version": ">= 5.60"}}
    if t.compute == "eks":
        providers["local"] = {"source": "hashicorp/local", "version": ">= 2.4"}
    tf = TfDoc(providers, {"aws": {"region": t.region}})
    image = tf.variable("image", default=spec.app.image, description="Container image (ECR for Lambda).")
    tf.variable("secrets_json", sensitive=True, description="The BSKY_* / API-key map as JSON or dotenv text.")
    if spec.service.enabled and "bearer" in spec.service.auth:
        tf.variable("http_token", sensitive=True, description="Bearer token for the HTTP service.")
    account_id = ref(tf.data("aws_caller_identity", "current"), "account_id")

    max_timeout = max([s.timeout_s for s in spec.schedules] + [spec.policy.max_run_seconds + 60])
    visibility = spec.worker.visibility_seconds
    if t.compute == "lambda":
        visibility = max(visibility, min(6 * min(max_timeout, 900), 43_200))
    backing = aws_backing(spec, tf, account_id, fetch_token=t.compute == "eks", visibility=visibility)

    k8s = t.compute == "eks"
    env = common_env(
        spec, "aws", platform="kubernetes" if k8s else "aws",
        compute="deployment" if k8s else t.compute, secrets=backing.secrets_url,
        state_url=backing.state_url, queue_url=backing.queue_url, delayed_url=backing.delayed_url,
        auth=list(spec.service.auth),
        extra={} if t.compute == "lambda" else {"AWS_REGION": t.region},
    )
    art.env = env
    app_policy = iam_json(backing.statements)

    if t.compute == "lambda":
        _render_lambda(spec, tf, art, image, env, app_policy, backing.queue, max_timeout)
    elif t.compute == "eks":
        _render_eks(spec, tf, art, env, app_policy, backing)
    else:
        _render_ecs(spec, tf, art, image, env, app_policy, backing.queue, backing.token_secret, account_id)

    tf.output("env", env)
    tf.output("state_url", backing.state_url)
    tf.output("queue_url", ref(backing.queue, "url"))
    art.terraform = tf.doc
    art.files["main.tf.json"] = tf.to_json()
    return art


# ----------------------------------------------------------------------
# EKS (existing cluster)
# ----------------------------------------------------------------------

def _render_eks(spec, tf: TfDoc, art: Artifact, env, app_policy, backing: AwsBacking):
    t = spec.targets.aws
    k = t.kubernetes
    app = spec.app.name
    subject = f"system:serviceaccount:{k.namespace}:{app}"
    if k.identity == "irsa":
        trust = [{"Effect": "Allow", "Principal": {"Federated": k.oidc_provider_arn}, "Action": "sts:AssumeRoleWithWebIdentity",
                  "Condition": {"StringEquals": {f"{k.oidc_issuer}:sub": subject, f"{k.oidc_issuer}:aud": "sts.amazonaws.com"}}}]
    else:
        trust = [{"Effect": "Allow", "Principal": {"Service": "pods.eks.amazonaws.com"}, "Action": ["sts:AssumeRole", "sts:TagSession"]}]
    if k.keda and k.keda_operator_role_arn:
        # KEDA reads queue depth by assuming this role from its own identity.
        trust.append({"Effect": "Allow", "Principal": {"AWS": k.keda_operator_role_arn}, "Action": "sts:AssumeRole"})
    role = tf.resource("aws_iam_role", "app", {"name": f"{app}-eks", "assume_role_policy": iam_json(trust)})
    tf.resource("aws_iam_role_policy", "app", {"name": "skyctl", "role": ref(role, "id"), "policy": app_policy})
    annotations = None
    if k.identity == "pod-identity":
        tf.resource("aws_eks_pod_identity_association", "app", {
            "cluster_name": k.cluster_name, "namespace": k.namespace, "service_account": app, "role_arn": ref(role, "arn"),
        })
    else:
        annotations = {"eks.amazonaws.com/role-arn": ref(role, "arn")}
    keda = None
    if k.keda:
        keda = KedaTrigger("aws-sqs-queue",
                           {"queueURL": ref(backing.queue, "url"), "queueLength": "1", "awsRegion": t.region},
                           {"provider": "aws", "roleArn": ref(role, "arn")})
    emit_k8s(tf, art, build_k8s(
        spec, namespace=k.namespace, env=env, sa_annotations=annotations, keda=keda,
        ingress_host=k.ingress_host, ingress_class=k.ingress_class,
    ))


# ----------------------------------------------------------------------
# ECS on Fargate
# ----------------------------------------------------------------------

def _render_ecs(spec, tf: TfDoc, art: Artifact, image, env, app_policy, queue, token_secret, account_id):
    t = spec.targets.aws
    app = spec.app.name
    task_role = tf.resource("aws_iam_role", "task", {"name": f"{app}-task", "assume_role_policy": _assume("ecs-tasks.amazonaws.com")})
    tf.resource("aws_iam_role_policy", "task", {"name": "skyctl", "role": ref(task_role, "id"), "policy": app_policy})
    exec_role = tf.resource("aws_iam_role", "execution", {"name": f"{app}-execution", "assume_role_policy": _assume("ecs-tasks.amazonaws.com")})
    tf.resource("aws_iam_role_policy_attachment", "execution", {
        "role": ref(exec_role, "name"), "policy_arn": "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
    })
    if token_secret:
        # ECS resolves `secrets` with the EXECUTION role before the container starts.
        tf.resource("aws_iam_role_policy", "execution_secrets", {
            "name": "inject-http-token", "role": ref(exec_role, "id"),
            "policy": iam_json([{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": ref(token_secret, "arn")}]),
        })
    cluster = tf.resource("aws_ecs_cluster", "main", {"name": app, "setting": {"name": "containerInsights", "value": "enabled"}})
    logs = tf.resource("aws_cloudwatch_log_group", "main", {"name": f"/ecs/{app}", "retention_in_days": 30})

    def container(name_suffix: str, compute: str, command: list[str]) -> str:
        c = {
            "name": CONTAINER, "image": image, "essential": True, "command": command,
            "environment": [{"name": k, "value": v} for k, v in {**env, "SKYCTL_COMPUTE": compute}.items()],
            "readonlyRootFilesystem": True,
            "mountPoints": [{"sourceVolume": "tmp", "containerPath": "/tmp", "readOnly": False}],
            "stopTimeout": 120,
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": ref(logs, "name"), "awslogs-region": t.region, "awslogs-stream-prefix": name_suffix}},
        }
        if token_secret and name_suffix == "http":
            c["secrets"] = [{"name": "SKYCTL_HTTP_TOKEN", "valueFrom": ref(token_secret, "arn")}]
            c["portMappings"] = [{"containerPort": 8080, "protocol": "tcp"}]
        return json.dumps([c])

    def task_def(name: str, compute: str, command: list[str], cpu: int, memory: int) -> str:
        return tf.resource("aws_ecs_task_definition", name, {
            "family": f"{app}-{name}", "requires_compatibilities": ["FARGATE"], "network_mode": "awsvpc",
            "cpu": str(cpu), "memory": str(memory), "task_role_arn": ref(task_role, "arn"),
            "execution_role_arn": ref(exec_role, "arn"), "volume": [{"name": "tmp"}],
            "runtime_platform": {"operating_system_family": "LINUX", "cpu_architecture": "X86_64"},
            "container_definitions": container(name, compute, command),
        })

    network = {"subnets": t.subnet_ids, "security_groups": t.security_group_ids, "assign_public_ip": t.assign_public_ip}

    # HTTP service behind an ALB.
    if spec.service.enabled:
        svc = spec.service
        http_td = task_def("http", "ecs", ["serve"], int(svc.cpu * 1024), svc.memory_mb)
        alb_sg = tf.resource("aws_security_group", "alb", {"name": f"{app}-alb", "vpc_id": t.vpc_id})
        task_sg = tf.resource("aws_security_group", "task", {"name": f"{app}-task", "vpc_id": t.vpc_id})
        tf.resource("aws_vpc_security_group_ingress_rule", "alb_https", {
            "security_group_id": ref(alb_sg, "id"), "from_port": 443, "to_port": 443, "ip_protocol": "tcp",
            "cidr_ipv4": "0.0.0.0/0" if svc.public else "10.0.0.0/8",
        })
        tf.resource("aws_vpc_security_group_egress_rule", "alb_to_task", {
            "security_group_id": ref(alb_sg, "id"), "referenced_security_group_id": ref(task_sg, "id"),
            "from_port": 8080, "to_port": 8080, "ip_protocol": "tcp",
        })
        tf.resource("aws_vpc_security_group_ingress_rule", "task_from_alb", {
            "security_group_id": ref(task_sg, "id"), "referenced_security_group_id": ref(alb_sg, "id"),
            "from_port": 8080, "to_port": 8080, "ip_protocol": "tcp",
        })
        tf.resource("aws_vpc_security_group_egress_rule", "task_out", {
            "security_group_id": ref(task_sg, "id"), "cidr_ipv4": "0.0.0.0/0", "ip_protocol": "-1",
        })
        alb = tf.resource("aws_lb", "http", {
            "name": f"{app}-http"[:32], "internal": not svc.public, "load_balancer_type": "application",
            "security_groups": [ref(alb_sg, "id")], "subnets": t.subnet_ids, "drop_invalid_header_fields": True,
        })
        tg = tf.resource("aws_lb_target_group", "http", {
            "name": f"{app}-http"[:32], "port": 8080, "protocol": "HTTP", "target_type": "ip", "vpc_id": t.vpc_id,
            "health_check": {"path": "/healthz", "matcher": "200"},
        })
        tf.resource("aws_lb_listener", "https", {
            "load_balancer_arn": ref(alb, "arn"), "port": 443, "protocol": "HTTPS",
            "ssl_policy": "ELBSecurityPolicy-TLS13-1-2-2021-06", "certificate_arn": t.certificate_arn,
            "default_action": {"type": "forward", "target_group_arn": ref(tg, "arn")},
        })
        tf.resource("aws_ecs_service", "http", {
            "name": f"{app}-http", "cluster": ref(cluster, "id"), "task_definition": ref(http_td, "arn"),
            "desired_count": max(svc.min_instances, 1), "launch_type": "FARGATE",
            "network_configuration": {**network, "security_groups": t.security_group_ids + [ref(task_sg, "id")]},
            "load_balancer": {"target_group_arn": ref(tg, "arn"), "container_name": CONTAINER, "container_port": 8080},
            "depends_on": ["aws_lb_listener.https"],
        })
        tf.output("service_url", f"https://{ref(alb, 'dns_name')}")
        art.workloads.append(Workload("http", "service", "ecs", ["serve"]))
        if svc.public:
            art.public_endpoints.append("alb:443")

    # Worker: 0..N tasks on queue depth.
    if spec.worker.enabled:
        worker_td = task_def("worker", "ecs", ["worker"], 512, 1024)
        worker = tf.resource("aws_ecs_service", "worker", {
            "name": f"{app}-worker", "cluster": ref(cluster, "id"), "task_definition": ref(worker_td, "arn"),
            "desired_count": 0, "launch_type": "FARGATE", "network_configuration": network,
            "lifecycle": {"ignore_changes": ["desired_count"]},
        })
        resource_id = f"service/{ref(cluster, 'name')}/{ref(worker, 'name')}"
        scaling = tf.resource("aws_appautoscaling_target", "worker", {
            "max_capacity": spec.worker.max_concurrency, "min_capacity": 0, "resource_id": resource_id,
            "scalable_dimension": "ecs:service:DesiredCount", "service_namespace": "ecs",
        })

        def policy(name: str, bound: dict, capacity: int) -> str:
            return tf.resource("aws_appautoscaling_policy", name, {
                "name": f"{app}-{name}", "policy_type": "StepScaling",
                "resource_id": ref(scaling, "resource_id"), "scalable_dimension": ref(scaling, "scalable_dimension"),
                "service_namespace": ref(scaling, "service_namespace"),
                "step_scaling_policy_configuration": {
                    "adjustment_type": "ExactCapacity", "cooldown": 60, "metric_aggregation_type": "Maximum",
                    "step_adjustment": [{**bound, "scaling_adjustment": capacity}],
                },
            })

        up = policy("worker_up", {"metric_interval_lower_bound": 0}, spec.worker.max_concurrency)
        down = policy("worker_down", {"metric_interval_upper_bound": 0}, 0)
        dims = {"QueueName": ref(queue, "name")}
        tf.resource("aws_cloudwatch_metric_alarm", "queue_has_work", {
            "alarm_name": f"{app}-queue-has-work", "namespace": "AWS/SQS", "metric_name": "ApproximateNumberOfMessagesVisible",
            "dimensions": dims, "statistic": "Maximum", "period": 60, "evaluation_periods": 1,
            "comparison_operator": "GreaterThanThreshold", "threshold": 0, "alarm_actions": [ref(up, "arn")],
        })
        # Scale in only when nothing is visible AND nothing is in flight; otherwise a
        # long agent run would be killed by scale-in as soon as the queue looked empty.
        tf.resource("aws_cloudwatch_metric_alarm", "queue_idle", {
            "alarm_name": f"{app}-queue-idle", "comparison_operator": "LessThanOrEqualToThreshold",
            "evaluation_periods": 15, "threshold": 0, "alarm_actions": [ref(down, "arn")],
            "metric_query": [
                {"id": "total", "expression": "visible + inflight", "label": "work", "return_data": True},
                {"id": "visible", "metric": {"namespace": "AWS/SQS", "metric_name": "ApproximateNumberOfMessagesVisible",
                                             "dimensions": dims, "period": 60, "stat": "Maximum"}},
                {"id": "inflight", "metric": {"namespace": "AWS/SQS", "metric_name": "ApproximateNumberOfMessagesNotVisible",
                                              "dimensions": dims, "period": 60, "stat": "Maximum"}},
            ],
        })
        art.workloads.append(Workload("worker", "worker", "ecs", ["worker"]))

    # Schedules: EventBridge Scheduler → ECS RunTask.
    if spec.schedules:
        job_td = task_def("job", "ecs", ["invoke", '{"kind":"tick"}'], 512, 1024)
        sched_role = tf.resource("aws_iam_role", "scheduler", {"name": f"{app}-scheduler", "assume_role_policy": _assume("scheduler.amazonaws.com")})
        tf.resource("aws_iam_role_policy", "scheduler", {
            "name": "run-jobs", "role": ref(sched_role, "id"),
            "policy": iam_json([
                {"Effect": "Allow", "Action": "ecs:RunTask", "Resource": f"{ref(job_td, 'arn_without_revision')}:*"},
                {"Effect": "Allow", "Action": "iam:PassRole", "Resource": [ref(task_role, "arn"), ref(exec_role, "arn")]},
            ]),
        })
        for s in spec.schedules:
            expr = to_eventbridge(CronSpec(s.cron, s.timezone))
            overrides = {"containerOverrides": [{"name": CONTAINER, "command": invoke_args(s.payload())}]}
            tf.resource("aws_scheduler_schedule", s.name.replace("-", "_"), {
                "name": f"{app}-{s.name}", "group_name": "default",
                "schedule_expression": expr, "schedule_expression_timezone": s.timezone,
                "flexible_time_window": {"mode": "OFF"},
                "target": {
                    "arn": ref(cluster, "arn"), "role_arn": ref(sched_role, "arn"), "input": json.dumps(overrides),
                    "retry_policy": {"maximum_retry_attempts": 2},
                    "ecs_parameters": {
                        "task_definition_arn": ref(job_td, "arn_without_revision"), "launch_type": "FARGATE",
                        "task_count": 1, "network_configuration": network,
                    },
                },
            })
            art.schedules.append(RenderedSchedule(s.name, expr, s.timezone, s.payload(), overlap_guard="app"))
        art.workloads.append(Workload("job", "job", "ecs", ["invoke", "<schedule payload>"]))


# ----------------------------------------------------------------------
# Lambda (same image)
# ----------------------------------------------------------------------

def _render_lambda(spec, tf: TfDoc, art: Artifact, image, env, app_policy, queue, max_timeout):
    app = spec.app.name
    role = tf.resource("aws_iam_role", "lambda", {"name": f"{app}-lambda", "assume_role_policy": _assume("lambda.amazonaws.com")})
    tf.resource("aws_iam_role_policy", "lambda", {"name": "skyctl", "role": ref(role, "id"), "policy": app_policy})
    tf.resource("aws_iam_role_policy_attachment", "lambda_logs", {
        "role": ref(role, "name"), "policy_arn": "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    })
    fn = tf.resource("aws_lambda_function", "skyctl", {
        "function_name": app, "role": ref(role, "arn"), "package_type": "Image", "image_uri": image,
        "timeout": min(max_timeout, 900), "memory_size": 1024,
        "image_config": {"entry_point": ["python", "-m", "awslambdaric"], "command": ["skyctl.platform.lambda_handler.handler"]},
        "environment": {"variables": env},
    })
    if spec.worker.enabled:
        tf.resource("aws_lambda_event_source_mapping", "work", {
            "event_source_arn": ref(queue, "arn"), "function_name": ref(fn, "arn"), "batch_size": 1,
            "function_response_types": ["ReportBatchItemFailures"],
            "scaling_config": {"maximum_concurrency": max(2, spec.worker.max_concurrency)},
        })
        art.workloads.append(Workload("worker", "worker", "lambda", ["sqs-event-source"]))
    if spec.schedules:
        sched_role = tf.resource("aws_iam_role", "scheduler", {"name": f"{app}-scheduler", "assume_role_policy": _assume("scheduler.amazonaws.com")})
        tf.resource("aws_iam_role_policy", "scheduler", {
            "name": "invoke-function", "role": ref(sched_role, "id"),
            "policy": iam_json([{"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": ref(fn, "arn")}]),
        })
        for s in spec.schedules:
            expr = to_eventbridge(CronSpec(s.cron, s.timezone))
            tf.resource("aws_scheduler_schedule", s.name.replace("-", "_"), {
                "name": f"{app}-{s.name}", "group_name": "default",
                "schedule_expression": expr, "schedule_expression_timezone": s.timezone,
                "flexible_time_window": {"mode": "OFF"},
                "target": {"arn": ref(fn, "arn"), "role_arn": ref(sched_role, "arn"), "input": json.dumps(s.payload()),
                           "retry_policy": {"maximum_retry_attempts": 2}},
            })
            art.schedules.append(RenderedSchedule(s.name, expr, s.timezone, s.payload(), overlap_guard="app"))
    art.workloads.append(Workload("function", "job", "lambda", ["skyctl.platform.lambda_handler.handler"]))
