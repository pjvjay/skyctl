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
skyctl.adapters.azure_blob
==========================
Native Azure Blob Storage ObjectStore with real compare-and-swap.

    version token   ETag
    if_absent       upload_blob(overwrite=False)            → ResourceExistsError on a race
    if_version      upload_blob(etag=…, match_condition=IfNotModified)
                                                            → ResourceModifiedError on a race

Credentials: DefaultAzureCredential (managed identity on Container Apps /
AKS Workload Identity; AZURE_CLIENT_ID selects a user-assigned identity).
Needs `skyctl[azure]`.

    SKYCTL_STATE_URL=az://container/prefix     + AZURE_STORAGE_ACCOUNT_NAME
"""

from __future__ import annotations

import os
from typing import Any

from ..ports import PreconditionFailed, Versioned


class AzureBlobObjectStore:
    atomic_cas = True

    def __init__(self, container: str, prefix: str = "", account: str | None = None, container_client=None):
        self.container = container
        self.prefix = prefix.strip("/")
        self.account = account or os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "").strip()
        self._cc = container_client

    @property
    def cc(self):
        if self._cc is None:
            try:
                from azure.identity import DefaultAzureCredential
                from azure.storage.blob import ContainerClient
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("Azure SDK is not installed. Run: pip install 'skyctl[azure]'") from e
            if not self.account:
                raise RuntimeError("AZURE_STORAGE_ACCOUNT_NAME is not set (needed for az:// state).")
            self._cc = ContainerClient(
                f"https://{self.account}.blob.core.windows.net", self.container, credential=DefaultAzureCredential()
            )
        return self._cc

    def _k(self, key: str) -> str:
        key = key.strip("/")
        if ".." in key.split("/"):
            raise ValueError(f"Key escapes the store root: {key}")
        return f"{self.prefix}/{key}" if self.prefix else key

    def get(self, key: str) -> Versioned | None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            downloader = self.cc.get_blob_client(self._k(key)).download_blob()
        except ResourceNotFoundError:
            return None
        data = downloader.readall()
        return Versioned(data, downloader.properties.etag)

    def put(self, key: str, data: bytes, *, if_version: str | None = None, if_absent: bool = False) -> str:
        from azure.core import MatchConditions
        from azure.core.exceptions import ResourceExistsError, ResourceModifiedError

        blob = self.cc.get_blob_client(self._k(key))
        kwargs: dict[str, Any] = {"overwrite": not if_absent}
        if if_version is not None:
            kwargs.update(etag=if_version, match_condition=MatchConditions.IfNotModified)
        try:
            return blob.upload_blob(data, **kwargs)["etag"]
        except (ResourceExistsError, ResourceModifiedError) as e:
            raise PreconditionFailed(key) from e

    def read(self, key: str) -> bytes:
        found = self.get(key)
        if found is None:
            raise FileNotFoundError(key)
        return found.data

    def write(self, key: str, data: bytes) -> None:
        self.put(key, data)

    def exists(self, key: str) -> bool:
        return self.cc.get_blob_client(self._k(key)).exists()

    def delete(self, key: str) -> None:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            self.cc.get_blob_client(self._k(key)).delete_blob()
        except ResourceNotFoundError:
            pass

    def list(self, prefix: str = "") -> list[str]:
        full = self._k(prefix) if prefix else (f"{self.prefix}/" if self.prefix else "")
        strip = len(self.prefix) + 1 if self.prefix else 0
        return sorted(b.name[strip:] for b in self.cc.list_blobs(name_starts_with=full))

    def append_line(self, key: str, line: str) -> None:
        from .local import append_line_cas

        append_line_cas(self, key, line)
