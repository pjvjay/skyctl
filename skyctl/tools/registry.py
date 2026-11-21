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
skyctl.tools.registry
=====================
One place to define a tool; many ways to expose it.

A tool is a plain function whose first parameter is the ToolContext and
whose remaining parameters are typed (use Annotated[..., Field(description=...)]
for argument docs). The registry derives a JSON schema from the signature
and can emit it for:

    to_anthropic()   Anthropic Messages API `tools`
    to_openai()      OpenAI-compatible `tools` (function calling)
    install_mcp()    an MCP server (FastMCP), via skyctl.mcp_server

and executes calls through `call()`, which is where policy is enforced.
Tool results are JSON-able dicts/lists so every consumer sees the same thing.
"""

from __future__ import annotations

import inspect
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError, create_model

_AT_URI = re.compile(r"at://[A-Za-z0-9:._-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._~-]+")
_HTTP_URL = re.compile(r"https?://[^\s\"'<>\\]+")
MAX_SEEN_URIS = 10_000


def _remember_uris(ctx, result, spec=None) -> None:
    """
    Grounding: note every at:// URI a tool returned, and every http(s) URL a
    *research* tool returned, so plans can be checked against what was observed.
    """
    blob = json.dumps(result, default=str)
    seen = getattr(ctx, "seen_uris", None)
    if seen is not None and len(seen) < MAX_SEEN_URIS:
        seen.update(_AT_URI.findall(blob))
    urls = getattr(ctx, "seen_urls", None)
    if urls is not None and spec is not None and "research" in spec.tags and len(urls) < MAX_SEEN_URIS:
        urls.update(u.rstrip(".,;:)]}") for u in _HTTP_URL.findall(blob))
    posts = getattr(ctx, "seen_posts", None)
    if isinstance(posts, dict):
        _remember_posts(result, posts)


def _remember_posts(node, posts: dict, depth: int = 0) -> None:
    """Author and text of posts a tool returned, so a plan preview can show what a reply answers."""
    if depth > 12 or len(posts) >= MAX_SEEN_URIS:
        return
    if isinstance(node, dict):
        uri, text = node.get("uri"), node.get("text")
        if isinstance(uri, str) and uri.startswith("at://") and isinstance(text, str):
            author = node.get("author")
            if isinstance(author, dict):
                author = author.get("handle") or author.get("did")
            posts.setdefault(uri, {"author": author if isinstance(author, str) else None, "text": text[:500]})
        for value in node.values():
            if isinstance(value, dict | list):
                _remember_posts(value, posts, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _remember_posts(value, posts, depth + 1)


class ToolError(Exception):
    """A tool call failed in a way the model should be told about."""


class ToolDenied(ToolError):
    """Policy refused the call."""


@dataclass
class ToolSpec:
    name: str
    description: str
    fn: Callable[..., Any]
    input_model: Any  # pydantic model class for the arguments
    mutating: bool = False  # changes state on Bluesky or in the queue
    destructive: bool = False  # deletes something
    account_param: str | None = "account"  # arg that names the account (policy scoping)
    tags: tuple[str, ...] = ()

    @property
    def input_schema(self) -> dict:
        schema = _inline_refs(self.input_model.model_json_schema())
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        return schema

    def bind(self, ctx) -> Callable[..., Any]:
        """The tool as a plain function with the context baked in (for MCP)."""
        fn = self.fn
        sig = inspect.signature(fn)
        hints = inspect.get_annotations(fn, eval_str=True)  # real types, not strings
        ctx_name, *params = sig.parameters.values()
        params = [p.replace(annotation=hints.get(p.name, p.annotation)) for p in params]

        def wrapper(*args, **kwargs):
            return fn(ctx, *args, **kwargs)

        wrapper.__signature__ = sig.replace(  # type: ignore[attr-defined]
            parameters=params, return_annotation=hints.get("return", sig.return_annotation)
        )
        wrapper.__annotations__ = {k: v for k, v in hints.items() if k != ctx_name.name}
        wrapper.__name__ = self.name
        wrapper.__doc__ = self.description
        return wrapper


def _inline_refs(schema: dict) -> dict:
    """
    Replace {"$ref": "#/$defs/X"} with the definition and drop $defs, so every
    consumer (Anthropic, OpenAI-compatible, MCP clients) gets a self-contained
    schema. Tool schemas here are not recursive.
    """
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = walk(dict(defs[ref.split("/")[-1]]))
                extra = {k: v for k, v in node.items() if k != "$ref"}
                return {**target, **extra}
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def _model_from_signature(fn: Callable, name: str):
    """Pydantic model of every parameter after the context parameter."""
    sig = inspect.signature(fn)
    hints = inspect.get_annotations(fn, eval_str=True)
    fields: dict[str, Any] = {}
    for i, (pname, param) in enumerate(sig.parameters.items()):
        if i == 0:
            continue  # ctx
        annotation = hints.get(pname, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[pname] = (annotation, default)
    return create_model(f"{name}Arguments", **fields)


@dataclass
class CallRecord:
    """What happened on one tool call; appended to the run transcript."""

    name: str
    args: dict
    ok: bool
    result: Any = None
    error: str | None = None
    duration_ms: int = 0
    dry_run: bool = False
    denied: bool = False
    pending_approval: bool = False
    skipped_duplicate: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "args": self.args,
            "ok": self.ok,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "dry_run": self.dry_run,
            "denied": self.denied,
            "pending_approval": self.pending_approval,
            "skipped_duplicate": self.skipped_duplicate,
        }


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    # -- definition ----------------------------------------------------

    def tool(
        self,
        name: str | None = None,
        *,
        mutating: bool = False,
        destructive: bool = False,
        account_param: str | None = "account",
        tags: tuple[str, ...] = (),
    ):
        """Decorator: register `fn(ctx, ...)` as a tool."""

        def decorate(fn: Callable) -> Callable:
            tool_name = name or fn.__name__
            description = inspect.cleandoc(fn.__doc__ or tool_name)
            # Tags drive exposure profiles; default: read | write | destructive.
            inferred = ("destructive",) if destructive else ("write",) if mutating else ("read",)
            self.register(
                ToolSpec(
                    name=tool_name,
                    description=description,
                    fn=fn,
                    input_model=_model_from_signature(fn, tool_name),
                    mutating=mutating or destructive,
                    destructive=destructive,
                    account_param=account_param,
                    tags=tuple(tags) or inferred,
                )
            )
            return fn

        return decorate

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool {spec.name!r} is already registered.")
        self._tools[spec.name] = spec

    # -- lookup --------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(f"Unknown tool {name!r}. Available: {', '.join(self._tools)}") from None

    def names(self) -> list[str]:
        return list(self._tools)

    def select(self, names: list[str] | None = None, policy=None) -> list[ToolSpec]:
        """The specs a consumer should see: filtered by name and by policy visibility."""
        specs = [self._tools[n] for n in (names or self._tools)]
        if policy is not None:
            specs = [s for s in specs if policy.visible(s)]
        return specs

    # -- schemas -------------------------------------------------------

    def to_anthropic(self, names: list[str] | None = None, policy=None) -> list[dict]:
        return [
            {"name": s.name, "description": s.description, "input_schema": s.input_schema}
            for s in self.select(names, policy)
        ]

    def to_openai(self, names: list[str] | None = None, policy=None) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.input_schema,
                },
            }
            for s in self.select(names, policy)
        ]

    # -- execution -----------------------------------------------------

    def call(self, name: str, args: dict, ctx) -> CallRecord:
        """
        Validate, apply policy, run. Never raises for a tool failure: the
        CallRecord carries the error so the model can react to it.
        """
        args = dict(args or {})
        record = CallRecord(name=name, args=args, ok=False)
        try:
            spec = self.get(name)
            try:
                validated = spec.input_model(**args)
            except ValidationError as e:
                raise ToolError(f"Invalid arguments for {name}: {e.errors()[0].get('msg', e)}") from e
            clean = validated.model_dump()
            policy = getattr(ctx, "policy", None)
            if policy is not None:
                policy.check(spec, clean, ctx)  # raises ToolDenied
                if spec.mutating:
                    mode = policy.write_mode(ctx)
                    if mode == "dry_run":
                        record.dry_run = True
                        record.result = {
                            "dry_run": True,
                            "would": {"tool": name, "args": clean},
                            "note": "Writes are disabled for this run; nothing was sent.",
                        }
                        record.ok = True
                        return record
                    if mode == "approval":
                        record.pending_approval = True
                        record.result = {
                            "pending_approval": True,
                            "would": {"tool": name, "args": clean},
                            "note": "Queued for human approval; it will be sent only if approved.",
                        }
                        record.ok = True
                        return record
            ledger = getattr(ctx, "ledger", None)
            if spec.mutating and ledger is not None:
                key = ledger.key_for(name, clean)
                previous = ledger.seen(key)
                if previous is not None:
                    record.skipped_duplicate = True
                    record.ok = True
                    record.result = {
                        "skipped": True,
                        "note": "This exact write was already made by an earlier run; not repeated.",
                        "previous": previous,
                    }
                    return record
            record.result = spec.fn(ctx, **clean)
            record.ok = True
            _remember_uris(ctx, record.result, spec)
            if spec.mutating:
                if policy is not None:
                    policy.count_write(ctx)
                # Only a write that took effect goes in the ledger: recording a failed attempt
                # would make a retry skip it and report success for a post that never went out.
                took_effect = not (isinstance(record.result, dict) and record.result.get("success") is False)
                if ledger is not None and took_effect:
                    ledger.record(
                        ledger.key_for(name, clean), {"run_id": getattr(ctx, "run_id", ""), "result": record.result}
                    )
            return record
        except ToolDenied as e:
            record.denied = True
            record.error = str(e)
        except ToolError as e:
            record.error = str(e)
        except Exception as e:  # a tool bug or an API failure
            record.error = f"{type(e).__name__}: {e}"
        return record

    def call_json(self, name: str, args: dict, ctx) -> tuple[str, CallRecord]:
        """call() plus the JSON text a model should receive."""
        started = time.monotonic()
        record = self.call(name, args, ctx)
        record.duration_ms = int((time.monotonic() - started) * 1000)
        if record.ok:
            payload = record.result
        else:
            payload = {"error": record.error, "denied": record.denied}
        return json.dumps(payload, default=str, ensure_ascii=False), record


def as_text(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False, indent=2)
