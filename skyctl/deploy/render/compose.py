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
skyctl.deploy.render.compose
============================
DeploySpec → a Docker Compose file (JSON is valid Compose YAML).

Compose has no scheduler, queue or delayed-delivery service, so this target
is the clearest example of *capability substitution*:

    missing capability        substitute
    ------------------------  ---------------------------------------------------
    scheduled jobs            `skyctl daemon` runs every schedule (APScheduler,
                              any time zone, max one instance at a time)
    work queue                /runs executes on a thread (compose keeps CPU after
                              the response, so this is safe here)
    delayed delivery          the tick reconciler sends one-time posts
    bucket with CAS           file:///data on a shared volume (lock-file CAS)
"""

from __future__ import annotations

import json

from ..spec import DeploySpec, required
from .base import Artifact, RenderedSchedule, Workload, common_env


def render_compose(spec: DeploySpec) -> Artifact:
    c = required(spec.targets.compose, "compose")
    app = spec.app.name
    art = Artifact("compose")
    env = common_env(
        spec,
        "compose",
        platform="compose",
        compute="compose",
        secrets="env",
        state_url="file:///data",
        queue_url="none",
        delayed_url="none",
    )
    art.env = env
    schedules = [
        {"name": s.name, "cron": s.cron, "timezone": s.timezone, "invoke": s.payload()} for s in spec.schedules
    ]
    base = {
        "image": spec.app.image,
        "env_file": [".env"],
        "environment": dict(env),
        "volumes": ["state:/data"],
        "read_only": True,
        "tmpfs": ["/tmp"],
        "user": "10001",
        "restart": "unless-stopped",
    }
    services = {
        "daemon": {
            **base,
            "command": ["daemon"],
            "environment": {**env, "SKYCTL_SCHEDULES_JSON": json.dumps(schedules, separators=(",", ":"))},
        }
    }
    art.workloads.append(Workload("daemon", "job", "compose", ["daemon"]))
    if spec.service.enabled:
        bind = "0.0.0.0" if spec.service.public else "127.0.0.1"
        services["http"] = {
            **base,
            "command": ["serve"],
            "ports": [f"{bind}:{c.port}:8080"],
            "environment": {**env, "PORT": "8080"},
        }
        art.workloads.append(Workload("http", "service", "compose", ["serve"]))
        if spec.service.public:
            art.public_endpoints.append(f"host:{c.port}")
    for s in spec.schedules:
        art.schedules.append(RenderedSchedule(s.name, s.cron, s.timezone, s.payload(), overlap_guard="platform"))
    doc = {"name": app, "services": services, "volumes": {"state": {}}}
    art.files["compose.json"] = json.dumps(doc, indent=2) + "\n"
    art.notes.append("docker compose -f compose.json up -d   (secrets come from .env beside it)")
    return art
