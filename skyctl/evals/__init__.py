"""
skyctl.evals — does the agent interpret free text well? (docs/MCP_AGENT.md §9)

    run_suite(provider_factory, repeat=3)  → Report (pass@N, pass^N, cost)
    SCENARIOS                              built-in scenarios
    World                                  the simulated network (skyctl.testing.world)
"""

from .harness import Report, ScenarioResult, evaluate, run_scenario, run_suite
from .scenarios import SCENARIOS

__all__ = ["Report", "SCENARIOS", "ScenarioResult", "evaluate", "run_scenario", "run_suite"]
