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
skyctl.adapters.aws
===================
AWS Secrets Manager as a SecretStore. Needs boto3 (pip install skyctl[aws]).
Credentials come from the default chain: ECS task role, Lambda role,
instance profile, or local AWS profile. No keys in skyctl config.

    SKYCTL_SECRETS=aws-sm://skyctl/prod          (secret name or ARN)
"""

from __future__ import annotations

import os

from .local import parse_secret_payload


class AwsSecretsManagerStore:
    def __init__(self, secret_id: str, region: str | None = None, client=None):
        self.secret_id = secret_id
        self.region = region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        self._client = client

    def _boto_client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("boto3 is not installed. Run: pip install 'skyctl[aws]'") from e
            self._client = boto3.client("secretsmanager", region_name=self.region)
        return self._client

    def load_raw(self) -> str:
        resp = self._boto_client().get_secret_value(SecretId=self.secret_id)
        text = resp.get("SecretString")
        return text if text is not None else resp["SecretBinary"].decode("utf-8")

    def load(self) -> dict[str, str]:
        return parse_secret_payload(self.load_raw())
