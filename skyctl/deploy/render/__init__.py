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

"""Renderers: DeploySpec → target artifacts. One function per target."""

from __future__ import annotations

from ..spec import DeploySpec
from .aws import render_aws
from .azure import render_azure
from .base import Artifact, Finding
from .compose import render_compose
from .gcp import render_gcp
from .kubernetes import render_kubernetes

RENDERERS = {
    "aws": render_aws,
    "azure": render_azure,
    "gcp": render_gcp,
    "kubernetes": render_kubernetes,
    "compose": render_compose,
}


def render(spec: DeploySpec, target: str) -> Artifact:
    if target not in RENDERERS:
        raise ValueError(f"Unknown target {target!r}. Known: {', '.join(RENDERERS)}")
    if getattr(spec.targets, target) is None:
        raise ValueError(f"Target {target!r} is not configured in [targets.{target}]")
    return RENDERERS[target](spec)


__all__ = ["RENDERERS", "Artifact", "Finding", "render"]
