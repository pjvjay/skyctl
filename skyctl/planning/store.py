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
skyctl.planning.store
=====================
Plan records in the state store: `plans/<id>.json`, one JSON document per
plan, every change a compare-and-swap update. Because records live in the
shared bucket (not in server memory), a plan created through one stateless
MCP/HTTP instance can be approved from the CLI and executed by another.

Lifecycle:

    awaiting_approval ──approve──► approved ──execute──► executing ──► completed | failed | dry_run
          │  (or created directly as approved when no approval is needed)      ▲
          └──────────────cancel──────────────► cancelled        resume ────────┘
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ..ports import ObjectStore, cas_update
from .check import PlanCheck
from .model import Plan

PREFIX = "plans/"
APPROVAL_MODES = ("none", "client", "elicit", "out_of_band")


class PlanError(RuntimeError):
    """The plan cannot move to the requested state."""


class ApprovalRequired(PlanError):
    def __init__(self, record: dict):
        self.record = record
        super().__init__(
            f"Plan {record['id']} needs approval ({record['approval']['mode']}). Show the preview to the user; "
            f"it can be approved with `skyctl plan approve {record['id']}` or POST /plans/{record['id']}/approve."
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PlanStore:
    def __init__(self, store: ObjectStore):
        self.store = store

    def _key(self, plan_id: str) -> str:
        if not plan_id or "/" in plan_id or ".." in plan_id:
            raise PlanError(f"invalid plan id {plan_id!r}")
        return f"{PREFIX}{plan_id}.json"

    # -- create / read ---------------------------------------------------------

    def create(
        self,
        plan: Plan,
        check: PlanCheck,
        *,
        created_by: str,
        approval_mode: str,
        source: str = "mcp",
        run_id: str | None = None,
    ) -> dict:
        if approval_mode not in APPROVAL_MODES:
            raise PlanError(f"approval mode must be one of {APPROVAL_MODES}")
        plan_id = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        required = approval_mode != "none" and check.summary.get("writes", 0) > 0
        record = {
            "id": plan_id,
            "plan": plan.model_dump(mode="json"),
            "plan_hash": check.plan_hash,
            "status": "awaiting_approval" if required else "approved",
            "created_at": _now(),
            "created_by": created_by,
            "source": source,
            "run_id": run_id,
            "approval": {"required": required, "mode": approval_mode, "by": None, "at": None, "via": None},
            "check": {
                "summary": check.summary,
                "preview": check.preview,
                "warnings": [f.to_dict() for f in check.findings if f.level == "warning"],
            },
            "steps": {s.id: {"status": "pending"} for s in plan.steps},
            "claimed_by": None,
            "claimed_until": None,
            "events": [{"at": _now(), "type": "created", "by": created_by}],
        }
        self.store.put(self._key(plan_id), json.dumps(record).encode("utf-8"), if_absent=True)
        return record

    def get(self, plan_id: str) -> dict | None:
        found = self.store.get(self._key(plan_id))
        return json.loads(found.data) if found else None

    def require(self, plan_id: str) -> dict:
        record = self.get(plan_id)
        if record is None:
            raise PlanError(f"no plan {plan_id!r}")
        return record

    def update(self, plan_id: str, mutate: Callable[[dict], Any]) -> dict:
        """Atomically apply mutate(record) (in place). Returns the record as written."""
        out: dict = {}

        def apply(raw: bytes | None) -> bytes | None:
            if raw is None:
                raise PlanError(f"no plan {plan_id!r}")
            record = json.loads(raw)
            mutate(record)
            out["record"] = record
            return json.dumps(record).encode("utf-8")

        cas_update(self.store, self._key(plan_id), apply)
        return out["record"]

    def list(self, status: str | None = None, limit: int = 20) -> list[dict]:
        records = []
        for key in self.store.list(PREFIX):
            if not key.endswith(".json"):
                continue
            try:
                r = json.loads(self.store.read(key))
            except Exception:
                continue
            if status is None or r["status"] == status:
                records.append(summarise(r))
        return sorted(records, key=lambda r: r["created_at"], reverse=True)[:limit]

    # -- transitions -------------------------------------------------------------

    def approve(self, plan_id: str, plan_hash: str, *, by: str, via: str) -> dict:
        def mutate(r: dict) -> None:
            if r["plan_hash"] != plan_hash:
                raise PlanError("plan hash mismatch: approve exactly the plan that was reviewed")
            if r["status"] != "awaiting_approval":
                raise PlanError(f"plan is {r['status']}, not awaiting approval")
            r["status"] = "approved"
            r["approval"].update(by=by, at=_now(), via=via)
            r["events"].append({"at": _now(), "type": "approved", "by": by, "via": via})

        return self.update(plan_id, mutate)

    def cancel(self, plan_id: str, *, by: str) -> dict:
        def mutate(r: dict) -> None:
            if r["status"] in ("completed", "cancelled"):
                raise PlanError(f"plan is already {r['status']}")
            if r["status"] == "executing" and r.get("claimed_until") and r["claimed_until"] > _now():
                raise PlanError("plan is executing; wait for it to finish")
            r["status"] = "cancelled"
            r["events"].append({"at": _now(), "type": "cancelled", "by": by})

        return self.update(plan_id, mutate)


def summarise(r: dict) -> dict:
    done = sum(1 for s in r["steps"].values() if s["status"] in ("done", "dry_run"))
    return {
        "id": r["id"],
        "status": r["status"],
        "goal": r["plan"]["goal"],
        "intent": r["plan"].get("intent"),
        "created_at": r["created_at"],
        "created_by": r["created_by"],
        "plan_hash": r["plan_hash"],
        "writes": r["check"]["summary"].get("writes", 0),
        "steps": len(r["steps"]),
        "steps_done": done,
        "approval": r["approval"],
    }
