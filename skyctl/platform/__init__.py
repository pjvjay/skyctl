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
skyctl.platform — the cross-cloud framework (see docs/CLOUD_FRAMEWORK.md).

    Platform      where code runs: detection, compute profiles, caps, limits, logging, identity
    services      URL scheme → adapter, per service kind (state, secrets, queue, delayed)
    Runtime       the resolved combination for this process, plus strategy selection
    invocation    how each platform *calls* the code, normalised to one Invocation
"""

from .base import Platform, Toolset, TraceContext, detect_platform, get_platform, platforms, register_platform
from .capabilities import Cap, CapabilityError, ComputeProfile, Limits
from .runtime import Runtime, get_runtime
from .services import build as build_service
from .services import register_scheme

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
