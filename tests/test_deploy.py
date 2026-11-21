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

"""The provisioning plane: spec, validation, renderers, conformance (including negative tests)."""

import json
from pathlib import Path

import pytest

from skyctl.deploy import check, load_spec, render, validate, validate_all
from skyctl.deploy.render.base import check_references
from skyctl.deploy.spec import DeploySpec

EXAMPLES = Path(__file__).resolve().parents[1] / "deploy" / "examples"
EXAMPLE = EXAMPLES / "managed.deploy.toml"
K8S_EXAMPLE = EXAMPLES / "kubernetes.deploy.toml"


@pytest.fixture
def spec() -> DeploySpec:
    return load_spec(EXAMPLE)


@pytest.fixture
def k8s_spec() -> DeploySpec:
    return load_spec(K8S_EXAMPLE)


def rendered(spec):
    return {t: render(spec, t) for t in spec.targets.names()}


def failing(checks, name=None, target=None):
    return [
        c for c in checks if not c.ok and (name is None or c.name == name) and (target is None or c.target == target)
    ]


# ----------------------------------------------------------------------
# Spec
# ----------------------------------------------------------------------


def test_example_spec_loads_validates_and_conforms(spec):
    assert spec.targets.names() == ["aws", "azure", "gcp", "kubernetes", "compose"]
    assert [f for f in validate_all(spec) if f.level == "error"] == []
    checks = check(spec, rendered(spec))
    assert failing(checks) == [], [str(c) for c in failing(checks)]
    assert len({c.name for c in checks}) == 12


def test_spec_rejects_bad_input():
    base = {"app": {"name": "skyctl", "image": "img"}}
    DeploySpec.model_validate(base)
    for bad in (
        {"app": {"name": "Sky_Ctl", "image": "x"}},
        {**base, "schedules": [{"name": "x", "cron": "not cron", "invoke": {"kind": "tick"}}]},
        {**base, "schedules": [{"name": "x", "cron": "* * * * *", "invoke": {"kind": "explode"}}]},
        {**base, "schedules": [{"name": "x", "cron": "* * * * *", "invoke": {"kind": "task.run", "task": "ghost"}}]},
        {**base, "tasks": {"t": {"goal": "g", "nonsense": 1}}},
        {**base, "surprise": True},
    ):
        with pytest.raises(ValueError):
            DeploySpec.model_validate(bad)


def test_tick_is_always_present():
    s = DeploySpec.model_validate({"app": {"name": "skyctl", "image": "img"}})
    assert [x.name for x in s.schedules] == ["tick"]


def test_tasks_roundtrip_through_the_rendered_tasks_file(spec, tmp_path):
    from skyctl.agent import load_tasks

    f = tmp_path / "tasks.toml"
    f.write_text(spec.tasks_toml())
    tasks = load_tasks(f)
    assert tasks["daily-engagement"].approval == "writes" and tasks["daily-engagement"].accounts == ["work"]


# ----------------------------------------------------------------------
# Validation: capabilities and limits
# ----------------------------------------------------------------------


def _with(spec, **changes):
    data = spec.model_dump()
    for path, value in changes.items():
        node = data
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value
    return DeploySpec.model_validate(data)


def errors(spec, target):
    return [f.message for f in validate(spec, target) if f.level == "error"]


def test_time_zones_are_rejected_only_where_unsupported(spec):
    data = spec.model_dump()
    data["schedules"][1]["timezone"] = "Europe/London"
    s = DeploySpec.model_validate(data)
    assert any("UTC only" in m for m in errors(s, "azure"))
    assert errors(s, "gcp") == [] and errors(s, "kubernetes") == [] and errors(s, "aws") == []


def test_lambda_limits(spec):
    s = _with(spec, targets__aws__compute="lambda", service__enabled=False)
    msgs = errors(s, "aws")
    assert msgs == []  # the example image is already ECR
    s2 = _with(s, app__image="ghcr.io/me/skyctl:1")
    assert any("ECR" in m for m in errors(s2, "aws"))
    data = s.model_dump()
    data["schedules"][1]["timeout_s"] = 1800
    assert any("stop at 900s" in m for m in errors(DeploySpec.model_validate(data), "aws"))
    assert any("not rendered" in m for m in errors(_with(s, service__enabled=True), "aws"))


def test_run_time_must_fit_delivery_deadlines(spec):
    s = _with(spec, policy__max_run_seconds=1200)
    assert any("Pub/Sub push ack deadline" in m for m in errors(s, "gcp"))
    assert errors(s, "azure") == []  # the worker heartbeat renews Service Bus locks


def test_public_service_must_authenticate(spec):
    s = _with(spec, service__public=True, service__auth=["platform"])
    assert any("anyone could call it" in m for m in errors(s, "aws"))
    assert not any("anyone" in m for m in errors(s, "gcp"))  # Cloud Run IAM enforces it


def test_eventbridge_unrepresentable_cron(spec):
    data = spec.model_dump()
    data["schedules"][1]["cron"] = "0 9 1 * mon"
    assert any("two schedules" in m for m in errors(DeploySpec.model_validate(data), "aws"))


def test_kubernetes_requirements(spec):
    assert any("bucket" in m for m in errors(_with(spec, targets__kubernetes__state_url="file:///data"), "kubernetes"))
    assert any("identity" in m for m in errors(_with(spec, targets__kubernetes__identity=None), "kubernetes"))


# ----------------------------------------------------------------------
# Renderers
# ----------------------------------------------------------------------


def test_cron_dialects_per_target(spec):
    arts = rendered(spec)
    by_target = {t: {s.name: s.expression for s in a.schedules} for t, a in arts.items()}
    assert by_target["aws"]["daily-engagement"] == "cron(0 9 ? * MON-FRI *)"
    assert by_target["gcp"]["daily-engagement"] == "0 9 * * mon-fri"
    k8s = json.loads(arts["kubernetes"].files["manifests.json"])
    cron = next(
        i for i in k8s["items"] if i["kind"] == "CronJob" and i["metadata"]["name"] == "skyctl-daily-engagement"
    )
    assert cron["spec"]["concurrencyPolicy"] == "Forbid" and cron["spec"]["timeZone"] == "UTC"
    args = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args == [
        "invoke",
        '{"kind":"task.run","slot":{"cron":"0 9 * * mon-fri","timezone":"UTC"},"task":"daily-engagement"}',
    ]


def test_every_target_uses_native_identity_and_no_static_cloud_keys(spec):
    arts = rendered(spec)
    for target, art in arts.items():
        blob = "".join(art.files.values())
        for forbidden in ("AWS_SECRET_ACCESS_KEY", "AZURE_CLIENT_SECRET", "GOOGLE_APPLICATION_CREDENTIALS"):
            assert forbidden not in blob, (target, forbidden)
    k8s = json.loads(arts["kubernetes"].files["manifests.json"])
    sa = next(i for i in k8s["items"] if i["kind"] == "ServiceAccount")
    assert sa["metadata"]["annotations"] == {"eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/skyctl-eks"}
    assert any(i["kind"] == "ConfigMap" and i["metadata"]["name"] == "skyctl-env" for i in k8s["items"])
    scaled = next(i for i in k8s["items"] if i["kind"] == "ScaledJob")
    assert scaled["spec"]["triggers"][0]["type"] == "aws-sqs-queue"


def test_gcp_avoids_self_reference_cycle_with_deterministic_url(spec):
    gcp = render(spec, "gcp")
    svc_env = {
        e["name"]: e.get("value")
        for e in gcp.terraform["resource"]["google_cloud_run_v2_service"]["svc"]["template"]["containers"][0]["env"]
    }
    assert "google_cloud_run_v2_service.svc" not in json.dumps(svc_env)
    assert svc_env["SKYCTL_DELAYED_URL"].startswith(
        "cloudtasks://my-skyctl-project/europe-west1/skyctl-delayed?url=https://skyctl-svc-"
    )
    assert svc_env["SKYCTL_AUTH"].startswith("google-oidc")


def test_aws_lambda_variant(spec):
    s = _with(spec, targets__aws__compute="lambda", service__enabled=False)
    art = render(s, "aws")
    fn = art.terraform["resource"]["aws_lambda_function"]["skyctl"]
    assert "AWS_REGION" not in fn["environment"]["variables"]  # reserved on Lambda
    esm = art.terraform["resource"]["aws_lambda_event_source_mapping"]["work"]
    assert esm["function_response_types"] == ["ReportBatchItemFailures"]
    assert art.terraform["resource"]["aws_sqs_queue"]["work"]["visibility_timeout_seconds"] >= 6 * fn["timeout"]
    assert failing(check(s, {"aws": art})) == []


def test_compose_substitutes_missing_capabilities(spec):
    art = render(spec, "compose")
    doc = json.loads(art.files["compose.json"])
    daemon = doc["services"]["daemon"]
    assert daemon["command"] == ["daemon"]
    schedules = json.loads(daemon["environment"]["SKYCTL_SCHEDULES_JSON"])
    assert [x["name"] for x in schedules] == ["tick", "daily-engagement"]
    assert schedules[1]["invoke"]["slot"] == {"cron": "0 9 * * mon-fri", "timezone": "UTC"}
    assert doc["services"]["http"]["ports"] == ["127.0.0.1:8080:8080"]


@pytest.mark.parametrize("example", ["managed", "kubernetes"])
def test_generated_examples_are_up_to_date(example):
    """deploy/generated/<example>/ is rendered from deploy/examples/<example>.deploy.toml."""
    src = EXAMPLES / f"{example}.deploy.toml"
    root = EXAMPLES.parent / "generated" / example
    for target, art in rendered(load_spec(src)).items():
        for name, content in art.files.items():
            path = root / target / name
            assert path.exists(), f"missing {path}; run: skyctl deploy render {src} --all --out {root}"
            assert path.read_text() == content, f"{path} is stale; run: skyctl deploy render {src} --all --out {root}"


# ----------------------------------------------------------------------
# Conformance can fail (mutation tests)
# ----------------------------------------------------------------------


def test_conformance_catches_dangling_terraform_references(spec):
    arts = rendered(spec)
    arts["gcp"].terraform["resource"]["google_storage_bucket_iam_member"]["runtime"]["bucket"] = (
        "${google_storage_bucket.nope.name}"
    )
    assert failing(check(spec, arts), "terraform.references", "gcp")
    assert check_references({"resource": {"x_y": {"a": {"v": "${upper(var.q)}"}}}}) != []


def test_conformance_catches_policy_drift(spec):
    arts = rendered(spec)
    arts["azure"].env["SKYCTL_AGENT_MAX_WRITES"] = "500"
    assert len(failing(check(spec, arts), "env.policy-parity")) == 5


def test_conformance_catches_missing_schedule_and_env_key(spec):
    arts = rendered(spec)
    arts["aws"].schedules.pop()
    del arts["kubernetes"].env["SKYCTL_QUEUE_URL"]
    checks = check(spec, arts)
    assert failing(checks, "schedules.complete", "aws")
    assert failing(checks, "env.contract", "kubernetes")


def test_conformance_catches_broad_iam(spec):
    arts = rendered(spec)
    policy = arts["aws"].terraform["resource"]["aws_iam_role_policy"]["task"]
    doc = json.loads(policy["policy"])
    doc["Statement"].append({"Effect": "Allow", "Action": "s3:*", "Resource": "*"})
    policy["policy"] = json.dumps(doc)
    arts["gcp"].terraform["resource"]["google_cloud_run_v2_service_iam_member"]["invoker"]["member"] = "allUsers"
    arts["azure"].terraform["resource"]["azurerm_role_assignment"]["blob_app"]["role_definition_name"] = "Owner"
    checks = check(spec, arts)
    for target in ("aws", "gcp", "azure"):
        assert failing(checks, "iam.least-privilege", target), target


def test_conformance_catches_exposure_and_secret_leaks(spec):
    arts = rendered(spec)
    arts["compose"].public_endpoints.append("host:8080")
    arts["aws"].terraform["variable"]["secrets_json"].pop("sensitive")
    arts["gcp"].files["main.tf.json"] += "abcd-efgh-ijkl-mnop"
    checks = check(spec, arts)
    assert failing(checks, "exposure", "compose")
    assert failing(checks, "secrets.handling", "aws")
    assert failing(checks, "secrets.handling", "gcp")


def test_conformance_catches_non_cas_state_and_foreign_resources(spec):
    arts = rendered(spec)
    arts["kubernetes"].env["SKYCTL_STATE_URL"] = "sftp://host/state"
    arts["aws"].terraform["resource"]["google_storage_bucket"] = {"oops": {}}
    checks = check(spec, arts)
    assert failing(checks, "state.cas", "kubernetes")
    assert failing(checks, "terraform.providers", "aws")


def test_common_env_cannot_be_overridden_by_a_renderer(spec):
    from skyctl.deploy.render.base import common_env

    with pytest.raises(ValueError, match="override"):
        common_env(
            spec,
            "aws",
            platform="aws",
            compute="ecs",
            secrets="env",
            state_url="s3://b",
            extra={"SKYCTL_AGENT_MAX_WRITES": "999"},
        )


# ----------------------------------------------------------------------
# Decision 1: the state bucket is always provisioned and protected
# ----------------------------------------------------------------------


def test_every_cloud_target_provisions_a_protected_versioned_state_bucket(spec, k8s_spec):
    for s in (spec, k8s_spec):
        checks = check(s, rendered(s))
        protected = [c for c in checks if c.name == "state.protected"]
        assert {c.target for c in protected} == {"aws", "azure", "gcp"} and all(c.ok for c in protected), protected


def test_state_protected_check_can_fail(spec):
    arts = rendered(spec)
    del arts["aws"].terraform["resource"]["aws_s3_bucket"]["state"]["lifecycle"]
    arts["gcp"].terraform["resource"]["google_storage_bucket"]["state"]["versioning"]["enabled"] = False
    del arts["azure"].terraform["resource"]["azurerm_storage_account"]
    checks = check(spec, arts)
    assert failing(checks, "state.protected", "aws") and failing(checks, "state.protected", "gcp")
    assert "not provisioned" in failing(checks, "state.protected", "azure")[0].detail


def test_protection_can_be_turned_off_deliberately(spec):
    s = _with(spec, state__protect=False)
    bucket = render(s, "aws").terraform["resource"]["aws_s3_bucket"]["state"]
    assert "lifecycle" not in bucket and failing(check(s, rendered(s))) == []


# ----------------------------------------------------------------------
# Decision 2: Azure uses Storage Queues
# ----------------------------------------------------------------------


def test_azure_uses_storage_queues_not_service_bus(spec):
    art = render(spec, "azure")
    res = art.terraform["resource"]
    assert not any(t.startswith("azurerm_servicebus") for t in res)
    assert set(res["azurerm_storage_queue"]) == {"work", "poison"}
    assert art.env["SKYCTL_QUEUE_URL"] == "azqueue://${azurerm_storage_account.state.name}/skyctl-work"
    assert art.env["SKYCTL_DELAYED_URL"] == "queue"
    # Entra-only: no shared keys, so nothing renders a connection string.
    assert res["azurerm_storage_account"]["state"]["shared_access_key_enabled"] is False
    assert "connection_string" not in art.files["main.tf.json"]
    # The Container Apps worker is a scheduled drain, not an event trigger.
    worker = res["azurerm_container_app_job"]["worker"]
    assert worker["schedule_trigger_config"]["cron_expression"] == "* * * * *"
    assert worker["template"]["container"][0]["args"] == ["worker", "--once"]
    assert "event_trigger_config" not in worker


def test_azure_poll_interval_is_configurable(spec):
    art = render(_with(spec, worker__poll_minutes=5), "azure")
    assert (
        art.terraform["resource"]["azurerm_container_app_job"]["worker"]["schedule_trigger_config"]["cron_expression"]
        == "*/5 * * * *"
    )


# ----------------------------------------------------------------------
# Decision 3: managed serverless OR Kubernetes on each cloud
# ----------------------------------------------------------------------


def test_kubernetes_example_validates_and_conforms(k8s_spec):
    assert [k8s_spec.compute_for(t) for t in k8s_spec.targets.names()] == ["eks", "aks", "gke"]
    assert [f for f in validate_all(k8s_spec) if f.level == "error"] == []
    assert failing(check(k8s_spec, rendered(k8s_spec))) == []


def _backing(art, types):
    return {t: art.terraform["resource"].get(t) for t in types}


@pytest.mark.parametrize(
    ("target", "types"),
    [
        (
            "aws",
            [
                "aws_s3_bucket",
                "aws_s3_bucket_versioning",
                "aws_s3_bucket_lifecycle_configuration",
                "aws_s3_bucket_server_side_encryption_configuration",
                "aws_s3_bucket_public_access_block",
                "aws_s3_object",
                "aws_sqs_queue",
                "aws_secretsmanager_secret",
                "aws_scheduler_schedule_group",
            ],
        ),
        (
            "azure",
            [
                "azurerm_storage_account",
                "azurerm_storage_container",
                "azurerm_storage_queue",
                "azurerm_storage_management_policy",
                "azurerm_storage_blob",
                "azurerm_key_vault",
                "azurerm_key_vault_secret",
            ],
        ),
        (
            "gcp",
            [
                "google_storage_bucket",
                "google_storage_bucket_object",
                "google_pubsub_topic",
                "google_secret_manager_secret",
            ],
        ),
    ],
)
def test_backing_services_are_identical_in_both_compute_options(spec, k8s_spec, target, types):
    """Choosing Kubernetes or managed serverless must not change the state, queue or secrets IaC."""
    managed, k8s = render(spec, target), render(k8s_spec, target)
    assert _backing(managed, types) == _backing(k8s, types)
    for key in ("SKYCTL_STATE_URL", "SKYCTL_AGENT_MAX_WRITES", "SKYCTL_AGENT_ACCOUNTS"):
        assert managed.env[key] == k8s.env[key], key
    # Same queue. The one intended difference: on GCP, Cloud Run consumes through a
    # push subscription and GKE through a pull subscription on the same topic.
    topic = lambda url: url.split("?", 1)[0]
    assert topic(managed.env["SKYCTL_QUEUE_URL"]) == topic(k8s.env["SKYCTL_QUEUE_URL"])
    if target != "gcp":
        assert managed.env["SKYCTL_QUEUE_URL"] == k8s.env["SKYCTL_QUEUE_URL"]


def test_k8s_modes_bind_cloud_values_without_cluster_credentials(k8s_spec):
    for target in ("aws", "azure", "gcp"):
        art = render(k8s_spec, target)
        res = art.terraform["resource"]
        assert "local_file" in res and not any(t.startswith("kubernetes_") for t in res)  # no cluster creds needed
        bind = json.loads(res["local_file"]["k8s_bind"]["content"])
        kinds = [i["kind"] for i in bind["items"]]
        assert kinds[:3] == ["Namespace", "ServiceAccount", "ConfigMap"]
        assert {"TriggerAuthentication", "ScaledJob"} <= set(kinds)
        cm = next(i for i in bind["items"] if i["kind"] == "ConfigMap")["data"]
        assert cm["SKYCTL_PLATFORM"] == "kubernetes" and cm["SKYCTL_STATE_URL"] == art.env["SKYCTL_STATE_URL"]
        # Static manifests carry no Terraform references.
        assert "${" not in art.files["manifests.json"]
        cron = next(i for i in json.loads(art.files["manifests.json"])["items"] if i["kind"] == "CronJob")
        assert cron["spec"]["concurrencyPolicy"] == "Forbid"
        assert all(s.overlap_guard == "platform" for s in art.schedules)


def test_k8s_identity_per_cloud(k8s_spec):
    aws = render(k8s_spec, "aws").terraform["resource"]
    assert aws["aws_eks_pod_identity_association"]["app"]["service_account"] == "skyctl"
    irsa = render(
        _with(
            k8s_spec,
            targets__aws__kubernetes__identity="irsa",
            targets__aws__kubernetes__oidc_provider_arn="arn:aws:iam::1:oidc-provider/oidc.eks.x/id/A",
            targets__aws__kubernetes__oidc_issuer="oidc.eks.x/id/A",
        ),
        "aws",
    ).terraform["resource"]
    assert "aws_eks_pod_identity_association" not in irsa
    trust = json.loads(irsa["aws_iam_role"]["app"]["assume_role_policy"])["Statement"][0]
    assert trust["Condition"]["StringEquals"]["oidc.eks.x/id/A:sub"] == "system:serviceaccount:skyctl:skyctl"
    bind = json.loads(irsa["local_file"]["k8s_bind"]["content"])
    sa = next(i for i in bind["items"] if i["kind"] == "ServiceAccount")
    assert sa["metadata"]["annotations"] == {"eks.amazonaws.com/role-arn": "${aws_iam_role.app.arn}"}

    az = render(k8s_spec, "azure").terraform["resource"]
    assert (
        az["azurerm_federated_identity_credential"]["k8s_app"]["issuer"]
        == "${data.azurerm_kubernetes_cluster.aks.oidc_issuer_url}"
    )
    gcp = render(k8s_spec, "gcp").terraform["resource"]
    assert gcp["google_service_account_iam_member"]["workload_identity"]["role"] == "roles/iam.workloadIdentityUser"
    assert "google_cloud_run_v2_service" not in gcp and "push_config" not in gcp["google_pubsub_subscription"]["work"]


def test_k8s_mode_fetches_the_http_token_from_the_cloud_secret_store(k8s_spec):
    for target, expected in (
        ("aws", "aws-sm://skyctl/http-token#SKYCTL_HTTP_TOKEN"),
        ("gcp", "gcp-sm://my-skyctl-project/skyctl-http-token#SKYCTL_HTTP_TOKEN"),
    ):
        assert expected in render(k8s_spec, target).env["SKYCTL_SECRETS"]
    aws = render(k8s_spec, "aws").terraform["resource"]
    policy = json.loads(aws["aws_iam_role_policy"]["app"]["policy"])
    assert policy["Statement"][0]["Resource"] == [
        "${aws_secretsmanager_secret.config.arn}",
        "${aws_secretsmanager_secret.http_token.arn}",
    ]


def test_k8s_validation(k8s_spec):
    assert any(
        "oidc_provider_arn" in m for m in errors(_with(k8s_spec, targets__aws__kubernetes__identity="irsa"), "aws")
    )
    warn = [f.message for f in validate(_with(k8s_spec, targets__aws__kubernetes__keda_operator_role_arn=None), "aws")]
    assert any("KEDA operator" in m for m in warn)
    assert any("tick catch-up" in f.message for f in validate(k8s_spec, "gcp"))
    # Time zones are fine on every Kubernetes target, including Azure's.
    data = k8s_spec.model_dump()
    data["schedules"][1]["timezone"] = "Europe/London"
    assert errors(DeploySpec.model_validate(data), "azure") == []
    with pytest.raises(ValueError, match=r"targets\.gcp\.kubernetes"):
        DeploySpec.model_validate(
            {**k8s_spec.model_dump(), "targets": {"gcp": {"project": "p", "region": "r", "compute": "gke"}}}
        )
