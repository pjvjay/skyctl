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

"""
skyctl.remote
=============
Run a free-text task *in* a cloud deployment from an operator's machine:

    skyctl run-cloud-task --cloud aws --description "Find a post about …, read the comments and …"

How it works (no HTTP service required):

    operator CLI ──task.run──► deployment work queue ──► cloud worker: agent in PLAN mode
         ▲                                                 │ transcript + plan → state bucket
         └────────── polls runs/<id>.jsonl, plans/<id> ◄───┘
    operator reviews the preview and approves (approval written to the bucket)
    operator CLI ──plan.execute──► work queue ──► cloud worker executes with ITS identity
         ▲                                         │ step results → plans/<id>
         └──────────────── polls plans/<id> ◄──────┘

The operator needs: send on the work queue, read/write on the state bucket
(see `operator` outputs in the generated Terraform). Never the Bluesky
account passwords: those stay in the deployment's secret store.

Connections:
    queue   state bucket + work queue (default for cloud profiles)
    http    the deployment's `skyctl serve` endpoint (SKYCTL_HTTP_TOKEN / SKYCTL_APPROVER_TOKEN)
    local   this machine (local settings), for development and demos

Profiles live in $SKYCTL_CONFIG_DIR/clouds.toml (default ~/.config/skyctl/),
in SKYCTL_CLOUD_<NAME>_{STATE_URL,QUEUE_URL,ENDPOINT} variables, or are
imported from `terraform output -json` with `skyctl cloud import`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .agent import TaskSpec
from .agent.runner import RunLog

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

TERMINAL_PLAN = ("completed", "failed", "dry_run", "cancelled")
_CITATION_HINT = re.compile(
    r"\b(news|facts?|sources?|cite|citations?|misinformation|disinformation|fact[- ]?check\w*)\b", re.I
)


class RemoteError(RuntimeError):
    """The deployment could not be reached or did not answer as expected."""


# ----------------------------------------------------------------------
# Profiles
# ----------------------------------------------------------------------


@dataclass
class CloudProfile:
    name: str
    platform: str = ""
    transport: str = "queue"  # queue | http | local
    state_url: str = ""
    queue_url: str = ""
    endpoint: str = ""
    token_env: str = "SKYCTL_HTTP_TOKEN"
    approver_token_env: str = "SKYCTL_APPROVER_TOKEN"
    env: dict = field(default_factory=dict)  # adapter settings, e.g. AWS_REGION, AZURE_STORAGE_ACCOUNT_NAME

    def validate(self) -> CloudProfile:
        if self.transport not in ("queue", "http", "local"):
            raise RemoteError(f"profile {self.name!r}: transport must be queue, http or local")
        if self.transport == "queue" and not (self.state_url and self.queue_url):
            raise RemoteError(
                f'profile {self.name!r} needs state_url and queue_url (or an endpoint with transport = "http")'
            )
        if self.transport == "http" and not self.endpoint:
            raise RemoteError(f'profile {self.name!r} needs an endpoint for transport = "http"')
        return self


def config_dir() -> Path:
    base = os.getenv("SKYCTL_CONFIG_DIR") or os.path.join(
        os.getenv("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "skyctl"
    )
    return Path(base)


def _toml_str(v: str) -> str:
    return json.dumps(str(v))


def save_profiles(profiles: dict[str, CloudProfile], path: Path | None = None) -> Path:
    path = path or config_dir() / "clouds.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# skyctl cloud profiles (skyctl cloud add / import). No secrets are stored here.", ""]
    for name, p in sorted(profiles.items()):
        lines.append(f"[clouds.{name}]")
        for key in ("platform", "transport", "state_url", "queue_url", "endpoint", "token_env", "approver_token_env"):
            value = getattr(p, key)
            if value:
                lines.append(f"{key} = {_toml_str(value)}")
        if p.env:
            lines.append(f"[clouds.{name}.env]")
            lines += [f"{k} = {_toml_str(v)}" for k, v in sorted(p.env.items())]
        lines.append("")
    path.write_text("\n".join(lines))
    return path


def load_profiles(path: Path | None = None) -> dict[str, CloudProfile]:
    path = path or config_dir() / "clouds.toml"
    if not path.exists():
        return {}
    data = tomllib.loads(path.read_text()).get("clouds", {})
    return {name: CloudProfile(name=name, **raw) for name, raw in data.items()}


def profile_from_env(name: str) -> CloudProfile | None:
    prefix = f"SKYCTL_CLOUD_{name.upper().replace('-', '_')}_"
    state, queue, endpoint = (os.getenv(prefix + k, "").strip() for k in ("STATE_URL", "QUEUE_URL", "ENDPOINT"))
    if not (state or endpoint):
        return None
    transport = "queue" if (state and queue) else "http"
    return CloudProfile(
        name=name, platform=name, transport=transport, state_url=state, queue_url=queue, endpoint=endpoint
    )


def resolve_profile(name: str, path: Path | None = None) -> CloudProfile:
    if name == "local":
        return CloudProfile(name="local", platform="local", transport="local")
    profile = profile_from_env(name) or load_profiles(path).get(name)
    if profile is None:
        raise RemoteError(
            f"No cloud profile {name!r}. Import one from your deployment:\n"
            f"  skyctl cloud import {name} --terraform-dir build/{name}\n"
            f"or add it by hand:\n"
            f"  skyctl cloud add {name} --state-url s3://… --queue-url sqs://…"
        )
    return profile.validate()


def profile_from_terraform_outputs(name: str, outputs: dict) -> CloudProfile:
    """Build a profile from `terraform output -json` of a generated skyctl deployment."""
    conn = (outputs.get("skyctl_connection") or {}).get("value")
    if not conn:
        raise RemoteError(
            "terraform outputs have no skyctl_connection (render the deployment with this skyctl version)"
        )
    endpoint = (outputs.get("service_url") or {}).get("value", "")
    return CloudProfile(
        name=name,
        platform=conn.get("platform", name),
        transport="queue",
        state_url=conn["state_url"],
        queue_url=conn["queue_url"],
        endpoint=endpoint,
        env=conn.get("env") or {},
    ).validate()


def terraform_outputs(directory: str) -> dict:
    try:
        out = subprocess.run(
            ["terraform", f"-chdir={directory}", "output", "-json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise RemoteError(
            "terraform is not installed; pass --outputs-file with `terraform output -json` instead"
        ) from None
    except subprocess.CalledProcessError as e:
        raise RemoteError(f"terraform output failed: {e.stderr.strip()[:300]}") from None
    return json.loads(out.stdout)


# ----------------------------------------------------------------------
# Connections
# ----------------------------------------------------------------------


class Connection(Protocol):
    name: str

    def submit_task(self, task: TaskSpec) -> str: ...
    def run_events(self, run_id: str) -> list[dict]: ...
    def get_plan(self, plan_id: str) -> dict | None: ...
    def approve_plan(self, plan_id: str, plan_hash: str, by: str) -> dict: ...
    def execute_plan(self, plan_id: str, plan_hash: str) -> None: ...


def _run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + uuid.uuid4().hex[:6]


class QueueConnection:
    """Talk to a deployment through its state bucket and work queue (the operator's cloud credentials)."""

    def __init__(self, profile: CloudProfile, state=None, queue=None):
        from .platform import services

        for key, value in profile.env.items():
            os.environ.setdefault(key, str(value))
        self.name = profile.name
        self.state = state or services.build("state", profile.state_url)
        self.queue = queue or services.build("queue", profile.queue_url)

    def submit_task(self, task: TaskSpec) -> str:
        run_id = _run_id()
        RunLog(self.state, run_id).append("queued", run_id=run_id, task=task.to_dict(), via="run-cloud-task")
        self.queue.send({"kind": "task.run", "run_id": run_id, "task": task.to_dict()}, dedupe_id=run_id)
        return run_id

    def run_events(self, run_id: str) -> list[dict]:
        try:
            return RunLog.load(self.state, run_id)
        except FileNotFoundError:
            return []

    def get_plan(self, plan_id: str) -> dict | None:
        from .planning import PlanStore

        return PlanStore(self.state).get(plan_id)

    def approve_plan(self, plan_id: str, plan_hash: str, by: str) -> dict:
        from .planning import PlanStore

        return PlanStore(self.state).approve(plan_id, plan_hash, by=by, via="run-cloud-task")

    def execute_plan(self, plan_id: str, plan_hash: str) -> None:
        self.queue.send(
            {"kind": "plan.execute", "plan_id": plan_id, "plan_hash": plan_hash}, dedupe_id=f"exec-{plan_id}"
        )


class HttpConnection:
    """Talk to a deployment's `skyctl serve` endpoint."""

    def __init__(self, profile: CloudProfile, client=None):
        import httpx

        self.name = profile.name
        self.base = profile.endpoint.rstrip("/")
        self.token = os.getenv(profile.token_env, "")
        self.approver_token = os.getenv(profile.approver_token_env, "")
        self.http = client or httpx.Client(timeout=60)

    def _req(self, method: str, path: str, token: str | None = None, **kw) -> dict:
        headers = {"Authorization": f"Bearer {token or self.token}"} if (token or self.token) else {}
        resp = self.http.request(method, f"{self.base}{path}", headers=headers, **kw)
        if resp.status_code == 404:
            return {}
        if resp.status_code >= 400:
            raise RemoteError(f"{method} {path}: HTTP {resp.status_code} {resp.text[:300]}")
        return resp.json()

    def submit_task(self, task: TaskSpec) -> str:
        return self._req("POST", "/runs", json=task.to_dict())["run_id"]

    def run_events(self, run_id: str) -> list[dict]:
        return self._req("GET", f"/runs/{run_id}").get("events", [])

    def get_plan(self, plan_id: str) -> dict | None:
        return self._req("GET", f"/plans/{plan_id}") or None

    def approve_plan(self, plan_id: str, plan_hash: str, by: str) -> dict:
        if not self.approver_token:
            raise RemoteError("approving over HTTP needs SKYCTL_APPROVER_TOKEN (the agent's token cannot approve)")
        return self._req("POST", f"/plans/{plan_id}/approve", token=self.approver_token, json={"plan_hash": plan_hash})

    def execute_plan(self, plan_id: str, plan_hash: str) -> None:
        self._req("POST", f"/plans/{plan_id}/execute", json={"plan_hash": plan_hash})


class LocalConnection:
    """Run on this machine with the local settings (development, demos, tests)."""

    def __init__(self, runner=None, ctx_factory: Callable[[], Any] | None = None):
        from .agent import AgentRunner

        self.name = "local"
        self.runner = runner or AgentRunner.from_settings()
        if self.runner.state is None:
            raise RemoteError("the local connection needs a state store (SKYCTL_STATE_URL)")
        self.state = self.runner.state
        self._ctx_factory = ctx_factory
        self._threads: list[threading.Thread] = []

    def _background(self, fn) -> None:
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        self._threads.append(t)

    def submit_task(self, task: TaskSpec) -> str:
        run_id = _run_id()
        RunLog(self.state, run_id).append("queued", run_id=run_id, task=task.to_dict(), via="run-cloud-task")
        self._background(lambda: self.runner.run(task, run_id=run_id))
        return run_id

    def run_events(self, run_id: str) -> list[dict]:
        try:
            return RunLog.load(self.state, run_id)
        except FileNotFoundError:
            return []

    def get_plan(self, plan_id: str) -> dict | None:
        from .planning import PlanStore

        return PlanStore(self.state).get(plan_id)

    def approve_plan(self, plan_id: str, plan_hash: str, by: str) -> dict:
        from .planning import PlanStore

        return PlanStore(self.state).approve(plan_id, plan_hash, by=by, via="run-cloud-task")

    def execute_plan(self, plan_id: str, plan_hash: str) -> None:
        from .planning import PlanStore, execute_plan
        from .platform.invocation import deployment_policy
        from .settings import Settings
        from .tools import ToolContext, registry

        def work():
            if self._ctx_factory is not None:
                ctx = self._ctx_factory()
            else:
                ctx = ToolContext(self.runner.manager, policy=deployment_policy(Settings.from_env()))
            ctx.plans = PlanStore(self.state)
            ctx.principal = "local-executor"
            try:
                execute_plan(ctx.plans, plan_id, plan_hash, ctx, registry)
            except Exception as e:  # recorded for the poller
                RunLog(self.state, f"plan-{plan_id}").append("error", error=str(e))

        self._background(work)


def connect(profile: CloudProfile, **kwargs) -> Connection:
    if profile.transport == "local":
        return LocalConnection(**kwargs)
    if profile.transport == "http":
        return HttpConnection(profile, **kwargs)
    return QueueConnection(profile, **kwargs)


# ----------------------------------------------------------------------
# The command's logic (the CLI is a thin wrapper; tests call this directly)
# ----------------------------------------------------------------------


def parse_description(description: str | None, words: tuple[str, ...]) -> str:
    """
    Accept --description "…", positional words, and the form
    `run_cloud_task --cloud aws -- description "…"` (the word "description"
    after the `--` separator is treated as the option name).
    """
    tokens = list(words)
    if tokens and tokens[0].lower().lstrip("-") in ("description", "d"):
        tokens = tokens[1:]
    positional = " ".join(tokens).strip()
    if description and positional:
        raise RemoteError("Give the task once: either --description or trailing text, not both.")
    text = (description or positional).strip()
    if not text:
        raise RemoteError('Describe the task, e.g. --description "Reply to questions in my mentions".')
    return text


def wants_citations(description: str) -> bool:
    """Requests about news, facts or misinformation should cite retrieved sources."""
    return bool(_CITATION_HINT.search(description))


def run_cloud_task(
    conn: Connection,
    task: TaskSpec,
    *,
    wait: bool = True,
    approve: str = "ask",
    confirm: Callable[[dict], bool] | None = None,
    principal: str = "operator",
    timeout: float = 900,
    poll: float = 2.0,
    on_event: Callable[[dict], None] | None = None,
    on_plan: Callable[[dict], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """
    Submit, follow, review and (if approved) execute. Returns an outcome dict:
    status ∈ queued | completed | awaiting_approval | failed | error | timeout | …
    """
    if approve not in ("ask", "auto", "never"):
        raise RemoteError("approve must be ask, auto or never")
    run_id = conn.submit_task(task)
    out: dict[str, Any] = {"run_id": run_id, "cloud": conn.name, "status": "queued"}
    if not wait:
        return out

    deadline = clock() + timeout
    seen, final = 0, None
    while final is None:
        events = conn.run_events(run_id)
        for entry in events[seen:]:
            if on_event is not None:
                on_event(entry)
        seen = len(events)
        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if final is None:
            if clock() > deadline:
                out["status"] = "timeout"
                return out
            sleep(poll)

    out.update(
        status=final.get("status"),
        summary=final.get("summary"),
        error=final.get("error"),
        plan_id=final.get("plan_id"),
        plan_status=final.get("plan_status"),
    )
    if not out["plan_id"] or final.get("status") != "needs_approval":
        return out

    record = conn.get_plan(out["plan_id"])
    if record is None:
        raise RemoteError(f"plan {out['plan_id']} is not visible in the state store")
    out["plan_hash"] = record["plan_hash"]
    if on_plan is not None:
        on_plan(record)
    approved = approve == "auto" or (approve == "ask" and confirm is not None and confirm(record))
    if not approved:
        out["status"] = "awaiting_approval"
        return out

    conn.approve_plan(out["plan_id"], record["plan_hash"], by=principal)
    conn.execute_plan(out["plan_id"], record["plan_hash"])
    while True:
        record = conn.get_plan(out["plan_id"]) or record
        if record["status"] in TERMINAL_PLAN:
            break
        if clock() > deadline:
            out["status"] = "timeout"
            out["plan_status"] = record["status"]
            return out
        sleep(poll)
    out["plan_status"] = record["status"]
    out["status"] = "completed" if record["status"] == "completed" else record["status"]
    out["steps"] = {
        sid: {
            "status": st["status"],
            "url": (st.get("result") or {}).get("url") if isinstance(st.get("result"), dict) else None,
            "error": st.get("error"),
        }
        for sid, st in record["steps"].items()
    }
    return out


def profile_to_dict(p: CloudProfile) -> dict:
    return {k: v for k, v in asdict(p).items() if v}
