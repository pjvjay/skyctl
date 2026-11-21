"""
skyctl.platform.capabilities
============================
Capabilities and limits: how the framework generalises without sinking to
the lowest common denominator. Features *ask* for capabilities and pick a
strategy; deployment validation *rejects* impossible combinations before
anything is provisioned.

Limits are as documented by each provider at the time of writing and
change over time; they live here, in one table, so they are easy to audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional


class Cap:
    """Capability names. Strings, so plugins can add their own."""

    # compute
    BACKGROUND_AFTER_RESPONSE = "compute.background_after_response"   # work may continue after an HTTP response
    SCALE_TO_ZERO = "compute.scale_to_zero"
    LONG_RUNNING = "compute.long_running"                             # a process may live indefinitely (daemon mode)
    SCHEDULED_JOBS = "compute.scheduled_jobs"                         # platform runs a container on a cron
    EVENT_JOBS = "compute.event_jobs"                                 # platform runs a container per queue message / batch
    # scheduling
    SCHEDULE_TIMEZONE = "schedule.timezone"                           # cron evaluated in a named time zone
    SCHEDULE_ONE_SHOT = "schedule.one_shot"                           # native run-once-at-time
    # identity and access
    WORKLOAD_IDENTITY = "identity.workload"                           # no static credentials needed
    HTTP_PLATFORM_AUTH = "http.platform_auth"                         # platform can authenticate callers before the app
    SECRETS_NATIVE_INJECTION = "secrets.native_injection"             # secrets mounted as env by the platform
    # state
    STATE_ATOMIC_CAS = "state.atomic_cas"


@dataclass(frozen=True)
class Limits:
    job_timeout_s: Optional[int] = None          # longest scheduled/batch run
    request_timeout_s: Optional[int] = None      # longest HTTP request
    min_schedule_interval_s: int = 60            # finest cron granularity
    secret_bytes: Optional[int] = None           # largest secret value
    note: str = ""


@dataclass(frozen=True)
class ComputeProfile:
    """One way of running the image on a platform (e.g. Lambda vs ECS on AWS)."""

    name: str
    kind: str                                     # "service" | "job" | "function" | "daemon"
    caps: frozenset = field(default_factory=frozenset)
    limits: Limits = field(default_factory=Limits)
    description: str = ""

    def with_caps(self, *extra: str) -> "ComputeProfile":
        return replace(self, caps=self.caps | frozenset(extra))


class CapabilityError(RuntimeError):
    """A feature needs a capability this runtime or deployment target lacks."""

    def __init__(self, capability: str, feature: str, where: str, remedy: str = ""):
        self.capability, self.feature, self.where = capability, feature, where
        msg = f"{feature} needs '{capability}', which {where} does not provide."
        super().__init__(msg + (f" {remedy}" if remedy else ""))
