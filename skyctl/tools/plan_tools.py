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
skyctl.tools.plan_tools
=======================
The plan protocol as tools (tag "plan"). An agent turns a free-text request
into a Plan, checks it, creates it, and — subject to approval — executes it.

    plan_check    validate a plan; returns errors, warnings and a preview. No side effects.
    plan_create   validate and store; returns plan_id, plan_hash and whether approval is needed
    plan_execute  run an approved plan (hash-bound, idempotent, resumable)
    plan_status   a plan's state, step results and links
    plan_list     recent plans
    plan_cancel   cancel a plan that has not finished

The MCP server replaces plan_execute with an async version that can ask
the human through elicitation and reports progress (skyctl.mcp_server).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ..planning import (
    ApprovalRequired,
    Plan,
    PlanError,
    PlanStore,
    check_plan,
    execute_plan,
    render_preview,
    summarise,
)
from .bsky import ToolContext, registry
from .registry import ToolError

PlanId = Annotated[str, Field(description="plan_id returned by plan_create.")]
PlanHash = Annotated[str, Field(description="plan_hash returned by plan_create (binds approval to this exact plan).")]

_PLAN_DOC = (
    'A plan: {"goal": str, "intent": "publish|engage|research|schedule|manage|delegate|other", '
    '"assumptions": [str], "questions": [str], "steps": [{"id": "s1", "tool": "<tool name>", "args": {...}, '
    '"why": str, "depends_on": ["earlier step id"], "continue_on_error": false}]}. '
    'Use {"$ref": "<step>.<field>"} in args to use an earlier step\'s result, e.g. {"$ref": "t1.posts.0.uri"}.'
)


def _store(ctx: ToolContext) -> PlanStore:
    if ctx.plans is None:
        raise ToolError("Plans need a state store (SKYCTL_STATE_URL); none is configured for this session.")
    return ctx.plans


def _detail(record: dict, max_result_chars: int = 2000) -> dict:
    import json

    steps = {}
    for sid, st in record["steps"].items():
        item = {k: v for k, v in st.items() if k in ("status", "error", "at")}
        result = st.get("result")
        if isinstance(result, dict):
            for key in ("url", "uri", "job_id", "success"):
                if key in result:
                    item[key] = result[key]
            if "posts" in result:
                item["urls"] = [p.get("url") for p in result["posts"]]
        elif result is not None and len(json.dumps(result, default=str)) <= max_result_chars:
            item["result"] = result
        steps[sid] = item
    return {**summarise(record), "steps": steps, "warnings": record["check"].get("warnings", [])}


@registry.tool(account_param=None, tags=("plan",))
def plan_check(ctx: ToolContext, plan: Annotated[Plan, Field(description=_PLAN_DOC)]) -> dict:
    """Validate a plan without storing or running it: tool names, arguments, account scope, the
    300-grapheme limit, schedule times, budget. Returns errors to fix, warnings, and a preview of
    every write. Use it while drafting; it has no side effects."""
    plan = Plan.model_validate(plan)
    check = check_plan(plan, ctx, registry)
    return {**check.to_dict(), "preview_text": render_preview(check, plan)}


@registry.tool(account_param=None, tags=("plan",))
def plan_create(ctx: ToolContext, plan: Annotated[Plan, Field(description=_PLAN_DOC)]) -> dict:
    """Validate and store a plan so it can be approved and executed. Fails with the list of
    errors if the plan is invalid (fix them and call again). Returns plan_id, plan_hash, status
    ("approved" or "awaiting_approval") and a human-readable preview to show the user."""
    plan = Plan.model_validate(plan)
    check = check_plan(plan, ctx, registry)
    if not check.ok:
        raise ToolError(
            "The plan is not valid:\n"
            + "\n".join(f"- [{f.step}] {f.message}" if f.step else f"- {f.message}" for f in check.errors)
        )
    record = _store(ctx).create(
        plan, check, created_by=ctx.principal, approval_mode=ctx.plan_approval, run_id=ctx.run_id or None
    )
    return {
        "plan_id": record["id"],
        "plan_hash": record["plan_hash"],
        "status": record["status"],
        "approval": record["approval"],
        "summary": check.summary,
        "warnings": [f.to_dict() for f in check.findings if f.level == "warning"],
        "preview_text": render_preview(check, plan),
    }


@registry.tool(account_param=None, tags=("plan",))
def plan_execute(ctx: ToolContext, plan_id: PlanId, plan_hash: PlanHash) -> dict:
    """Execute a created plan step by step. Only runs if it is approved (or needs no approval);
    re-running a partly executed or failed plan resumes it without repeating writes."""
    try:
        record = execute_plan(_store(ctx), plan_id, plan_hash, ctx, registry)
    except ApprovalRequired as e:
        raise ToolError(str(e)) from None
    except PlanError as e:
        raise ToolError(str(e)) from None
    return _detail(record)


@registry.tool(account_param=None, tags=("plan",))
def plan_status(ctx: ToolContext, plan_id: PlanId) -> dict:
    """A plan's status, approval, and per-step results with links."""
    try:
        return _detail(_store(ctx).require(plan_id))
    except PlanError as e:
        raise ToolError(str(e)) from None


@registry.tool(account_param=None, tags=("plan",))
def plan_list(
    ctx: ToolContext,
    status: Annotated[
        str | None, Field(description="Filter: awaiting_approval, approved, executing, completed, failed, cancelled.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict:
    """Recent plans, newest first."""
    return {"plans": _store(ctx).list(status=status, limit=limit)}


@registry.tool(account_param=None, tags=("plan",))
def plan_cancel(ctx: ToolContext, plan_id: PlanId) -> dict:
    """Cancel a plan that has not finished, e.g. when the user changes their mind."""
    try:
        return summarise(_store(ctx).cancel(plan_id, by=ctx.principal))
    except PlanError as e:
        raise ToolError(str(e)) from None
