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
skyctl.evals — does the agent interpret free text well? (docs/MCP_AGENT.md §9)

    run_suite(provider_factory, repeat=3)  → Report (pass@N, pass^N, cost)
    SCENARIOS                              built-in scenarios
    World                                  the simulated network (skyctl.testing.world)
"""

from .harness import Report, ScenarioResult, evaluate, run_scenario, run_suite
from .scenarios import SCENARIOS

__all__ = ["SCENARIOS", "Report", "ScenarioResult", "evaluate", "run_scenario", "run_suite"]
