"""
skyctl.platform.runtime
=======================
The Runtime is the resolved environment of this process: which platform
and compute profile it runs on, and which services (state, secrets, queue,
delayed delivery) its settings point at. Everything the cloud entry points
need comes from here, built lazily and once.

    rt = get_runtime()
    rt.platform.name, rt.compute.name       # "gcp", "cloud-run"
    rt.has(Cap.BACKGROUND_AFTER_RESPONSE)   # False on request-billed Cloud Run
    rt.require(Cap.SCHEDULED_JOBS, "tick")  # raises CapabilityError with a remedy
    rt.job_store(), rt.queue(), rt.delayed_delivery()
"""

from __future__ import annotations

import os
import warnings
from functools import cached_property
from typing import Optional

from .base import Platform, detect_platform, get_platform
from .capabilities import Cap, CapabilityError, ComputeProfile
from . import services


class Runtime:
    def __init__(self, settings=None, platform: Optional[Platform] = None, compute: Optional[str] = None):
        from ..settings import Settings

        self.settings = settings or Settings.from_env()
        if platform is None:
            platform, detected = detect_platform()
            compute = compute or detected
        self.platform = platform
        self.compute: ComputeProfile = platform.profile(compute)

    # -- capabilities ------------------------------------------------------

    def capabilities(self) -> frozenset:
        caps = set(self.platform.capabilities(self.compute.name))
        store = self.object_store()
        if getattr(store, "atomic_cas", False):
            caps.add(Cap.STATE_ATOMIC_CAS)
        else:
            caps.discard(Cap.STATE_ATOMIC_CAS)
        return frozenset(caps)

    def has(self, capability: str) -> bool:
        return capability in self.capabilities()

    def require(self, capability: str, feature: str, remedy: str = "") -> None:
        if not self.has(capability):
            raise CapabilityError(capability, feature, f"{self.platform.name}/{self.compute.name}", remedy)

    # -- services ----------------------------------------------------------

    @cached_property
    def _state(self):
        return services.build("state", self.settings.state_url)

    def object_store(self):
        return self._state

    def session_store(self):
        from ..adapters import session_store_for

        return session_store_for(self._state)

    def job_store(self):
        from ..adapters import job_store_for

        if not getattr(self._state, "atomic_cas", False):
            warnings.warn(
                f"State store {self.settings.state_url!r} cannot do atomic compare-and-swap; "
                "concurrent writers (CLI + tick + worker) may lose updates. Use s3://, az://, gs:// "
                "or file:// on a single host.",
                RuntimeWarning,
                stacklevel=2,
            )
        return job_store_for(self._state)

    @cached_property
    def _queue(self):
        return services.build("queue", self.settings.queue_url or "none")

    def queue(self):
        return self._queue

    @cached_property
    def _delayed(self):
        return services.build("delayed", self.settings.delayed_url or "none", queue=self._queue)

    def delayed_delivery(self):
        return self._delayed

    # -- strategies --------------------------------------------------------

    def run_strategy(self) -> str:
        """
        How an HTTP-submitted agent run executes:
          queue   → enqueue; a worker (event job / queue consumer) runs it
          thread  → run on a background thread in this process
        Background threads are only safe where the platform keeps CPU after
        the response; elsewhere a queue is required.
        """
        if self._queue is not None:
            return "queue"
        if self.has(Cap.BACKGROUND_AFTER_RESPONSE):
            return "thread"
        raise CapabilityError(
            Cap.BACKGROUND_AFTER_RESPONSE, "Asynchronous agent runs (POST /runs)",
            f"{self.platform.name}/{self.compute.name}",
            "Set SKYCTL_QUEUE_URL so runs are executed by a worker "
            "(or use an always-allocated CPU profile).",
        )

    def describe(self) -> dict:
        return {
            "platform": self.platform.name,
            "compute": self.compute.name,
            "compute_kind": self.compute.kind,
            "capabilities": sorted(self.capabilities()),
            "limits": {k: v for k, v in vars(self.compute.limits).items() if v not in (None, "")},
            "state": self.settings.state_url,
            "atomic_cas": bool(getattr(self._state, "atomic_cas", False)),
            "queue": self.settings.queue_url or None,
            "delayed": self.settings.delayed_url or None,
            "run_strategy": self._safe(self.run_strategy),
            "identity": self.platform.identity(os.environ),
        }

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except CapabilityError as e:
            return f"unavailable: {e}"


_runtime: Optional[Runtime] = None


def get_runtime(settings=None, refresh: bool = False) -> Runtime:
    """The process-wide Runtime (secrets are loaded first via load_settings)."""
    global _runtime
    if _runtime is None or refresh or (settings is not None and settings is not _runtime.settings):
        from ..settings import load_settings

        _runtime = Runtime(settings or load_settings())
    return _runtime
