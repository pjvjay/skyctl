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
from typing import Optional

from .local import parse_secret_payload


class AwsSecretsManagerStore:
    def __init__(self, secret_id: str, region: Optional[str] = None, client=None):
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

    def load(self) -> dict[str, str]:
        resp = self._boto_client().get_secret_value(SecretId=self.secret_id)
        text = resp.get("SecretString")
        if text is None:
            text = resp["SecretBinary"].decode("utf-8")
        return parse_secret_payload(text)
