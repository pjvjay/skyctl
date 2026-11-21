"""
skyctl.adapters.azure
=====================
Azure Key Vault as a SecretStore. Needs azure-identity and
azure-keyvault-secrets (pip install skyctl[azure]). Auth is
DefaultAzureCredential: managed identity on Container Apps, `az login`
locally.

    SKYCTL_SECRETS=azure-kv://<vault-name>/<secret-name>
"""

from __future__ import annotations

from .local import parse_secret_payload


class AzureKeyVaultStore:
    def __init__(self, vault: str, secret_name: str, client=None):
        self.vault = vault
        self.secret_name = secret_name
        self._client = client

    def _kv_client(self):
        if self._client is None:
            try:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "Azure SDK is not installed. Run: pip install 'skyctl[azure]'"
                ) from e
            url = self.vault if self.vault.startswith("https://") else f"https://{self.vault}.vault.azure.net"
            self._client = SecretClient(vault_url=url, credential=DefaultAzureCredential())
        return self._client

    def load_raw(self) -> str:
        return self._kv_client().get_secret(self.secret_name).value or ""

    def load(self) -> dict[str, str]:
        return parse_secret_payload(self.load_raw())
