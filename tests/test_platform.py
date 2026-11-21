"""The platform framework: detection, capabilities, runtime strategies, invocation, auth, logging, cron."""

import base64
import io
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from skyctl.adapters import MemoryObjectStore, ObjectJobStore
from skyctl.adapters.queues import MemoryQueue
from skyctl.platform import Cap, CapabilityError, Runtime, detect_platform, get_platform, platforms
from skyctl.platform.invocation import (
    Dispatcher,
    Invocation,
    InvocationError,
    RetryableError,
    decode_http,
    decode_json,
    decode_lambda_event,
    default_dispatcher,
    run_worker,
)
from skyctl.scheduler import BskyScheduler
from skyctl.settings import AgentDefaults, Settings


# ----------------------------------------------------------------------
# Detection and capabilities
# ----------------------------------------------------------------------

@pytest.mark.parametrize("env,expected", [
    ({}, ("local", "local")),
    ({"AWS_LAMBDA_FUNCTION_NAME": "f"}, ("aws", "lambda")),
    ({"ECS_CONTAINER_METADATA_URI_V4": "http://x"}, ("aws", "ecs")),
    ({"CONTAINER_APP_NAME": "a"}, ("azure", "container-apps")),
    ({"CONTAINER_APP_NAME": "a", "CONTAINER_APP_JOB_NAME": "j"}, ("azure", "container-apps-job")),
    ({"K_SERVICE": "s"}, ("gcp", "cloud-run")),
    ({"CLOUD_RUN_JOB": "j", "K_SERVICE": "s"}, ("gcp", "cloud-run-job")),
    ({"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, ("kubernetes", "deployment")),
    # A managed cloud runtime wins over generic Kubernetes markers.
    ({"KUBERNETES_SERVICE_HOST": "10.0.0.1", "K_SERVICE": "s"}, ("gcp", "cloud-run")),
    ({"SKYCTL_PLATFORM": "compose"}, ("compose", "compose")),
    ({"SKYCTL_CLOUD": "gcp", "SKYCTL_COMPUTE": "cloud-run-instance"}, ("gcp", "cloud-run-instance")),
])
def test_detection(env, expected):
    platform, compute = detect_platform(env)
    assert (platform.name, compute) == expected


def test_every_platform_is_well_formed():
    found = platforms()
    assert {"local", "compose", "aws", "azure", "gcp", "kubernetes"} <= set(found)
    for p in found.values():
        assert p.default_compute in p.compute_profiles, p.name
        for prof in p.compute_profiles.values():
            assert prof.kind in ("service", "job", "function", "daemon"), (p.name, prof.name)
            assert all(c.count(".") == 1 for c in p.capabilities(prof.name)), (p.name, prof.name)
    with pytest.raises(ValueError):
        get_platform("nimbus")
    with pytest.raises(ValueError):
        get_platform("aws").profile("app-runner")
    assert get_platform("k8s").name == "kubernetes"


def test_capability_facts_that_drive_strategy():
    aws, gcp, azure = get_platform("aws"), get_platform("gcp"), get_platform("azure")
    assert Cap.BACKGROUND_AFTER_RESPONSE not in aws.capabilities("lambda")
    assert Cap.BACKGROUND_AFTER_RESPONSE in aws.capabilities("ecs")
    assert Cap.BACKGROUND_AFTER_RESPONSE not in gcp.capabilities("cloud-run")
    assert Cap.BACKGROUND_AFTER_RESPONSE in gcp.capabilities("cloud-run-instance")
    assert Cap.SCHEDULE_TIMEZONE not in azure.capabilities("container-apps-job")
    assert Cap.SCHEDULE_TIMEZONE in gcp.capabilities("cloud-run-job")
    assert aws.profile("lambda").limits.job_timeout_s == 900


def _runtime(platform="local", compute=None, **settings_kw):
    settings = Settings(state_url="memory://rt", **settings_kw)
    return Runtime(settings, platform=get_platform(platform), compute=compute)


def test_run_strategy_is_a_capability_decision():
    assert _runtime("local").run_strategy() == "thread"
    assert _runtime("aws", "ecs").run_strategy() == "thread"
    with pytest.raises(CapabilityError) as e:
        _runtime("aws", "lambda").run_strategy()
    assert "SKYCTL_QUEUE_URL" in str(e.value)
    with pytest.raises(CapabilityError):
        _runtime("gcp", "cloud-run").run_strategy()
    assert _runtime("gcp", "cloud-run", queue_url="memory://q1").run_strategy() == "queue"


def test_runtime_reports_and_requires():
    rt = _runtime("azure", "container-apps-job")
    assert rt.has(Cap.STATE_ATOMIC_CAS)           # memory store is atomic
    rt.require(Cap.SCHEDULED_JOBS, "tick")
    with pytest.raises(CapabilityError) as e:
        rt.require(Cap.SCHEDULE_TIMEZONE, "Europe/London schedules", "Use UTC.")
    assert "azure/container-apps-job" in str(e.value) and "Use UTC." in str(e.value)
    info = rt.describe()
    assert info["platform"] == "azure" and info["compute_kind"] == "job"
    assert info["atomic_cas"] is True


def test_non_atomic_state_drops_the_capability_and_warns():
    pytest.importorskip("fsspec")
    from skyctl.adapters.fsspec_store import FsspecObjectStore

    rt = Runtime(Settings(state_url="memory://x"), platform=get_platform("local"))
    assert rt.has(Cap.STATE_ATOMIC_CAS)
    # An unregistered scheme (e.g. sftp://) falls back to fsspec, which cannot do atomic CAS.
    rt._state = FsspecObjectStore("memory://non-atomic")
    assert not rt.has(Cap.STATE_ATOMIC_CAS)
    with pytest.warns(RuntimeWarning, match="compare-and-swap"):
        rt.job_store()


def test_service_registry_routes_bucket_schemes_to_native_cas_stores():
    from skyctl.adapters.azure_blob import AzureBlobObjectStore
    from skyctl.adapters.gcs import GcsObjectStore
    from skyctl.adapters.s3 import S3ObjectStore
    from skyctl.platform import services

    s3 = services.build("state", "s3://bucket/skyctl")
    assert isinstance(s3, S3ObjectStore) and (s3.bucket, s3.prefix) == ("bucket", "skyctl")
    assert isinstance(services.build("state", "az://container/p"), AzureBlobObjectStore)
    assert isinstance(services.build("state", "gs://b/p"), GcsObjectStore)
    assert services.build("queue", "none") is None
    with pytest.raises(ValueError):
        services.build("queue", "kafka://x")
    services.register_scheme("queue", "test", lambda url, **kw: "custom")
    assert services.build("queue", "test://x") == "custom"


# ----------------------------------------------------------------------
# Invocation decoding
# ----------------------------------------------------------------------

def b64(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


def test_decode_plain_and_errors():
    inv = decode_json('{"kind": "tick"}', source="cli")
    assert (inv.kind, inv.source) == ("tick", "cli")
    with pytest.raises(InvocationError):
        decode_json("not json")
    with pytest.raises(InvocationError):
        decode_json({"no": "kind"})


def test_decode_cloudevents_binary_and_structured():
    [inv] = decode_http({"ce-specversion": "1.0", "ce-type": "skyctl.tick", "ce-id": "e1"}, b'{"kind": "tick"}')
    assert inv.kind == "tick" and inv.id == "e1" and inv.source == "cloudevents:skyctl.tick"
    structured = {"specversion": "1.0", "type": "skyctl.post", "id": "e2", "source": "/x",
                  "data": {"kind": "post.send", "job_id": "j1"}}
    [inv] = decode_http({"content-type": "application/cloudevents+json"}, json.dumps(structured).encode())
    assert inv.payload == {"kind": "post.send", "job_id": "j1"}
    batch = [structured, {**structured, "id": "e3", "data": None, "data_base64": b64({"kind": "tick"})}]
    invs = decode_http({"content-type": "application/cloudevents-batch+json"}, json.dumps(batch).encode())
    assert [i.kind for i in invs] == ["post.send", "tick"]


def test_decode_pubsub_push_and_eventarc():
    push = {"message": {"data": b64({"kind": "post.send", "job_id": "j9"}), "messageId": "m1"},
            "subscription": "projects/p/subscriptions/s", "deliveryAttempt": 3}
    [inv] = decode_http({"content-type": "application/json"}, json.dumps(push).encode())
    assert (inv.kind, inv.id, inv.attempt, inv.source) == ("post.send", "m1", 3, "pubsub-push")
    # Eventarc wraps the same envelope in a binary-mode CloudEvent.
    [inv] = decode_http({"ce-specversion": "1.0", "ce-type": "google.cloud.pubsub.topic.v1.messagePublished"},
                        json.dumps(push).encode())
    assert inv.source == "eventarc-pubsub" and inv.payload["job_id"] == "j9"


def test_decode_event_grid_schema_and_validation():
    events = [{"id": "g1", "eventType": "skyctl.tick", "data": {"kind": "tick"}, "subject": "s", "dataVersion": "1"}]
    [inv] = decode_http({}, json.dumps(events).encode())
    assert inv.kind == "tick" and inv.source == "eventgrid:skyctl.tick"
    validation = [{"id": "v", "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
                   "data": {"validationCode": "abc123"}}]
    [inv] = decode_http({}, json.dumps(validation).encode())
    assert inv.kind == "eventgrid.validate"
    assert default_dispatcher().dispatch(inv).result == {"validationResponse": "abc123"}


def test_decode_lambda_shapes():
    sqs_event = {"Records": [
        {"eventSource": "aws:sqs", "messageId": "a", "body": '{"kind": "tick"}', "attributes": {"ApproximateReceiveCount": "2"}},
        {"eventSource": "aws:sqs", "messageId": "b", "body": '{"kind": "post.send", "job_id": "x"}', "attributes": {}},
    ]}
    shape, items = decode_lambda_event(sqs_event)
    assert shape == "sqs" and [(i, inv.kind, inv.attempt) for i, inv in items] == [("a", "tick", 2), ("b", "post.send", 1)]
    shape, [(_, inv)] = decode_lambda_event({"kind": "tick"})
    assert shape == "direct" and inv.source == "lambda"
    shape, [(_, inv)] = decode_lambda_event({"detail-type": "Scheduled Event", "source": "aws.events", "detail": {"kind": "tick"}})
    assert inv.source == "eventbridge:aws.events"
    url_event = {"requestContext": {}, "isBase64Encoded": True, "headers": {"content-type": "application/json"},
                 "body": b64({"kind": "tick"})}
    shape, [(_, inv)] = decode_lambda_event(url_event)
    assert shape == "http" and inv.kind == "tick"


def test_trace_context_per_platform():
    aws = get_platform("aws").trace_from_headers(
        {"X-Amzn-Trace-Id": "Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;Sampled=1"})
    assert aws.trace_id == "1-5759e988-bd862e3fe1be46a994272793" and aws.sampled
    gcp = get_platform("gcp").trace_from_headers({"X-Cloud-Trace-Context": "105445aa7843bc8bf206b12000100000/1;o=1"})
    assert (gcp.trace_id, gcp.span_id, gcp.sampled) == ("105445aa7843bc8bf206b12000100000", "1", True)
    w3c = get_platform("azure").trace_from_headers({"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"})
    assert w3c.trace_id == "4bf92f3577b34da6a3ce929d0e0e4736" and w3c.sampled


# ----------------------------------------------------------------------
# Dispatch, worker, Lambda handler
# ----------------------------------------------------------------------

def test_dispatcher_classifies_failures():
    def boom(inv):
        raise RuntimeError("transient")

    def bad(inv):
        raise ValueError("bad input")

    def later(inv):
        raise RetryableError("try again")

    d = Dispatcher({"boom": boom, "bad": bad, "later": later, "ok": lambda inv: 42})
    assert d.dispatch(Invocation("ok", {})).result == 42
    assert d.dispatch(Invocation("boom", {})).retry is True
    assert d.dispatch(Invocation("later", {})).retry is True
    assert d.dispatch(Invocation("bad", {})).retry is False
    unknown = d.dispatch(Invocation("nope", {}))
    assert not unknown.ok and not unknown.retry


def test_default_dispatcher_end_to_end(manager, fake_atproto):
    store = MemoryObjectStore()
    scheduler = BskyScheduler(manager, job_store=ObjectJobStore(store))
    sp = scheduler.schedule("personal", "via post.send", run_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    d = default_dispatcher(_runtime(), manager=manager, scheduler=scheduler)
    first = d.dispatch(Invocation("post.send", {"kind": "post.send", "job_id": sp.id}))
    assert first.ok and first.result["sent"] is True
    dup = d.dispatch(Invocation("post.send", {"kind": "post.send", "job_id": sp.id}))
    assert dup.ok and dup.result["noop"] is True                 # at-least-once delivery is harmless
    assert d.dispatch(Invocation("tick", {"kind": "tick"})).result["sent"] == []
    assert not d.dispatch(Invocation("post.send", {"kind": "post.send"})).ok


def test_task_run_invocation_enforces_ceilings_and_idempotency(manager, fake_atproto):
    from skyctl.agent import AgentRunner
    from skyctl.ports import ChatTurn, ToolCall

    class Script:
        model, tool_format = "s", "anthropic"

        def chat(self, system, messages, tools):
            if len(messages) == 1:
                return ChatTurn("", [ToolCall("c", "post", {"account": "work", "text": "queued hello"})], "tool_use")
            return ChatTurn("done", [], "end")

    state = MemoryObjectStore()
    runner = AgentRunner(manager, provider=Script(), state=state)
    rt = _runtime(agent=AgentDefaults(allow_writes=True, max_writes=1, accounts=["work"]))
    d = default_dispatcher(rt, manager=manager, runner=runner)
    payload = {"kind": "task.run", "run_id": "r1", "task": {"goal": "say hi", "allow_writes": True, "max_writes": 50}}
    out = d.dispatch(Invocation("task.run", payload))
    assert out.ok and out.result["writes"] == 1 and out.result["task"]["max_writes"] == 1
    again = d.dispatch(Invocation("task.run", payload))              # redelivered message
    assert again.ok and again.result["writes"] == 0
    assert len(manager.client("work")._client.sent) == 1
    denied = d.dispatch(Invocation("task.run", {"kind": "task.run", "task": {"goal": "x", "allow_delete": True}}))
    assert not denied.ok and "deletion is disabled" in denied.error and not denied.retry


def test_worker_acks_retries_and_drops():
    q = MemoryQueue()
    calls = {"flaky": 0}

    def flaky(inv):
        calls["flaky"] += 1
        if calls["flaky"] < 2:
            raise RuntimeError("blip")
        return "ok"

    d = Dispatcher({"ok": lambda inv: 1, "flaky": flaky, "bad": lambda inv: (_ for _ in ()).throw(ValueError("no"))})
    for kind in ("ok", "flaky", "bad"):
        q.send({"kind": kind})
    q.send({"not": "an invocation"})
    stats = run_worker(q, d, max_messages=10, idle_exit_seconds=0.2, retry_base_seconds=0, wait_seconds=0)
    assert stats.to_dict() == {"processed": 5, "succeeded": 2, "retried": 1, "dropped": 2}
    assert len(q) == 0


def test_lambda_handler_reports_partial_batch_failures():
    from skyctl.platform.lambda_handler import handler

    d = Dispatcher({"ok": lambda inv: 1, "flaky": lambda inv: (_ for _ in ()).throw(RuntimeError("x")),
                    "bad": lambda inv: (_ for _ in ()).throw(ValueError("y"))})
    event = {"Records": [
        {"eventSource": "aws:sqs", "messageId": m, "body": json.dumps({"kind": k}), "attributes": {}}
        for m, k in (("1", "ok"), ("2", "flaky"), ("3", "bad"))
    ]}
    assert handler(event, None, dispatcher=d) == {"batchItemFailures": [{"itemIdentifier": "2"}]}
    assert handler({"kind": "ok"}, None, dispatcher=d)["ok"] is True
    with pytest.raises(RuntimeError):
        handler({"kind": "flaky"}, None, dispatcher=d)     # async invoke → Lambda retries


# ----------------------------------------------------------------------
# Auth
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def rsa_key():
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key, **claims):
    import jwt

    base = {"iss": "https://accounts.google.com", "aud": "https://svc.run.app", "sub": "123",
            "email": "scheduler@proj.iam.gserviceaccount.com", "exp": int(time.time()) + 300}
    base.update(claims)
    return jwt.encode(base, key, algorithm="RS256")


def test_auth_chain_bearer_and_google_oidc(rsa_key):
    from skyctl.platform.auth import AuthChain, AuthError, StaticBearer, build_auth_chain, google_oidc

    chain = AuthChain([
        StaticBearer("s3cret"),
        google_oidc("https://svc.run.app", allowed=["scheduler@proj.iam.gserviceaccount.com"],
                    key_resolver=lambda token: rsa_key.public_key()),
    ])
    assert chain.authenticate({"Authorization": "Bearer s3cret"}).method == "bearer"
    p = chain.authenticate({"Authorization": f"Bearer {_token(rsa_key)}"})
    assert p.method == "google-oidc" and p.subject == "scheduler@proj.iam.gserviceaccount.com"
    for bad in (
        {"Authorization": "Bearer wrong"},
        {"Authorization": f"Bearer {_token(rsa_key, aud='https://other.run.app')}"},
        {"Authorization": f"Bearer {_token(rsa_key, exp=int(time.time()) - 3600)}"},
        {"Authorization": f"Bearer {_token(rsa_key, email='intruder@evil.iam.gserviceaccount.com')}"},
        {},
    ):
        with pytest.raises(AuthError):
            chain.authenticate(bad)
    # A token from an issuer no method handles is rejected, not accepted.
    with pytest.raises(AuthError):
        chain.authenticate({"Authorization": f"Bearer {_token(rsa_key, iss='https://evil.example')}"})
    assert build_auth_chain(env={"SKYCTL_HTTP_TOKEN": "t"}).names == ["bearer"]
    assert build_auth_chain(env={"SKYCTL_HTTP_ALLOW_ANON": "true"}).names == ["platform"]
    assert build_auth_chain(env={}).names == []
    with pytest.raises(ValueError):
        build_auth_chain("google-oidc", env={})          # audience required
    with pytest.raises(ValueError):
        build_auth_chain("kerberos", env={})


def test_auth_entra_accepts_v1_and_v2_issuers(rsa_key):
    from skyctl.platform.auth import entra_oidc

    m = entra_oidc("tenant-1", "api://skyctl", allowed=["oid-1"], key_resolver=lambda t: rsa_key.public_key())
    for iss in ("https://login.microsoftonline.com/tenant-1/v2.0", "https://sts.windows.net/tenant-1/"):
        tok = _token(rsa_key, iss=iss, aud="api://skyctl", oid="oid-1")
        assert m.authenticate({"authorization": f"Bearer {tok}"}).subject == "scheduler@proj.iam.gserviceaccount.com"


def test_auth_aws_alb_signer_check():
    import jwt
    from cryptography.hazmat.primitives.asymmetric import ec

    from skyctl.platform.auth import AuthError, AwsAlbOidc

    key = ec.generate_private_key(ec.SECP256R1())
    arn = "arn:aws:elasticloadbalancing:us-east-1:123:loadbalancer/app/skyctl/abc"
    m = AwsAlbOidc("us-east-1", arn, key_resolver=lambda kid: key.public_key())

    def alb_token(signer):
        return jwt.encode({"sub": "u1", "email": "me@example.com", "exp": int(time.time()) + 60}, key,
                          algorithm="ES256", headers={"kid": "k1", "signer": signer, "iss": "https://idp"})

    assert m.authenticate({"x-amzn-oidc-data": alb_token(arn)}).subject == "me@example.com"
    with pytest.raises(AuthError):
        m.authenticate({"x-amzn-oidc-data": alb_token(arn.replace("abc", "other"))})
    assert m.authenticate({}) is None


# ----------------------------------------------------------------------
# Logging and cron
# ----------------------------------------------------------------------

def test_log_shapes_per_platform(monkeypatch):
    from skyctl.platform import logging as plog
    from skyctl.platform.base import TraceContext

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj")
    token = plog.set_trace(TraceContext("abc", "7", True))
    try:
        plog.set_platform(get_platform("gcp"))
        line = json.loads(plog.format_event("hello", "warning", x=1))
        assert line["severity"] == "WARNING" and line["logging.googleapis.com/trace"] == "projects/proj/traces/abc"
        plog.set_platform(get_platform("azure"))
        assert json.loads(plog.format_event("hello"))["operation_Id"] == "abc"
        plog.set_platform(get_platform("aws"))
        assert json.loads(plog.format_event("hello"))["xray_trace_id"] == "abc"
        buf = io.StringIO()
        plog.emit_metric("posts_sent", 2, stream=buf, account="work")
        emf = json.loads(buf.getvalue())
        assert emf["posts_sent"] == 2 and emf["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["account"]]
        plog.set_platform(get_platform("local"))
        buf = io.StringIO()
        plog.emit_metric("posts_sent", 2, stream=buf)
        assert json.loads(buf.getvalue())["metric"] == "posts_sent"
    finally:
        plog._trace.reset(token)
        plog.set_platform(None)


@pytest.mark.parametrize("unix,eventbridge", [
    ("*/5 * * * *", "cron(0/5 * * * ? *)"),
    ("0 9 * * *", "cron(0 9 * * ? *)"),
    ("0 9 * * mon-fri", "cron(0 9 ? * MON-FRI *)"),
    ("0 9 * * 1-5", "cron(0 9 ? * 2-6 *)"),
    ("30 8 * * 0,6", "cron(30 8 ? * 1,7 *)"),
    ("0 0 1 * *", "cron(0 0 1 * ? *)"),
    ("15 */2 * * 7", "cron(15 0/2 ? * 1 *)"),
])
def test_cron_to_eventbridge(unix, eventbridge):
    from skyctl.platform.cron import CronSpec, to_eventbridge

    assert to_eventbridge(CronSpec(unix)) == eventbridge


def test_cron_rejects_what_eventbridge_cannot_express():
    from skyctl.platform.cron import CronError, CronSpec, to_eventbridge

    with pytest.raises(CronError, match="two schedules"):
        to_eventbridge(CronSpec("0 9 1 * mon"))
    with pytest.raises(CronError):
        CronSpec("* * *").fields()
    assert CronSpec("*/15 * * * *").min_interval_seconds() == 900
    assert CronSpec("0 9 * * *", "Etc/UTC").is_utc and not CronSpec("0 9 * * *", "Europe/London").is_utc


def test_daemon_schedules_dispatch(monkeypatch):
    from skyctl.platform.daemon import build_scheduler, load_schedules

    assert load_schedules("")[0]["invoke"] == {"kind": "tick"}
    with pytest.raises(ValueError):
        load_schedules('[{"name": "x"}]')
    fired = []
    d = Dispatcher({"tick": lambda inv: fired.append(inv.source)})
    sched = build_scheduler(d, [{"name": "t", "cron": "* * * * *", "timezone": "Europe/London", "invoke": {"kind": "tick"}}], blocking=False)
    job = sched.get_jobs()[0]
    assert str(job.trigger.timezone) == "Europe/London"
    job.func()
    assert fired == ["daemon:t"]


def test_worker_heartbeat_keeps_long_handlers_exclusive():
    """A handler outliving the visibility window is not redelivered while it runs."""
    q = MemoryQueue()
    q.send({"kind": "slow"})
    seen_by_other = []

    def slow(inv):
        for _ in range(6):
            time.sleep(0.1)
            seen_by_other.extend(q.receive(visibility_seconds=1))   # a second worker polling
        return "done"

    stats = run_worker(q, Dispatcher({"slow": slow}), visibility_seconds=0.25, heartbeat_seconds=0.05,
                       idle_exit_seconds=0.1, wait_seconds=0)
    assert stats.succeeded == 1 and seen_by_other == []


def test_run_time_ceiling_clamps_deadline():
    from skyctl.agent import TaskSpec, apply_deployment_ceilings

    task = apply_deployment_ceilings(TaskSpec(goal="g", deadline_seconds=3600), AgentDefaults(max_run_seconds=540))
    assert task.deadline_seconds == 540


# ----------------------------------------------------------------------
# Extensibility: a fourth platform / service arrives as a plugin
# ----------------------------------------------------------------------

def test_third_party_platform_and_service_plugins(monkeypatch):
    import importlib.metadata as md

    import skyctl.platform.base as base
    import skyctl.platform.services as services
    from skyctl.platform.capabilities import ComputeProfile

    class OciPlatform(base.Platform):
        name = "oci"
        default_compute = "container-instances"
        compute_profiles = {"container-instances": ComputeProfile("container-instances", "job",
                                                                  frozenset({Cap.SCHEDULED_JOBS}))}

        def detect(self, env):
            return "container-instances" if env.get("OCI_RESOURCE_PRINCIPAL_VERSION") else None

    class EP:
        def __init__(self, name, obj):
            self.name, self._obj = name, obj

        def load(self):
            return self._obj

    def fake_entry_points(group):
        return {"skyctl.platforms": [EP("oci", OciPlatform)],
                "skyctl.services": [EP("state:ocios", lambda url, **kw: ("oci-store", url))]}.get(group, [])

    monkeypatch.setattr(md, "entry_points", fake_entry_points)
    monkeypatch.setattr(base, "_ENTRY_POINTS_LOADED", False)
    monkeypatch.setattr(services, "_LOADED", False)
    monkeypatch.setattr(base, "_PLATFORMS", dict(base._PLATFORMS))
    try:
        platform, compute = detect_platform({"OCI_RESOURCE_PRINCIPAL_VERSION": "2.2"})
        assert (platform.name, compute) == ("oci", "container-instances")
        assert services.build("state", "ocios://bucket/p") == ("oci-store", "ocios://bucket/p")
    finally:
        services._FACTORIES.pop(("state", "ocios"), None)


def test_broken_plugin_does_not_break_the_core(monkeypatch, capsys):
    import importlib.metadata as md

    import skyctl.platform.base as base

    class Broken:
        name = "broken"

        def load(self):
            raise ImportError("missing dependency")

    monkeypatch.setattr(md, "entry_points", lambda group: [Broken()] if group == "skyctl.platforms" else [])
    monkeypatch.setattr(base, "_ENTRY_POINTS_LOADED", False)
    assert "aws" in platforms()
    assert "failed to load" in capsys.readouterr().out


def test_worker_stops_between_messages_when_asked():
    q = MemoryQueue()
    for _ in range(5):
        q.send({"kind": "ok"})
    handled = []
    stop_after_two = lambda: len(handled) >= 2          # noqa: E731
    run_worker(q, Dispatcher({"ok": lambda inv: handled.append(1)}), idle_exit_seconds=None,
               stop=stop_after_two, wait_seconds=0)
    assert len(handled) == 2 and len(q) == 3             # the rest stays queued for the next worker


# ----------------------------------------------------------------------
# Schedule slots: the portable idempotency key for scheduled runs
# ----------------------------------------------------------------------

def test_previous_fire_finds_the_slot():
    from skyctl.platform.cron import previous_fire

    utc = timezone.utc
    assert previous_fire("0 9 * * mon-fri", now=datetime(2026, 3, 3, 9, 0, 30, tzinfo=utc)) == datetime(2026, 3, 3, 9, 0, tzinfo=utc)
    # Saturday: the slot is still Friday's run.
    assert previous_fire("0 9 * * mon-fri", now=datetime(2026, 3, 7, 12, 0, tzinfo=utc)) == datetime(2026, 3, 6, 9, 0, tzinfo=utc)
    assert previous_fire("*/5 * * * *", now=datetime(2026, 3, 3, 9, 7, tzinfo=utc)) == datetime(2026, 3, 3, 9, 5, tzinfo=utc)
    assert previous_fire("0 0 1 * *", now=datetime(2026, 3, 20, tzinfo=utc)) == datetime(2026, 3, 1, tzinfo=utc)
    london = previous_fire("0 9 * * *", "Europe/London", now=datetime(2026, 7, 1, 8, 30, tzinfo=utc))
    assert london.isoformat() == "2026-07-01T09:00:00+01:00"


def test_scheduled_task_runs_key_the_ledger_by_slot(manager, fake_atproto, monkeypatch):
    from skyctl.agent import AgentRunner
    from skyctl.ports import ChatTurn, ToolCall
    import skyctl.platform.cron as cron_mod

    class Script:
        model, tool_format = "s", "anthropic"

        def chat(self, system, messages, tools):
            if len(messages) == 1:
                return ChatTurn("", [ToolCall("c", "post", {"account": "work", "text": "Good morning"})], "tool_use")
            return ChatTurn("done", [], "end")

    state = MemoryObjectStore()
    runner = AgentRunner(manager, provider=Script(), state=state)
    d = default_dispatcher(_runtime(agent=AgentDefaults(allow_writes=True)), manager=manager, runner=runner)
    payload = {"kind": "task.run", "task": {"goal": "greet", "name": "greet", "allow_writes": True},
               "slot": {"cron": "0 9 * * *", "timezone": "UTC"}}
    monday = datetime(2026, 3, 2, 9, 0, 20, tzinfo=timezone.utc)
    real = cron_mod.previous_fire
    monkeypatch.setattr(cron_mod, "previous_fire", lambda c, tz="UTC", now=None: real(c, tz, now or clock["now"]))
    clock = {"now": monday}
    assert d.dispatch(Invocation("task.run", payload)).result["writes"] == 1
    clock["now"] = monday + timedelta(minutes=3)          # platform retry / overlapping execution, same slot
    assert d.dispatch(Invocation("task.run", payload)).result["writes"] == 0
    clock["now"] = monday + timedelta(days=1)             # next day's slot
    assert d.dispatch(Invocation("task.run", payload)).result["writes"] == 1
    assert len(manager.client("work")._client.sent) == 2


def test_multiple_secret_urls_merge_in_order(monkeypatch):
    import os

    import skyctl.adapters as adapters_mod
    from skyctl.adapters.local import StaticSecretStore
    from skyctl.settings import load_settings

    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("SKYCTL_SECRETS", "aws-sm://app/config, aws-sm://app/http-token")
    maps = {"aws-sm://app/config": {"BSKY_ACCOUNTS": "work", "SKYCTL_HTTP_TOKEN": "from-config"},
            "aws-sm://app/http-token": {"SKYCTL_HTTP_TOKEN": "from-token-secret", "OTHER": "x"}}
    monkeypatch.setattr(adapters_mod, "secret_store_for", lambda url, env_file=".env": StaticSecretStore(maps[url]))
    settings = load_settings(force=True)
    assert settings.secret_urls == ["aws-sm://app/config", "aws-sm://app/http-token"]
    assert settings.http_token == "from-config" and os.environ["OTHER"] == "x"
