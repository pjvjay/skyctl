"""
skyctl.server
=============
The HTTP entry point for cloud deployments (`skyctl serve`):

    GET     /healthz                process is up (public)
    GET     /readyz                 runtime description: platform, capabilities, services
    POST    /invoke                 any push trigger: Cloud Tasks, Pub/Sub push, Eventarc /
                                    Event Grid (CloudEvents or Event Grid schema), plain JSON
    OPTIONS /invoke                 CloudEvents webhook validation handshake (public)
    POST    /tick                   one scheduler pass
    POST    /runs                   start a task-agent run; body = TaskSpec JSON → {run_id}
    GET     /runs/{run_id}          status + transcript
    POST    /runs/{run_id}/approve  execute the writes a run queued for approval
    GET     /plans                  recent plans
    GET     /plans/{plan_id}        one plan: steps, approval, results
    POST    /plans/{plan_id}/approve   {"plan_hash"}: approvers only (SKYCTL_APPROVER_TOKEN / SKYCTL_PLAN_APPROVERS)
    POST    /plans/{plan_id}/execute   {"plan_hash"}: run an approved plan
    POST    /plans/{plan_id}/cancel
    *       /mcp                    MCP over streamable HTTP

MCP sessions are stateless by default (any instance serves any request).
Elicitation and progress need a stateful streaming session: set
SKYCTL_MCP_STATEFUL=true and give the service session affinity.

Auth is the chain in SKYCTL_AUTH (skyctl.platform.auth); /healthz and the
OPTIONS handshake are public.

How /runs executes is a *capability decision* (Runtime.run_strategy):
on platforms that keep CPU after a response it uses a worker thread; on
Lambda, request-billed Cloud Run and Container Apps it enqueues a
`task.run` invocation for the worker, because background threads there are
throttled or frozen once the response is sent.
"""

from __future__ import annotations

import contextlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .agent import AgentRunner, PolicyCeilingError, RunLog, TaskConfigError, TaskSpec, apply_deployment_ceilings
from .manager import BskyManager
from .platform.auth import AuthChain, AuthError, build_auth_chain, plan_approvers
from .platform.capabilities import CapabilityError
from .platform.invocation import Dispatcher, InvocationError, decode_http, default_dispatcher
from .platform.logging import log_event, set_trace
from .platform.runtime import Runtime
from .scheduler import BskyScheduler
from .settings import Settings, load_settings
from .tools import ToolPolicy


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, chain: AuthChain, runtime: Runtime):
        super().__init__(app)
        self.chain = chain
        self.runtime = runtime

    async def dispatch(self, request: Request, call_next):
        set_trace(self.runtime.platform.trace_from_headers(request.headers))
        if request.url.path == "/healthz" or (request.method == "OPTIONS" and request.url.path == "/invoke"):
            return await call_next(request)
        if not self.chain.methods:
            return JSONResponse(
                {"error": "No authentication configured; refusing. Set SKYCTL_HTTP_TOKEN or SKYCTL_AUTH "
                          "(e.g. 'platform' behind Cloud Run IAM / Easy Auth)."},
                status_code=503,
            )
        try:
            request.state.principal = self.chain.authenticate(request.headers)
        except AuthError as e:
            return JSONResponse({"error": "unauthorized", "detail": str(e)}, status_code=401)
        return await call_next(request)


class RunRegistry:
    """In-memory view of runs started by this instance; transcripts are in the state store."""

    def __init__(self):
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, run_id: str, task: TaskSpec, status: str = "running") -> None:
        with self._lock:
            self._runs[run_id] = {"run_id": run_id, "status": status, "task": task.to_dict(), "events": []}

    def event(self, run_id: str, entry: dict) -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id]["events"].append(entry)

    def finish(self, run_id: str, result: dict) -> None:
        with self._lock:
            if run_id in self._runs:
                self._runs[run_id].update(status=result["status"], result=result)

    def get(self, run_id: str) -> Optional[dict]:
        with self._lock:
            return self._runs.get(run_id)


def create_app(
    settings: Optional[Settings] = None,
    manager: Optional[BskyManager] = None,
    scheduler: Optional[BskyScheduler] = None,
    runner: Optional[AgentRunner] = None,
    mcp_server=None,
    runtime: Optional[Runtime] = None,
    dispatcher: Optional[Dispatcher] = None,
    auth_chain: Optional[AuthChain] = None,
    workers: int = 4,
) -> Starlette:
    settings = settings or load_settings()
    runtime = runtime or Runtime(settings)
    if auth_chain is None:
        env = dict(os.environ)
        if settings.http_token:
            env["SKYCTL_HTTP_TOKEN"] = settings.http_token
        auth_chain = build_auth_chain(env=env)
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="skyctl-run")
    runs = RunRegistry()
    state: dict = {"manager": manager, "scheduler": scheduler, "runner": runner, "dispatcher": dispatcher}

    def get_manager() -> BskyManager:
        if state["manager"] is None:
            state["manager"] = BskyManager.from_settings(settings)
        return state["manager"]

    def get_scheduler() -> BskyScheduler:
        if state["scheduler"] is None:
            state["scheduler"] = BskyScheduler.from_settings(get_manager(), settings)
        return state["scheduler"]

    def get_runner() -> AgentRunner:
        if state["runner"] is None:
            state["runner"] = AgentRunner.from_settings(get_manager(), settings)
        return state["runner"]

    def get_dispatcher() -> Dispatcher:
        if state["dispatcher"] is None:
            state["dispatcher"] = default_dispatcher(
                runtime, manager=get_manager(), scheduler=get_scheduler(), runner=get_runner()
            )
        return state["dispatcher"]

    def default_policy_fields() -> dict:
        d = settings.agent
        return {"accounts": d.accounts, "allow_writes": d.allow_writes, "allow_delete": d.allow_delete,
                "max_writes": d.max_writes, "max_turns": d.max_turns}

    # -- routes --------------------------------------------------------

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def readyz(request: Request) -> Response:
        try:
            aliases = get_manager().aliases()
            runtime.object_store().exists("readyz")
            return JSONResponse({"ok": True, "accounts": aliases, "cloud": runtime.platform.name,
                                 "runtime": runtime.describe(), "auth": auth_chain.names})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=503)

    async def tick(request: Request) -> Response:
        import anyio

        result = await anyio.to_thread.run_sync(get_scheduler().tick)
        return JSONResponse(result.to_dict())

    async def invoke_options(request: Request) -> Response:
        # CloudEvents webhook abuse protection (Event Grid, others): echo the origin back.
        origin = request.headers.get("webhook-request-origin")
        headers = {"WebHook-Allowed-Origin": origin, "WebHook-Allowed-Rate": "*"} if origin else {}
        return Response(status_code=200, headers=headers)

    async def invoke(request: Request) -> Response:
        import anyio

        try:
            invocations = decode_http(request.headers, await request.body(), runtime.platform)
        except InvocationError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        outcomes = []
        for inv in invocations:
            outcome = await anyio.to_thread.run_sync(get_dispatcher().dispatch, inv)
            log_event("invocation", kind=inv.kind, source=inv.source, attempt=inv.attempt, ok=outcome.ok, error=outcome.error)
            if inv.kind == "eventgrid.validate" and outcome.ok:
                return JSONResponse(outcome.result)
            outcomes.append(outcome)
        # Status codes drive the pushing platform's retry: 5xx = retry, 4xx = permanent, 2xx = done.
        if all(o.ok for o in outcomes):
            status = 200
        elif any(o.retry for o in outcomes):
            status = 503
        else:
            status = 422
        return JSONResponse({"outcomes": [o.to_dict() for o in outcomes]}, status_code=status)

    async def start_run(request: Request) -> Response:
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        fields = {**default_policy_fields(), **body}
        try:
            task = TaskSpec.from_dict(fields)
            apply_deployment_ceilings(task, settings.agent)
        except (TaskConfigError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except PolicyCeilingError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        try:
            strategy = runtime.run_strategy()
        except CapabilityError as e:
            return JSONResponse({"error": str(e)}, status_code=501)

        runner = get_runner()
        run_id = runner._clock().strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()

        if strategy == "queue":
            runtime.queue().send({"kind": "task.run", "run_id": run_id, "task": task.to_dict()}, dedupe_id=run_id)
            RunLog(runner.state, run_id).append("queued", run_id=run_id, task=task.to_dict())
            runs.start(run_id, task, status="queued")
            return JSONResponse({"run_id": run_id, "status": "queued", "strategy": "queue"}, status_code=202)

        runs.start(run_id, task)

        def work():
            try:
                result = runner.run(task, run_id=run_id, on_event=lambda entry: runs.event(run_id, entry))
                runs.finish(run_id, result.to_dict())
            except Exception as e:  # pragma: no cover - defensive
                runs.finish(run_id, {"status": "error", "error": str(e), "run_id": run_id})

        pool.submit(work)
        return JSONResponse({"run_id": run_id, "status": "running", "strategy": "thread"}, status_code=202)

    async def get_run(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        local = runs.get(run_id)
        if local is not None and local["status"] != "queued":
            return JSONResponse(local)
        runner = get_runner()
        if runner.state is None:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        try:
            events = RunLog.load(runner.state, run_id)
        except FileNotFoundError:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        final = next((e for e in reversed(events) if e.get("type") == "result"), None)
        status = final["status"] if final else ("running" if any(e["type"] == "task" for e in events) else "queued")
        return JSONResponse({"run_id": run_id, "status": status, "result": final, "events": events})

    async def approve_run(request: Request) -> Response:
        run_id = request.path_params["run_id"]
        call_ids = None
        if await request.body():
            body = await request.json()
            call_ids = body.get("call_ids") if isinstance(body, dict) else None
        import anyio

        try:
            records = await anyio.to_thread.run_sync(lambda: get_runner().approve(run_id, call_ids))
        except FileNotFoundError:
            return JSONResponse({"error": "unknown run"}, status_code=404)
        return JSONResponse({"run_id": run_id, "approved": records})

    # -- plans ---------------------------------------------------------------
    def plan_context(request: Request):
        from .planning import PlanStore
        from .tools import ToolContext, ToolPolicy

        d = settings.agent
        ctx = ToolContext(get_manager(), policy=ToolPolicy(accounts=d.accounts, allow_writes=d.allow_writes,
                                                           allow_delete=d.allow_delete, max_writes=d.max_writes),
                          scheduler=state["scheduler"])
        ctx.plans = PlanStore(runtime.object_store())
        principal = getattr(request.state, "principal", None)
        ctx.principal = principal.subject if principal is not None else "anonymous"
        return ctx

    async def list_plans(request: Request) -> Response:
        ctx = plan_context(request)
        return JSONResponse({"plans": ctx.plans.list(status=request.query_params.get("status"))})

    async def get_plan(request: Request) -> Response:
        record = plan_context(request).plans.get(request.path_params["plan_id"])
        return JSONResponse(record) if record else JSONResponse({"error": "unknown plan"}, status_code=404)

    async def approve_plan(request: Request) -> Response:
        from .planning import PlanError

        ctx = plan_context(request)
        approvers = plan_approvers()
        if ctx.principal.lower() not in approvers:
            return JSONResponse({"error": f"{ctx.principal!r} may not approve plans (SKYCTL_APPROVER_TOKEN / "
                                          "SKYCTL_PLAN_APPROVERS)"}, status_code=403)
        body = await request.json() if await request.body() else {}
        try:
            record = ctx.plans.approve(request.path_params["plan_id"], str(body.get("plan_hash", "")),
                                       by=ctx.principal, via="http")
        except PlanError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"plan_id": record["id"], "status": record["status"], "approval": record["approval"]})

    async def execute_plan_route(request: Request) -> Response:
        import anyio

        from .planning import ApprovalRequired, PlanError, execute_plan
        from .tools import registry
        from .tools.plan_tools import _detail

        ctx = plan_context(request)
        body = await request.json() if await request.body() else {}
        try:
            record = await anyio.to_thread.run_sync(lambda: execute_plan(
                ctx.plans, request.path_params["plan_id"], str(body.get("plan_hash", "")), ctx, registry))
        except ApprovalRequired as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except PlanError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(_detail(record))

    async def cancel_plan(request: Request) -> Response:
        from .planning import PlanError

        ctx = plan_context(request)
        try:
            record = ctx.plans.cancel(request.path_params["plan_id"], by=ctx.principal)
        except PlanError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"plan_id": record["id"], "status": record["status"]})

    routes = [
        Route("/healthz", healthz),
        Route("/readyz", readyz),
        Route("/invoke", invoke, methods=["POST"]),
        Route("/invoke", invoke_options, methods=["OPTIONS"]),
        Route("/tick", tick, methods=["POST"]),
        Route("/runs", start_run, methods=["POST"]),
        Route("/runs/{run_id}", get_run),
        Route("/runs/{run_id}/approve", approve_run, methods=["POST"]),
        Route("/plans", list_plans),
        Route("/plans/{plan_id}", get_plan),
        Route("/plans/{plan_id}/approve", approve_plan, methods=["POST"]),
        Route("/plans/{plan_id}/execute", execute_plan_route, methods=["POST"]),
        Route("/plans/{plan_id}/cancel", cancel_plan, methods=["POST"]),
    ]

    # -- MCP -----------------------------------------------------------
    # mcp_server: None → build one from settings; False → do not mount MCP.
    lifespans = []
    if mcp_server is False:
        mcp_server = None
    elif mcp_server is None and os.getenv("SKYCTL_HTTP_MCP", "true").lower() not in ("0", "false", "no"):
        from .mcp_server import McpConfig, build_mcp_server

        policy = ToolPolicy(
            accounts=settings.agent.accounts,
            allow_writes=settings.agent.allow_writes,
            allow_delete=settings.agent.allow_delete,
            max_writes=settings.agent.max_writes,
        )
        config = McpConfig.from_env(default_profile="plan", default_approval="out_of_band")
        config.principal = "mcp-http"
        config.grounding = "error" if os.getenv("SKYCTL_MCP_STATEFUL", "").lower() in ("1", "true", "yes") else "warning"
        stateful = os.getenv("SKYCTL_MCP_STATEFUL", "").lower() in ("1", "true", "yes")
        if config.approval == "elicit" and not stateful:
            log_event("mcp.config", level="warning",
                      message="SKYCTL_MCP_APPROVAL=elicit needs SKYCTL_MCP_STATEFUL=true (and session affinity); "
                              "stateless sessions cannot carry the elicitation round-trip.")
        mcp_server = build_mcp_server(
            get_manager(), policy, config=config, state=runtime.object_store(), runner=get_runner(),
            scheduler=state["scheduler"], streamable_http_path="/",
            stateless_http=not stateful, json_response=not stateful, **_transport_security(),
        )
    if mcp_server is not None:
        mcp_app = mcp_server.streamable_http_app()
        routes.append(Mount("/mcp", app=mcp_app))
        lifespans.append(lambda: mcp_server.session_manager.run())

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for ls in lifespans:
                await stack.enter_async_context(ls())
            yield
        pool.shutdown(wait=False)

    return Starlette(
        routes=routes,
        middleware=[Middleware(AuthMiddleware, chain=auth_chain, runtime=runtime)],
        lifespan=lifespan,
    )


def _transport_security() -> dict:
    """
    MCP SDKs protect *unauthenticated localhost* servers against DNS rebinding
    by checking the Host header. This server always sits behind the auth
    chain and, in the cloud, behind a load balancer whose Host is your domain.
    Set SKYCTL_MCP_ALLOWED_HOSTS (e.g. "mcp.example.com,*.run.app") to keep
    the check with the right hosts; otherwise authentication is the guard.
    """
    try:
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError:          # older SDKs have no such check
        return {}
    hosts = [h.strip() for h in os.getenv("SKYCTL_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if hosts:
        origins = [o.strip() for o in os.getenv("SKYCTL_MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
        return {"transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)}
    return {"transport_security": TransportSecuritySettings(enable_dns_rebinding_protection=False)}


def serve(host: str = "0.0.0.0", port: Optional[int] = None) -> None:
    import uvicorn

    from .platform.logging import configure_logging
    from .platform.runtime import get_runtime

    runtime = get_runtime()
    configure_logging(runtime.platform, os.getenv("SKYCTL_LOG_FORMAT", "auto"))
    uvicorn.run(create_app(runtime.settings, runtime=runtime), host=host, port=port or runtime.settings.port,
                log_level="info")
