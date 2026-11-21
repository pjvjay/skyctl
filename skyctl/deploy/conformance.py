"""
skyctl.deploy.conformance
=========================
Cross-target invariants. Rendering one spec for several targets must
produce deployments that behave the same where it matters; these checks
turn "equivalent by intention" into "equivalent by test".

    env.contract           every target sets every common SKYCTL_* key
    env.policy-parity      policy ceilings are identical on every target
    state.cas              state lives somewhere with atomic compare-and-swap
    schedules.complete     every schedule exists on every target, same invocation
    schedules.overlap      overlap is prevented by the platform or guarded by the app
    exposure               nothing is public unless service.public
    terraform.references   no dangling ${…} references
    terraform.providers    a target only uses its own provider's resources
    secrets.handling       secret inputs are sensitive; no secret-looking literals
    iam.least-privilege    no wildcard grants beyond an explicit allow-list; no public IAM members
    workloads.worker       the worker exists where the spec asks for one
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .render.base import COMMON_KEYS, POLICY_KEYS, Artifact, check_references
from .spec import DeploySpec

PROVIDER_PREFIXES = {"aws": ("aws_",), "azure": ("azurerm_", "random_"), "gcp": ("google_",)}
WILDCARD_ALLOWED_ACTIONS = {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"}
PROJECT_ROLES_ALLOWED = {"roles/aiplatform.user"}
_APP_PASSWORD = re.compile(r"\b[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}\b")
_CAS_SCHEMES = ("s3://", "az://", "gs://", "file://")


@dataclass
class Check:
    name: str
    target: str
    ok: bool
    detail: str = ""

    def __str__(self) -> str:
        return f"{'PASS' if self.ok else 'FAIL'} {self.name:<22} {self.target:<11} {self.detail}"


def _tf_resources(doc: dict):
    for type_, items in (doc.get("resource") or {}).items():
        for name, body in items.items():
            yield type_, name, body


def check(spec: DeploySpec, artifacts: dict[str, Artifact]) -> list[Check]:
    out: list[Check] = []
    add = lambda name, target, ok, detail="": out.append(Check(name, target, bool(ok), detail))  # noqa: E731

    # -- env ---------------------------------------------------------------
    policy_values = {}
    for target, art in artifacts.items():
        missing = [k for k in COMMON_KEYS if k not in art.env]
        add("env.contract", target, not missing, f"missing {missing}" if missing else f"{len(COMMON_KEYS)} keys")
        policy_values[target] = tuple(art.env.get(k) for k in POLICY_KEYS)
        state = art.env.get("SKYCTL_STATE_URL", "")
        add("state.cas", target, state.startswith(_CAS_SCHEMES), state)
    distinct = set(policy_values.values())
    for target in artifacts:
        add("env.policy-parity", target, len(distinct) <= 1,
            "identical ceilings on every target" if len(distinct) <= 1 else f"differs: {policy_values[target]}")

    # -- schedules -----------------------------------------------------------
    wanted = {(s.name, json.dumps(s.invoke, sort_keys=True)) for s in spec.schedules}
    for target, art in artifacts.items():
        got = {(s.name, json.dumps(s.invoke, sort_keys=True)) for s in art.schedules}
        add("schedules.complete", target, got == wanted,
            f"{len(got)} schedules" if got == wanted else f"missing {sorted(wanted - got)} extra {sorted(got - wanted)}")
        unguarded = [s.name for s in art.schedules if s.overlap_guard not in ("platform", "app")]
        app_guarded = [s.name for s in art.schedules if s.overlap_guard == "app"]
        add("schedules.overlap", target, not unguarded,
            "platform prevents overlap" if not app_guarded else f"app-guarded (claim protocol): {', '.join(app_guarded)}")

    # -- exposure --------------------------------------------------------------
    should_be_public = spec.service.enabled and spec.service.public
    for target, art in artifacts.items():
        ok = bool(art.public_endpoints) == should_be_public or (should_be_public and target == "kubernetes"
                                                                   and not spec.targets.kubernetes.ingress_host)
        add("exposure", target, ok, ", ".join(art.public_endpoints) or "no public endpoints")

    # -- terraform ---------------------------------------------------------
    for target, art in artifacts.items():
        if art.terraform is None:
            continue
        doc = art.terraform
        problems = check_references(doc)
        add("terraform.references", target, not problems, "; ".join(problems[:3]) or "all references resolve")
        prefixes = PROVIDER_PREFIXES[target]
        foreign = sorted({t for t, _, _ in _tf_resources(doc) if not t.startswith(prefixes)})
        add("terraform.providers", target, not foreign, f"foreign resource types {foreign}" if foreign else "")

        variables = doc.get("variable", {})
        insecure = [v for v in ("secrets_json", "http_token") if v in variables and not variables[v].get("sensitive")]
        literal = any(_APP_PASSWORD.search(content) for content in art.files.values())
        add("secrets.handling", target, not insecure and not literal,
            "sensitive inputs only" if not (insecure or literal) else f"insecure {insecure} literal={literal}")

        add("iam.least-privilege", target, *_iam_check(target, doc))

    # -- non-terraform secrets -----------------------------------------------
    for target, art in artifacts.items():
        if art.terraform is None:
            literal = any(_APP_PASSWORD.search(content) for content in art.files.values())
            add("secrets.handling", target, not literal, "secrets referenced, never embedded")

    # -- worker ------------------------------------------------------------
    for target, art in artifacts.items():
        has_worker = any(w.kind == "worker" for w in art.workloads)
        expected = spec.worker.enabled and not (target == "kubernetes" and not spec.targets.kubernetes.queue_url) and target != "compose"
        add("workloads.worker", target, has_worker or not expected,
            "worker present" if has_worker else ("MISSING" if expected else "no worker (substituted or not requested)"))
    return out


def _iam_check(target: str, doc: dict) -> tuple[bool, str]:
    issues = []
    if target == "aws":
        for type_, name, body in _tf_resources(doc):
            if type_ != "aws_iam_role_policy":
                continue
            for st in json.loads(body["policy"])["Statement"]:
                actions = st["Action"] if isinstance(st["Action"], list) else [st["Action"]]
                resources = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
                if "*" in actions or any(a.endswith(":*") for a in actions):
                    issues.append(f"{name}: wildcard action")
                if "*" in resources and not set(actions) <= WILDCARD_ALLOWED_ACTIONS:
                    issues.append(f"{name}: Resource * for {actions}")
    elif target == "azure":
        for type_, name, body in _tf_resources(doc):
            if type_ == "azurerm_role_assignment":
                scope = body["scope"]
                if re.fullmatch(r"/subscriptions/[^/]+/?", scope) or body.get("role_definition_name") in ("Owner", "Contributor"):
                    issues.append(f"{name}: broad scope or role")
    elif target == "gcp":
        for type_, name, body in _tf_resources(doc):
            member = str(body.get("member", ""))
            if member in ("allUsers", "allAuthenticatedUsers"):
                issues.append(f"{name}: public IAM member")
            if type_ == "google_project_iam_member" and body["role"] not in PROJECT_ROLES_ALLOWED:
                issues.append(f"{name}: project-wide {body['role']}")
    return (not issues, "; ".join(issues) or "scoped grants only")
