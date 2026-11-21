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
skyctl.deploy — the provisioning plane (docs/CLOUD_FRAMEWORK.md §7).

    spec         skyctl.deploy.toml → DeploySpec (cloud-neutral intent)
    validate     capability- and limit-aware checks per target
    render       DeploySpec → Terraform JSON (aws, azure, gcp), manifests (kubernetes), compose
    conformance  cross-target invariants over the rendered artifacts
"""

from .conformance import Check, check
from .render import RENDERERS, Artifact, render
from .spec import DeploySpec, load_spec
from .validate import validate, validate_all

__all__ = ["RENDERERS", "Artifact", "Check", "DeploySpec", "check", "load_spec", "render", "validate", "validate_all"]
