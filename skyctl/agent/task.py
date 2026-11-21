"""
skyctl.agent.task
=================
TaskSpec: one unit of agent work, produced identically by the CLI, the
HTTP API, a scheduled job or a tasks.toml entry. RunResult: what happened.

tasks.toml (optional, for scheduled/named tasks):

    [tasks.daily-engagement]
    goal = "Read the work account's notifications from the last day and reply to genuine questions."
    accounts = ["work"]
    allow_writes = true
    max_writes = 5
    approval = "writes"        # none | writes  (writes: queue for a human, do not send)
    model = "bedrock:anthropic.claude-opus-5-5"
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Union

from ..tools.policy import ToolPolicy

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

DEFAULT_TASKS_FILE = "tasks.toml"
APPROVAL_MODES = ("none", "writes")


class TaskConfigError(ValueError):
    """A task definition is missing something or is inconsistent."""


@dataclass
class TaskSpec:
    goal: str
    name: Optional[str] = None
    accounts: Optional[list[str]] = None       # None → every configured account
    allow_writes: bool = False
    allow_delete: bool = False
    allow_public_writes: bool = True
    max_writes: int = 5
    max_turns: int = 25
    max_tool_calls: int = 60
    deadline_seconds: Optional[int] = 600
    dry_run: bool = False
    approval: str = "none"
    model: Optional[str] = None
    effort: Optional[str] = None
    tools: Optional[list[str]] = None          # allow-list of tool names
    deny_tools: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)   # extra facts for the prompt
    idempotency_key: Optional[str] = None      # dedupe writes across retried runs

    def __post_init__(self):
        if not self.goal or not self.goal.strip():
            raise TaskConfigError("A task needs a goal.")
        if self.approval not in APPROVAL_MODES:
            raise TaskConfigError(f"approval must be one of {APPROVAL_MODES}, got {self.approval!r}.")
        if self.accounts is not None:
            self.accounts = [a.lower() for a in self.accounts]

    def policy(self) -> ToolPolicy:
        return ToolPolicy(
            accounts=self.accounts,
            allow_writes=self.allow_writes,
            allow_delete=self.allow_delete,
            max_writes=self.max_writes,
            allow_public_writes=self.allow_public_writes,
            deny_tools=self.deny_tools,
            allow_tools=self.tools,
            approval=self.approval,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSpec":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise TaskConfigError(f"Unknown task fields: {', '.join(sorted(unknown))}")
        return cls(**data)


@dataclass
class RunResult:
    run_id: str
    status: str                 # completed | needs_approval | budget_exhausted | deadline | refused | error
    summary: str = ""
    turns: int = 0
    tool_calls: int = 0
    writes: int = 0
    pending_writes: int = 0
    usage: dict = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    error: Optional[str] = None
    model: str = ""
    task: Optional[dict] = None

    @property
    def ok(self) -> bool:
        return self.status in ("completed", "needs_approval")

    def to_dict(self) -> dict:
        return asdict(self)


class PolicyCeilingError(PermissionError):
    """A task asked for more than this deployment allows."""


def apply_deployment_ceilings(task: TaskSpec, ceilings) -> TaskSpec:
    """
    Clamp a task to the deployment's policy ceilings (skyctl.settings.AgentDefaults).
    Called where work is *executed* (HTTP, queue worker, Lambda, invoke), not
    only where it is submitted: a queue message can be injected by anything
    with send permission, so the executor must not trust it.
    """
    if task.allow_writes and not ceilings.allow_writes:
        raise PolicyCeilingError("writes are disabled on this deployment (SKYCTL_AGENT_ALLOW_WRITES)")
    if task.allow_delete and not ceilings.allow_delete:
        raise PolicyCeilingError("deletion is disabled on this deployment (SKYCTL_AGENT_ALLOW_DELETE)")
    task.max_writes = min(task.max_writes, ceilings.max_writes)
    task.max_turns = min(task.max_turns, max(ceilings.max_turns, 1))
    limit = getattr(ceilings, "max_run_seconds", None)
    if limit:
        task.deadline_seconds = min(task.deadline_seconds or limit, limit)
    if ceilings.accounts is not None:
        task.accounts = [a for a in (task.accounts or ceilings.accounts) if a in ceilings.accounts]
    return task


TASKS_STATE_KEY = "config/tasks.toml"


def load_tasks(path: Optional[Union[str, Path]] = None, state=None) -> dict[str, TaskSpec]:
    """
    Read tasks and return {name: TaskSpec}. Looks at `path`, then
    $SKYCTL_TASKS_FILE, then ./tasks.toml, then `config/tasks.toml` in the
    state store (where cloud deployments put it; see skyctl.deploy).
    """
    path = Path(path or os.getenv("SKYCTL_TASKS_FILE", "") or DEFAULT_TASKS_FILE)
    if path.exists():
        text, source = path.read_text(encoding="utf-8"), str(path)
    else:
        if state is None:
            try:
                from ..platform.runtime import get_runtime

                state = get_runtime().object_store()
            except Exception:
                state = None
        if state is None or not state.exists(TASKS_STATE_KEY):
            raise TaskConfigError(f"Tasks file '{path}' not found (nor {TASKS_STATE_KEY} in the state store).")
        text, source = state.read(TASKS_STATE_KEY).decode("utf-8"), TASKS_STATE_KEY
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise TaskConfigError(f"Could not parse {source}: {e}") from e
    path = source
    tasks = {}
    for name, raw in (data.get("tasks") or {}).items():
        if not isinstance(raw, dict):
            raise TaskConfigError(f"Task '{name}' must be a table.")
        try:
            tasks[name.lower()] = TaskSpec.from_dict({"name": name.lower(), **raw})
        except TypeError as e:
            raise TaskConfigError(f"Task '{name}': {e}") from e
    if not tasks:
        raise TaskConfigError(f"{path} defines no [tasks.<name>] sections.")
    return tasks
