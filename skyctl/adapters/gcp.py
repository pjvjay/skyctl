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
skyctl.adapters.gcp
===================
Google Secret Manager as a SecretStore. Needs google-cloud-secret-manager
(pip install skyctl[gcp]). Auth is Application Default Credentials: the
Cloud Run service account, or `gcloud auth application-default login`.

    SKYCTL_SECRETS=gcp-sm://<project>/<secret-id>[@<version>]
"""

from __future__ import annotations

from .local import parse_secret_payload


class GcpSecretManagerStore:
    def __init__(self, project: str, secret_id: str, version: str = "latest", client=None):
        self.project = project
        self.secret_id = secret_id
        self.version = version
        self._client = client

    def _sm_client(self):
        if self._client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "google-cloud-secret-manager is not installed. Run: pip install 'skyctl[gcp]'"
                ) from e
            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def load_raw(self) -> str:
        name = f"projects/{self.project}/secrets/{self.secret_id}/versions/{self.version}"
        return self._sm_client().access_secret_version(request={"name": name}).payload.data.decode("utf-8")

    def load(self) -> dict[str, str]:
        return parse_secret_payload(self.load_raw())
