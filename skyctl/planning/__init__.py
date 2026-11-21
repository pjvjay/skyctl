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
    "APPROVAL_MODES",
    "ApprovalRequired",
    "Finding",
    "Plan",
    "PlanCheck",
    "PlanError",
    "PlanStep",
    "PlanStore",
    "RefError",
    "check_plan",
    "execute_plan",
    "register_plan_validator",
    "render_preview",
    "summarise",
]
