"""
skyctl.platform — the cross-cloud framework (see docs/CLOUD_FRAMEWORK.md).

    Platform      where code runs: detection, compute profiles, caps, limits, logging, identity
    services      URL scheme → adapter, per service kind (state, secrets, queue, delayed)
    Runtime       the resolved combination for this process, plus strategy selection
    invocation    how each platform *calls* the code, normalised to one Invocation
"""

from .base import Platform, TraceContext, Toolset, detect_platform, get_platform, platforms, register_platform
from .capabilities import Cap, CapabilityError, ComputeProfile, Limits
from .runtime import Runtime, get_runtime
from .services import build as build_service, register_scheme

__all__ = [
    "Cap",
    "CapabilityError",
    "ComputeProfile",
    "Limits",
    "Platform",
    "Runtime",
    "Toolset",
    "TraceContext",
    "build_service",
    "detect_platform",
    "get_platform",
    "get_runtime",
    "platforms",
    "register_platform",
    "register_scheme",
]
