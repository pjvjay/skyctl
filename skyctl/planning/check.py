"""
skyctl.planning.check
=====================
Static validation of a Plan against the session's tools and policy, plus a
preview a human can approve. Nothing here touches the network.

Checks, in order:
  1. structure     step references point backwards; plans cannot nest plan/agent tools
  2. questions     unresolved questions block execution
  3. tools         exist and are *permitted* for this session (exposure is not required)
  4. arguments     validate against each tool's input schema ($refs substituted)
  5. policy        account scope, public-network writes, write budget
  6. content       grapheme limits, thread splitting, images vs. link cards,
                   schedule times in the future, cron syntax
  7. grounding     posts a step targets (parent_uri, uri, quote_uri) were returned by a tool
                   in this session, so a model cannot act on a URI it invented
  8. hygiene       duplicate writes, two replies to one post
  9. validators    pluggable organisation rules (e.g. a link allow-list)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..richtext import MAX_GRAPHEMES, detect_facets, grapheme_len, split_thread
from ..tools.registry import ToolDenied, ToolRegistry
from .model import Plan, PlanStep, iter_refs, substitute

META_TAGS = {"plan", "agent"}
URI_ARGS = ("parent_uri", "uri", "quote_uri")       # arguments that name an existing post
TEXT_TOOLS = {"post", "reply", "schedule_post", "schedule_cron"}

# Top-level result fields a $ref may use, per tool (unknown tools are not checked).
_POST_RESULT = {"account", "success", "uri", "cid", "url", "created_at", "error"}
RESULT_FIELDS: dict[str, set] = {
    "post": _POST_RESULT, "reply": _POST_RESULT, "repost": _POST_RESULT, "like": _POST_RESULT,
    "thread": {"posts", "success"},
    "delete_post": {"success", "uri"},
    "schedule_post": {"job_id", "account", "run_at", "status"},
    "schedule_cron": {"job_id", "account", "cron", "status"},
    "cancel_job": {"job_id", "cancelled"},
    "list_accounts": {"accounts"}, "list_jobs": {"jobs"},
    "get_profile": {"data", "note"}, "get_timeline": {"data", "note"}, "get_notifications": {"data", "note"},
    "search_posts": {"data", "note"}, "list_posts": {"data", "note"}, "get_post": {"data", "note"},
    "fetch_link_metadata": {"data", "note"},
}


@dataclass
class Finding:
    level: str                 # "error" | "warning"
    message: str
    step: Optional[str] = None

    def to_dict(self) -> dict:
        return {"level": self.level, "step": self.step, "message": self.message}


@dataclass
class PlanCheck:
    ok: bool
    plan_hash: str
    findings: list[Finding] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    preview: list[dict] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "plan_hash": self.plan_hash,
            "errors": [f.to_dict() for f in self.findings if f.level == "error"],
            "warnings": [f.to_dict() for f in self.findings if f.level == "warning"],
            "summary": self.summary, "preview": self.preview,
        }


PlanValidator = Callable[[Plan, PlanStep, dict, Any], list[Finding]]
_VALIDATORS: list[PlanValidator] = []


def register_plan_validator(fn: PlanValidator) -> PlanValidator:
    """Add an organisation rule: fn(plan, step, resolved_args, ctx) -> [Finding]."""
    _VALIDATORS.append(fn)
    return fn


_URL = re.compile(r"https?://([^/\s]+)")


@register_plan_validator
def link_allowlist(plan: Plan, step: PlanStep, args: dict, ctx) -> list[Finding]:
    """SKYCTL_PLAN_LINK_ALLOWLIST=example.com,docs.example.com restricts links in published text and cards."""
    allowed = [d.strip().lower() for d in os.getenv("SKYCTL_PLAN_LINK_ALLOWLIST", "").split(",") if d.strip()]
    if not allowed:
        return []
    blob = " ".join(str(args.get(k) or "") for k in ("text", "link_card"))
    bad = [h for h in _URL.findall(blob) if not any(h.lower() == d or h.lower().endswith("." + d) for d in allowed)]
    return [Finding("error", f"link to {h} is not in SKYCTL_PLAN_LINK_ALLOWLIST", step.id) for h in bad]


def check_plan(plan: Plan, ctx, registry: ToolRegistry, now: Optional[datetime] = None,
               completed_steps: frozenset = frozenset(), proposal: bool = True) -> PlanCheck:
    """
    `completed_steps` (on resume) are checked structurally but not re-judged or re-counted.
    `proposal=False` is the executor's re-check: grounding asks whether the *proposing*
    session observed the targets, so it is not re-applied by whoever executes the plan.
    """
    now = now or datetime.now(timezone.utc)
    findings: list[Finding] = []
    err = lambda msg, step=None: findings.append(Finding("error", msg, step))      # noqa: E731
    warn = lambda msg, step=None: findings.append(Finding("warning", msg, step))   # noqa: E731
    policy = ctx.policy
    seen: dict[str, PlanStep] = {}
    writes, reads, destructive = 0, 0, 0
    by_account: dict[str, int] = {}
    public_accounts: set[str] = set()
    preview: list[dict] = []
    write_keys: dict[str, str] = {}
    reply_parents: dict[str, str] = {}

    if plan.questions:
        err("the plan has unresolved questions; ask the user, then submit a plan without them: "
            + " | ".join(plan.questions))

    for step in plan.steps:
        # -- structure ---------------------------------------------------
        for dep in step.depends_on:
            if dep not in seen:
                err(f"depends_on {dep!r} is not an earlier step", step.id)
        for path in iter_refs(step.args):
            ref_step, _, rest = path.partition(".")
            if ref_step not in seen:
                err(f"$ref {path!r} must point to an earlier step", step.id)
                continue
            top = rest.split(".", 1)[0] if rest else ""
            fields = RESULT_FIELDS.get(seen[ref_step].tool)
            if not top:
                err(f"$ref {path!r} must name a field of {ref_step!r}'s result", step.id)
            elif fields is not None and top not in fields:
                err(f"$ref {path!r}: {seen[ref_step].tool} results have no {top!r} (fields: {', '.join(sorted(fields))})", step.id)

        # -- tool ------------------------------------------------------------
        if step.tool not in registry:
            err(f"unknown tool {step.tool!r}", step.id)
            seen[step.id] = step
            continue
        spec = registry.get(step.tool)
        if set(spec.tags) & META_TAGS:
            err(f"{step.tool!r} cannot be a plan step", step.id)
            seen[step.id] = step
            continue
        if policy is not None and not policy.permits(spec):
            err(f"tool {step.tool!r} is not permitted in this session", step.id)

        # -- arguments (refs replaced by placeholders) ------------------------
        probe = substitute(step.args, lambda p: f"at://ref/{p}")
        try:
            args = spec.input_model(**probe).model_dump()
        except Exception as e:
            first = e.errors()[0] if hasattr(e, "errors") else {"loc": (), "msg": str(e)}
            loc = ".".join(str(x) for x in first.get("loc", ()))
            err(f"invalid arguments for {step.tool}: {loc + ': ' if loc else ''}{first.get('msg')}", step.id)
            seen[step.id] = step
            continue
        has_refs = any(True for _ in iter_refs(step.args))
        if step.id in completed_steps:
            preview.append(_preview(step, spec, args, ctx))
            seen[step.id] = step
            continue

        # -- policy ------------------------------------------------------------
        account = args.get(spec.account_param) if spec.account_param else None
        if policy is not None and account is not None and not policy.account_allowed(account):
            err(f"account {account!r} is outside this session's scope", step.id)
        if spec.mutating:
            writes += 1
            destructive += int(spec.destructive)
            if account:
                by_account[account] = by_account.get(account, 0) + 1
                try:
                    from ..agents import is_public_network

                    if is_public_network(ctx.manager.client(account).config.pds_url):
                        public_accounts.add(account)
                except KeyError:
                    err(f"unknown account {account!r}", step.id)
        else:
            reads += 1

        # -- content ------------------------------------------------------------
        text = args.get("text")
        if step.tool in TEXT_TOOLS and isinstance(text, str) and "at://ref/" not in text:
            n = grapheme_len(text)
            if n > MAX_GRAPHEMES:
                err(f"text is {n} graphemes; the limit is {MAX_GRAPHEMES} (use a thread step)", step.id)
            if not text.strip():
                err("text is empty", step.id)
        if step.tool == "post":
            if len(args.get("images") or []) > 4:
                err("at most 4 images per post", step.id)
            if args.get("images") and args.get("link_card"):
                err("a post cannot have both images and a link card", step.id)
        if step.tool == "schedule_post":
            try:
                when = datetime.fromisoformat(str(args["run_at"]).replace("Z", "+00:00"))
                when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
                if when <= now:
                    err(f"run_at {args['run_at']} is not in the future", step.id)
            except ValueError:
                err(f"run_at {args.get('run_at')!r} is not ISO 8601", step.id)
        if step.tool == "schedule_cron":
            try:
                from apscheduler.triggers.cron import CronTrigger

                CronTrigger.from_crontab(args["cron"], timezone="UTC")
            except Exception:
                err(f"cron {args.get('cron')!r} is not a valid 5-field expression", step.id)

        # -- hygiene ---------------------------------------------------------------
        if spec.mutating and not has_refs:
            key = f"{step.tool}:{sorted((k, str(v)) for k, v in args.items())}"
            if key in write_keys:
                warn(f"duplicates step {write_keys[key]!r}; the executor's ledger will skip it", step.id)
            write_keys.setdefault(key, step.id)
        # -- grounding: target only posts this session has actually observed ----------
        grounding = getattr(ctx, "grounding", "off") if proposal else "off"
        if grounding != "off":
            for key in URI_ARGS:
                value = step.args.get(key)
                if isinstance(value, str) and value.startswith("at://") and value not in ctx.seen_uris:
                    findings.append(Finding(
                        "error" if grounding == "error" else "warning",
                        f"{key} {value} was not returned by any tool in this session; read it first "
                        "(get_post / get_notifications / search_posts) so it is known to exist", step.id))

        if step.tool == "reply" and isinstance(args.get("parent_uri"), str) and "at://ref/" not in args["parent_uri"]:
            parent = args["parent_uri"]
            if parent in reply_parents:
                warn(f"second reply to the same post as step {reply_parents[parent]!r}", step.id)
            reply_parents.setdefault(parent, step.id)

        for validator in _VALIDATORS:
            findings.extend(validator(plan, step, args, ctx))

        preview.append(_preview(step, spec, args, ctx))
        seen[step.id] = step

    # -- budget and mode -----------------------------------------------------------
    if policy is not None and writes:
        if not policy.allow_writes:
            warn("writes are disabled for this session: executing would only preview the writes")
        else:
            remaining = policy.max_writes - getattr(ctx, "writes_done", 0)
            if writes > remaining:
                err(f"the plan makes {writes} writes but only {remaining} remain in this session's budget")

    summary = {
        "steps": len(plan.steps), "reads": reads, "writes": writes, "destructive": destructive,
        "writes_by_account": by_account, "public_network_accounts": sorted(public_accounts),
        "tools": sorted({s.tool for s in plan.steps}), "intent": plan.intent,
        "assumptions": plan.assumptions,
    }
    ok = not any(f.level == "error" for f in findings)
    return PlanCheck(ok=ok, plan_hash=plan.hash, findings=findings, summary=summary, preview=preview)


def _preview(step: PlanStep, spec, args: dict, ctx) -> dict:
    item: dict[str, Any] = {"step": step.id, "tool": step.tool, "effect": "write" if spec.mutating else "read"}
    if step.why:
        item["why"] = step.why
    account = args.get("account")
    if account:
        item["account"] = account
        try:
            item["handle"] = ctx.manager.client(account).config.handle
        except KeyError:
            pass
    refs = sorted(set(iter_refs(step.args)))
    if refs:
        item["uses"] = refs
    text = args.get("text")
    if isinstance(text, str):
        if step.tool == "thread":
            item["parts"] = split_thread(text)
        else:
            item["text"] = text
            item["graphemes"] = grapheme_len(text)
            item["facets"] = [{"type": f.kind, "value": f.value} for f in detect_facets(text)]
    for key in ("parent_uri", "uri", "quote_uri", "link_card", "run_at", "cron", "job_id", "query", "images"):
        if args.get(key):
            item[key] = args[key]
    return item


def render_preview(check: PlanCheck, plan: Plan) -> str:
    """The plan as a human reads it before approving (elicitation, CLI, chat)."""
    s = check.summary
    lines = [f"Plan: {plan.goal}", f"Hash: {check.plan_hash[:19]}…",
             f"{s['writes']} write(s), {s['reads']} read(s)"
             + (f", {s['destructive']} DESTRUCTIVE" if s.get("destructive") else "")
             + (f"; public accounts: {', '.join(s['public_network_accounts'])}" if s.get("public_network_accounts") else "")]
    for a in plan.assumptions:
        lines.append(f"Assumes: {a}")
    for p in check.preview:
        if p["effect"] != "write":
            continue
        who = f"@{p['handle']}" if p.get("handle") else p.get("account", "")
        head = f"[{p['step']}] {p['tool']} as {who}"
        if p.get("parent_uri"):
            head += f" → reply to {p['parent_uri']}"
        if p.get("run_at") or p.get("cron"):
            head += f" at {p.get('run_at') or p.get('cron')}"
        lines.append(head)
        if p.get("text"):
            lines.append(f"    “{p['text']}”")
        for i, part in enumerate(p.get("parts", []), 1):
            lines.append(f"    {i}/ “{part}”")
        if p.get("link_card"):
            lines.append(f"    card: {p['link_card']}")
        if p.get("uses"):
            lines.append(f"    uses: {', '.join(p['uses'])}")
    for f in check.findings:
        lines.append(f"{f.level.upper()}{' [' + f.step + ']' if f.step else ''}: {f.message}")
    return "\n".join(lines)
