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

    def load(self) -> dict[str, str]:
        name = f"projects/{self.project}/secrets/{self.secret_id}/versions/{self.version}"
        resp = self._sm_client().access_secret_version(request={"name": name})
        return parse_secret_payload(resp.payload.data.decode("utf-8"))
