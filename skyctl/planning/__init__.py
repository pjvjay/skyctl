"""
skyctl.planning — the plan protocol (docs/MCP_AGENT.md §4).

    Plan / PlanStep     a structured, hash-addressed proposal of tool calls
    check_plan          static validation against tools and policy + a human preview
    PlanStore           plan records in the state store (CAS), approval, cancel
    execute_plan        deterministic, hash-bound, resumable execution
"""

from .check import Finding, PlanCheck, check_plan, register_plan_validator, render_preview
from .execute import execute_plan
from .model import Plan, PlanStep, RefError
from .store import APPROVAL_MODES, ApprovalRequired, PlanError, PlanStore, summarise

__all__ = [
    "APPROVAL_MODES", "ApprovalRequired", "Finding", "Plan", "PlanCheck", "PlanError", "PlanStep", "PlanStore",
    "RefError", "check_plan", "execute_plan", "register_plan_validator", "render_preview", "summarise",
]
