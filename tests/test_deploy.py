"""The provisioning plane: spec, validation, renderers, conformance (including negative tests)."""

import copy
import json
from pathlib import Path

import pytest

from skyctl.deploy import check, load_spec, render, validate, validate_all
from skyctl.deploy.render.base import check_references
from skyctl.deploy.spec import DeploySpec

EXAMPLE = Path(__file__).resolve().parents[1] / "deploy" / "examples" / "skyctl.deploy.toml"


@pytest.fixture
def spec() -> DeploySpec:
    return load_spec(EXAMPLE)


def rendered(spec):
    return {t: render(spec, t) for t in spec.targets.names()}


def failing(checks, name=None, target=None):
    return [c for c in checks if not c.ok and (name is None or c.name == name) and (target is None or c.target == target)]


# ----------------------------------------------------------------------
# Spec
# ----------------------------------------------------------------------

def test_example_spec_loads_validates_and_conforms(spec):
    assert spec.targets.names() == ["aws", "azure", "gcp", "kubernetes", "compose"]
    assert [f for f in validate_all(spec) if f.level == "error"] == []
    checks = check(spec, rendered(spec))
    assert failing(checks) == [], [str(c) for c in failing(checks)]
    assert len({c.name for c in checks}) == 11


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
    assert msgs == []                                   # the example image is already ECR
    s2 = _with(s, app__image="ghcr.io/me/skyctl:1")
    assert any("ECR" in m for m in errors(s2, "aws"))
    data = s.model_dump()
    data["schedules"][1]["timeout_s"] = 1800
    assert any("stop at 900s" in m for m in errors(DeploySpec.model_validate(data), "aws"))
    assert any("not rendered" in m for m in errors(_with(s, service__enabled=True), "aws"))


def test_run_time_must_fit_delivery_deadlines(spec):
    s = _with(spec, policy__max_run_seconds=1200)
    assert any("Pub/Sub push ack deadline" in m for m in errors(s, "gcp"))
    assert errors(s, "azure") == []                     # the worker heartbeat renews Service Bus locks


def test_public_service_must_authenticate(spec):
    s = _with(spec, service__public=True, service__auth=["platform"])
    assert any("anyone could call it" in m for m in errors(s, "aws"))
    assert not any("anyone" in m for m in errors(s, "gcp"))     # Cloud Run IAM enforces it


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
    cron = next(i for i in k8s["items"] if i["kind"] == "CronJob" and i["metadata"]["name"] == "skyctl-daily-engagement")
    assert cron["spec"]["concurrencyPolicy"] == "Forbid" and cron["spec"]["timeZone"] == "UTC"
    args = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"]
    assert args == ["invoke", '{"kind":"task.run","task":"daily-engagement"}']


def test_every_target_uses_native_identity_and_no_static_cloud_keys(spec):
    arts = rendered(spec)
    for target, art in arts.items():
        blob = "".join(art.files.values())
        for forbidden in ("AWS_SECRET_ACCESS_KEY", "AZURE_CLIENT_SECRET", "GOOGLE_APPLICATION_CREDENTIALS"):
            assert forbidden not in blob, (target, forbidden)
    k8s = json.loads(arts["kubernetes"].files["manifests.json"])
    sa = next(i for i in k8s["items"] if i["kind"] == "ServiceAccount")
    assert sa["metadata"]["annotations"] == {"eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/skyctl-eks"}
    scaled = next(i for i in k8s["items"] if i["kind"] == "ScaledJob")
    assert scaled["spec"]["triggers"][0]["type"] == "aws-sqs-queue"


def test_gcp_avoids_self_reference_cycle_with_deterministic_url(spec):
    gcp = render(spec, "gcp")
    svc_env = {e["name"]: e.get("value") for e in
               gcp.terraform["resource"]["google_cloud_run_v2_service"]["svc"]["template"]["containers"][0]["env"]}
    assert "google_cloud_run_v2_service.svc" not in json.dumps(svc_env)
    assert svc_env["SKYCTL_DELAYED_URL"].startswith("cloudtasks://my-skyctl-project/europe-west1/skyctl-delayed?url=https://skyctl-svc-")
    assert svc_env["SKYCTL_AUTH"].startswith("google-oidc")


def test_aws_lambda_variant(spec):
    s = _with(spec, targets__aws__compute="lambda", service__enabled=False)
    art = render(s, "aws")
    fn = art.terraform["resource"]["aws_lambda_function"]["skyctl"]
    assert "AWS_REGION" not in fn["environment"]["variables"]           # reserved on Lambda
    esm = art.terraform["resource"]["aws_lambda_event_source_mapping"]["work"]
    assert esm["function_response_types"] == ["ReportBatchItemFailures"]
    assert art.terraform["resource"]["aws_sqs_queue"]["work"]["visibility_timeout_seconds"] >= 6 * fn["timeout"]
    assert failing(check(s, {"aws": art})) == []


def test_compose_substitutes_missing_capabilities(spec):
    art = render(spec, "compose")
    doc = json.loads(art.files["compose.json"])
    daemon = doc["services"]["daemon"]
    assert daemon["command"] == ["daemon"]
    assert [x["name"] for x in json.loads(daemon["environment"]["SKYCTL_SCHEDULES_JSON"])] == ["tick", "daily-engagement"]
    assert doc["services"]["http"]["ports"] == ["127.0.0.1:8080:8080"]


def test_generated_examples_are_up_to_date(spec):
    """deploy/generated/ is rendered from the example spec; regenerate with `skyctl deploy render`."""
    root = EXAMPLE.parents[1] / "generated"
    for target, art in rendered(spec).items():
        for name, content in art.files.items():
            path = root / target / name
            assert path.exists(), f"missing {path}; run: skyctl deploy render {EXAMPLE} --all --out deploy/generated"
            assert path.read_text() == content, f"{path} is stale; re-render it"


# ----------------------------------------------------------------------
# Conformance can fail (mutation tests)
# ----------------------------------------------------------------------

def test_conformance_catches_dangling_terraform_references(spec):
    arts = rendered(spec)
    arts["gcp"].terraform["resource"]["google_storage_bucket_iam_member"]["runtime"]["bucket"] = "${google_storage_bucket.nope.name}"
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
        common_env(spec, "aws", platform="aws", compute="ecs", secrets="env", state_url="s3://b",
                   extra={"SKYCTL_AGENT_MAX_WRITES": "999"})
