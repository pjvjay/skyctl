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
skyctl.planning.execute
=======================
Deterministic plan execution. No model is involved after approval, so
nothing read during execution (a timeline, a mention) can change what is
done: the approved plan is the whole program.

Guarantees:
* hash-bound: executes only the plan whose hash was approved;
* re-validated: the plan is checked again against the *current* policy;
* exclusive: a claim stops two executors running one plan at once;
* resumable and idempotent: step states are saved after every step, and
  writes go through a ledger keyed by plan id, so re-running after a crash
  skips writes already made and still has their results for later $refs.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from ..tools.registry import ToolRegistry
from .check import check_plan
from .model import Plan, RefError, resolve_path, substitute
from .store import ApprovalRequired, PlanError, PlanStore

ProgressFn = Callable[[int, int, str], None]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _child_context(ctx, plan_id: str):
    """A context for running plan steps: permission-based, ledgered, pre-approved."""
    from ..agent.runner import WriteLedger
    from ..tools import ToolContext

    child = ToolContext(
        ctx.manager,
        policy=ctx.policy,
        scheduler=ctx._scheduler,
        dry_run=ctx.dry_run,
        run_id=f"plan:{plan_id}",
        approved=True,
    )
    child.via_plan = True
    child.writes_done = ctx.writes_done
    state = ctx.plans.store if ctx.plans is not None else None
    child.ledger = WriteLedger(state, f"plan:{plan_id}") if state is not None else None
    return child


def _succeeded(record) -> str:
    """done | dry_run | failed for one CallRecord."""
    if not record.ok:
        return "failed"
    if record.dry_run or record.pending_approval:
        return "dry_run"
    result = record.result
    if isinstance(result, dict) and result.get("success") is False:
        return "failed"
    return "done"


def execute_plan(
    plans: PlanStore,
    plan_id: str,
    plan_hash: str,
    ctx,
    registry: ToolRegistry,
    *,
    in_band_approval: str | None = None,
    on_progress: ProgressFn | None = None,
    claim_seconds: int = 900,
) -> dict:
    record = plans.require(plan_id)
    if record["plan_hash"] != plan_hash:
        raise PlanError("plan hash mismatch: execute exactly the plan that was created/approved")
    if record["status"] == "cancelled":
        raise PlanError("plan was cancelled")
    if record["status"] in ("completed", "dry_run"):
        return record
    if record["status"] == "awaiting_approval":
        if not in_band_approval:
            raise ApprovalRequired(record)
        record = plans.approve(plan_id, plan_hash, by=getattr(ctx, "principal", "unknown"), via=in_band_approval)

    plan = Plan.model_validate(record["plan"])
    finished = {sid for sid, st in record["steps"].items() if st["status"] in ("done", "dry_run")}
    recheck = check_plan(plan, ctx, registry, completed_steps=frozenset(finished), proposal=False)
    if not recheck.ok:
        raise PlanError(
            "the plan no longer passes the current policy: "
            + "; ".join(f"[{f.step}] {f.message}" if f.step else f.message for f in recheck.errors)
        )

    owner = f"{socket.gethostname()}:{uuid.uuid4().hex[:6]}"

    def claim(r: dict) -> None:
        busy = r["status"] == "executing" and r.get("claimed_until") and r["claimed_until"] > _now().isoformat()
        if busy and r.get("claimed_by") != owner:
            raise PlanError("the plan is already executing elsewhere")
        if r["status"] == "failed":  # re-executing a failed plan retries its failed steps
            for st in r["steps"].values():
                if st["status"] in ("failed", "skipped"):
                    st.clear()
                    st["status"] = "pending"
        r["status"] = "executing"
        r["claimed_by"] = owner
        r["claimed_until"] = (_now() + timedelta(seconds=claim_seconds)).isoformat()
        r["events"].append({"at": _now().isoformat(), "type": "executing", "by": getattr(ctx, "principal", "unknown")})

    record = plans.update(plan_id, claim)
    child = _child_context(ctx, plan_id)
    results = {sid: st.get("result") for sid, st in record["steps"].items() if st["status"] in ("done", "dry_run")}
    failed: set[str] = {sid for sid, st in record["steps"].items() if st["status"] in ("failed", "skipped")}
    total = len(plan.steps)

    for index, step in enumerate(plan.steps, 1):
        state = record["steps"][step.id]
        if state["status"] in ("done", "dry_run"):
            continue
        blocked = [d for d in step.depends_on if d in failed]
        if blocked or (failed and not all(record["steps"][f].get("continue_on_error") for f in failed)):
            outcome: dict[str, Any] = {
                "status": "skipped",
                "error": f"skipped: {', '.join(blocked) or 'an earlier step'} failed",
            }
        else:
            try:
                args = substitute(step.args, lambda path: resolve_path(results, path))
                _, call = registry.call_json(step.tool, args, child)
                status = _succeeded(call)
                result = call.result
                if call.skipped_duplicate and isinstance(result, dict):
                    result = (result.get("previous") or {}).get("result", result)  # resume: reuse the earlier result
                    status = "done"
                error = call.error or (result.get("error") if isinstance(result, dict) else None)
                outcome = {"status": status, "result": result, "error": error if status == "failed" else None}
            except RefError as e:
                outcome = {"status": "failed", "error": str(e)}
        outcome["at"] = _now().isoformat()
        outcome["continue_on_error"] = step.continue_on_error
        if outcome["status"] in ("done", "dry_run"):
            results[step.id] = outcome.get("result")
        else:
            failed.add(step.id)

        def save(r: dict, sid=step.id, o=outcome) -> None:
            r["steps"][sid] = o
            r["claimed_until"] = (_now() + timedelta(seconds=claim_seconds)).isoformat()

        record = plans.update(plan_id, save)
        if on_progress is not None:
            on_progress(index, total, f"{step.id} {step.tool}: {outcome['status']}")

    ctx.writes_done = child.writes_done

    def finish(r: dict) -> None:
        states = [s["status"] for s in r["steps"].values()]
        r["status"] = (
            "failed"
            if any(s in ("failed", "skipped") for s in states)
            else "dry_run"
            if any(s == "dry_run" for s in states)
            else "completed"
        )
        r["claimed_by"] = r["claimed_until"] = None
        r["events"].append({"at": _now().isoformat(), "type": r["status"]})

    return plans.update(plan_id, finish)
