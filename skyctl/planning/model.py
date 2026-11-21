"""
skyctl.planning.model
=====================
The Plan: a structured, reviewable proposal of tool calls, produced by an
agent (the MCP client's model, or skyctl's own runner) from a free-text
request.

    {
      "goal": "Reply to pricing questions in today's mentions on the work account",
      "intent": "engage",
      "assumptions": ["'today' means the last 24 hours"],
      "questions": [],
      "steps": [
        {"id": "r1", "tool": "reply",
         "args": {"account": "work", "parent_uri": "at://did:plc:x/app.bsky.feed.post/1",
                  "text": "Yes! Students get 20% off."},
         "why": "@fan.test asked about a student discount"},
        {"id": "p1", "tool": "post",
         "args": {"account": "work", "text": "FAQ: pricing", "quote_uri": {"$ref": "r1.uri"}},
         "depends_on": ["r1"]}
      ]
    }

* `{"$ref": "<step>.<field>[.<field>|.<index>]…"}` anywhere in args is
  replaced at execution time with a value from an earlier step's result.
* Non-empty `questions` mean the request was ambiguous; the plan cannot run
  until they are resolved (a new plan without them).
* The canonical JSON form is hashed; approval binds to that hash.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterator, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

INTENTS = ("publish", "engage", "research", "schedule", "manage", "delegate", "other")
STATUSES = ("awaiting_approval", "approved", "executing", "completed", "failed", "cancelled")


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$", description="Unique step id, e.g. r1.")
    tool: str = Field(description="A tool name from the catalogue.")
    args: dict[str, Any] = Field(default_factory=dict, description='Tool arguments; {"$ref": "step.field"} for earlier results.')
    why: str = Field("", description="One line: why this step serves the goal.")
    depends_on: list[str] = Field(default_factory=list)
    continue_on_error: bool = False


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, description="The user's request, restated precisely.")
    intent: Optional[Literal["publish", "engage", "research", "schedule", "manage", "delegate", "other"]] = None
    assumptions: list[str] = Field(default_factory=list, description="Interpretations made where the request was loose.")
    questions: list[str] = Field(default_factory=list, description="Unresolved ambiguities; a plan with questions cannot run.")
    steps: list[PlanStep] = Field(min_length=1, max_length=50)

    @field_validator("steps")
    @classmethod
    def _unique_ids(cls, steps: list[PlanStep]) -> list[PlanStep]:
        ids = [s.id for s in steps]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate step ids: {', '.join(dupes)}")
        return steps

    def canonical(self) -> bytes:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @property
    def hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical()).hexdigest()


# ----------------------------------------------------------------------
# References between steps
# ----------------------------------------------------------------------

class RefError(ValueError):
    pass


def iter_refs(value: Any) -> Iterator[str]:
    """Every "$ref" path inside an args structure."""
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            yield value["$ref"]
            return
        for v in value.values():
            yield from iter_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from iter_refs(v)


def substitute(value: Any, resolve) -> Any:
    """Copy of `value` with every {"$ref": path} replaced by resolve(path)."""
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            return resolve(value["$ref"])
        return {k: substitute(v, resolve) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, resolve) for v in value]
    return value


def resolve_path(results: dict[str, Any], path: str) -> Any:
    step, *fields = path.split(".")
    if step not in results:
        raise RefError(f"$ref {path!r}: step {step!r} has no result")
    node = results[step]
    for f in fields:
        if isinstance(node, list) and f.isdigit() and int(f) < len(node):
            node = node[int(f)]
        elif isinstance(node, dict) and f in node:
            node = node[f]
        else:
            raise RefError(f"$ref {path!r}: no field {f!r}")
    return node
