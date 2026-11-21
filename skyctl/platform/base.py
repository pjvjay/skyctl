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
skyctl.platform.base
====================
The Platform plugin interface and its registry.

A Platform describes *where the code runs*: how to detect it, the compute
profiles it offers (with capabilities and limits), how logs must be shaped
for its logging backend, how trace context arrives, and its toolset. It
does NOT decide which storage/queue/secret services are used: those are
chosen by URL scheme (skyctl.platform.services), because compute and
services are independent axes (Cloud Run may use S3; an EKS pod is
"kubernetes" for compute but "aws" for services).

Third-party platforms register through the `skyctl.platforms` entry point:

    [project.entry-points."skyctl.platforms"]
    oci = "skyctl_oci:OciPlatform"
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from .capabilities import ComputeProfile


@dataclass(frozen=True)
class Toolset:
    """The provider-side tooling a platform relies on (docs, `skyctl platform info`)."""

    python_packages: tuple[str, ...] = ()
    credential_chain: str = ""
    iac: tuple[str, ...] = ()
    cli: str = ""
    emulators: tuple[str, ...] = ()
    ci_auth: str = ""
    observability: tuple[str, ...] = ()
    registry: str = ""
    extra: str = ""


@dataclass(frozen=True)
class TraceContext:
    trace_id: str | None = None
    span_id: str | None = None
    sampled: bool | None = None


class Platform:
    """Base class. Subclasses set the class attributes and override hooks."""

    name: str = "base"
    display_name: str = "Base"
    compute_profiles: Mapping[str, ComputeProfile] = {}
    default_compute: str = ""
    caps: frozenset = frozenset()  # platform-wide capabilities
    toolset: Toolset = Toolset()
    service_schemes: tuple[str, ...] = ()  # URL schemes native to this provider (for docs/defaults)

    # -- detection -------------------------------------------------------

    def detect(self, env: Mapping[str, str]) -> str | None:
        """Return the compute profile name if running here, else None."""
        return None

    # -- compute ---------------------------------------------------------

    def profile(self, name: str | None = None) -> ComputeProfile:
        name = name or self.default_compute
        try:
            return self.compute_profiles[name]
        except KeyError:
            raise ValueError(
                f"Platform {self.name!r} has no compute profile {name!r}. Available: {', '.join(self.compute_profiles)}"
            ) from None

    def capabilities(self, compute: str | None = None) -> frozenset:
        return self.caps | self.profile(compute).caps

    # -- observability -----------------------------------------------------

    def trace_from_headers(self, headers: Mapping[str, str]) -> TraceContext:
        """W3C traceparent by default; platforms add their native header."""
        tp = _header(headers, "traceparent")
        if tp:
            parts = tp.split("-")
            if len(parts) >= 4:
                return TraceContext(parts[1], parts[2], parts[3].endswith("1"))
        return TraceContext()

    def log_fields(self, level: str, trace: TraceContext) -> dict:
        """Extra top-level fields for a JSON log line on this platform."""
        fields = {"level": level}
        if trace.trace_id:
            fields["trace_id"] = trace.trace_id
        if trace.span_id:
            fields["span_id"] = trace.span_id
        return fields

    # -- identity ----------------------------------------------------------

    def identity(self, env: Mapping[str, str]) -> dict:
        """A description of the workload identity in use (never secrets)."""
        return {}

    def __repr__(self) -> str:
        return f"<Platform {self.name}>"


def _header(headers: Mapping[str, str], name: str) -> str:
    for k, v in headers.items():
        if k.lower() == name:
            return v
    return ""


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------

_PLATFORMS: dict[str, Platform] = {}
_ENTRY_POINTS_LOADED = False


def register_platform(platform: Platform) -> Platform:
    _PLATFORMS[platform.name] = platform
    return platform


def _load_entry_points() -> None:
    global _ENTRY_POINTS_LOADED
    if _ENTRY_POINTS_LOADED:
        return
    _ENTRY_POINTS_LOADED = True
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group="skyctl.platforms"):
            try:
                obj = ep.load()
                register_platform(obj() if isinstance(obj, type) else obj)
            except Exception as e:  # a broken plugin must not break the core
                print(f"[skyctl] Warning: platform plugin {ep.name!r} failed to load: {e}")
    except Exception:
        pass


def platforms() -> dict[str, Platform]:
    from . import builtin  # noqa: F401  (registers the built-ins)

    _load_entry_points()
    return dict(_PLATFORMS)


def get_platform(name: str) -> Platform:
    available = platforms()
    aliases = {"cloud-run": "gcp", "google": "gcp", "k8s": "kubernetes", "docker": "compose"}
    key = aliases.get(name.lower(), name.lower())
    if key not in available:
        raise ValueError(f"Unknown platform {name!r}. Available: {', '.join(sorted(available))}")
    return available[key]


def detect_platform(env: Mapping[str, str] | None = None) -> tuple[Platform, str]:
    """
    (platform, compute profile) for this process. SKYCTL_PLATFORM (or the
    older SKYCTL_CLOUD) and SKYCTL_COMPUTE override detection.
    """
    env = os.environ if env is None else env
    explicit = (env.get("SKYCTL_PLATFORM") or env.get("SKYCTL_CLOUD") or "").strip()
    compute = (env.get("SKYCTL_COMPUTE") or "").strip() or None
    if explicit:
        platform = get_platform(explicit)
        return platform, compute or platform.detect(env) or platform.default_compute
    # Most specific first: a managed cloud runtime beats generic Kubernetes.
    order = ["aws", "azure", "gcp", "kubernetes"]
    found = platforms()
    for name in order + [n for n in found if n not in order and n not in ("local", "compose")]:
        if name in found:
            profile = found[name].detect(env)
            if profile:
                return found[name], compute or profile
    local = found["local"]
    return local, compute or local.default_compute
