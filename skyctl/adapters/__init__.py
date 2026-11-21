"""
skyctl.adapters
===============
Factories that turn the URL-style settings into adapter instances.
Only the adapter you select is imported, so cloud SDKs stay optional.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from .local import (
    EnvSecretStore,
    FileJobStore,
    FileObjectStore,
    FileSessionStore,
    MemoryObjectStore,
    ObjectJobStore,
    ObjectSessionStore,
    StaticSecretStore,
    parse_secret_payload,
)

__all__ = [
    "EnvSecretStore",
    "FileJobStore",
    "FileObjectStore",
    "FileSessionStore",
    "MemoryObjectStore",
    "ObjectJobStore",
    "ObjectSessionStore",
    "StaticSecretStore",
    "job_store_for",
    "object_store_for",
    "parse_secret_payload",
    "secret_store_for",
    "session_store_for",
]


def secret_store_for(url: str, env_file: str = ".env"):
    """
    env                                   → .env / process env
    aws-sm://<name-or-arn>                → AWS Secrets Manager
    azure-kv://<vault>/<secret>           → Azure Key Vault
    gcp-sm://<project>/<secret>[@version] → Google Secret Manager
    """
    url = (url or "env").strip()
    if url in ("", "env"):
        return EnvSecretStore(env_file)
    scheme, sep, rest = url.partition("://")
    if not sep:
        raise ValueError(f"SKYCTL_SECRETS must be 'env' or a URL, got {url!r}")
    if scheme == "aws-sm":
        from .aws import AwsSecretsManagerStore
        return AwsSecretsManagerStore(rest)
    if scheme == "azure-kv":
        from .azure import AzureKeyVaultStore
        vault, _, secret = rest.partition("/")
        if not vault or not secret:
            raise ValueError("azure-kv URL must be azure-kv://<vault>/<secret>")
        return AzureKeyVaultStore(vault, secret)
    if scheme == "gcp-sm":
        from .gcp import GcpSecretManagerStore
        project, _, secret = rest.partition("/")
        secret, _, version = secret.partition("@")
        if not project or not secret:
            raise ValueError("gcp-sm URL must be gcp-sm://<project>/<secret>[@version]")
        return GcpSecretManagerStore(project, secret, version or "latest")
    raise ValueError(f"Unknown secret store scheme {scheme!r} in {url!r}")


def object_store_for(url: str):
    """
    file:// and memory:// use the stdlib; s3://, az://, gs:// use the native
    stores with real CAS; any other fsspec scheme falls back to
    FsspecObjectStore (atomic_cas=False).
    """
    url = (url or "").strip() or "file://./.skyctl"
    parsed = urlparse(url)
    scheme = parsed.scheme or "file"
    if scheme == "file":
        path = parsed.netloc + parsed.path if parsed.netloc in ("", ".") else parsed.path
        return FileObjectStore(Path(path or ".skyctl"))
    if scheme == "memory":
        return MemoryObjectStore()
    from ..platform import services

    return services.build("state", url)


def session_store_for(store) -> ObjectSessionStore:
    return ObjectSessionStore(store, prefix="sessions/")


def job_store_for(store) -> ObjectJobStore:
    return ObjectJobStore(store, key="queue.json")
