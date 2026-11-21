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
Port contracts run against every ObjectStore / JobStore implementation.
S3 runs against moto (an in-process AWS emulator); Azure Blob and GCS run
against SDK-faithful fakes (skyctl.testing.fakes).
"""

import pytest

from conftest import require
from skyctl.adapters import FileObjectStore, MemoryObjectStore, ObjectJobStore
from skyctl.adapters.azure_blob import AzureBlobObjectStore
from skyctl.adapters.gcs import GcsObjectStore
from skyctl.testing.contracts import (
    check_cas_under_contention,
    check_job_store_lease,
    check_object_store,
)
from skyctl.testing.fakes import FakeAzureContainer, FakeGcsBucket


@pytest.fixture
def s3_store():
    moto = require("moto")
    boto3 = require("boto3")
    from skyctl.adapters.s3 import S3ObjectStore

    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="skyctl-state")
        yield S3ObjectStore("skyctl-state", prefix="skyctl", client=client)


def atomic_stores(tmp_path):
    return {
        "memory": MemoryObjectStore(),
        "file": FileObjectStore(tmp_path / "file-store"),
        "azure-blob": AzureBlobObjectStore("c", prefix="skyctl", container_client=FakeAzureContainer()),
        "gcs": GcsObjectStore("b", prefix="skyctl", bucket_client=FakeGcsBucket()),
    }


@pytest.mark.parametrize("name", ["memory", "file", "azure-blob", "gcs"])
def test_object_store_contract(name, tmp_path):
    check_object_store(atomic_stores(tmp_path)[name])


def test_s3_object_store_contract(s3_store):
    check_object_store(s3_store)
    # Keys are namespaced under the prefix in the real bucket.
    s3_store.write("x.txt", b"1")
    raw = s3_store.client.list_objects_v2(Bucket="skyctl-state")["Contents"]
    assert "skyctl/x.txt" in [o["Key"] for o in raw]


def test_fsspec_store_contract_is_explicitly_non_atomic():
    require("fsspec")
    from skyctl.adapters.fsspec_store import FsspecObjectStore

    check_object_store(FsspecObjectStore("memory://contract"), atomic=False)


@pytest.mark.parametrize("name", ["memory", "file", "azure-blob", "gcs"])
def test_cas_has_no_lost_updates_under_contention(name, tmp_path):
    check_cas_under_contention(atomic_stores(tmp_path)[name])


def test_s3_conditional_put_on_deleted_key_is_a_lost_race(s3_store):
    from skyctl.ports import PreconditionFailed

    v = s3_store.put("gone.txt", b"1")
    s3_store.delete("gone.txt")
    with pytest.raises(PreconditionFailed):
        s3_store.put("gone.txt", b"2", if_version=v)


@pytest.mark.skipif(
    not __import__("os").getenv("SKYCTL_IT_S3_BUCKET"),
    reason="moto checks S3 preconditions without a lock, so contention needs real S3 "
    "(set SKYCTL_IT_S3_BUCKET; integration tier, see CLOUD_FRAMEWORK.md §9)",
)
def test_s3_cas_has_no_lost_updates_under_contention_real_bucket():
    import os

    from skyctl.adapters.s3 import S3ObjectStore

    check_cas_under_contention(S3ObjectStore(os.environ["SKYCTL_IT_S3_BUCKET"], prefix="skyctl-it"))


@pytest.mark.parametrize("name", ["memory", "file", "azure-blob", "gcs"])
def test_job_store_lease_contract(name, tmp_path):
    check_job_store_lease(ObjectJobStore(atomic_stores(tmp_path)[name]))


def test_s3_job_store_lease_contract(s3_store):
    check_job_store_lease(ObjectJobStore(s3_store))


def test_job_store_update_is_atomic_across_writers(tmp_path):
    """Two schedulers adding jobs concurrently never lose one another's jobs."""
    import threading
    from datetime import datetime, timezone

    from skyctl.models import ScheduledPost

    store = ObjectJobStore(FileObjectStore(tmp_path / "jobs"))
    now = datetime.now(timezone.utc)

    def add(prefix):
        for i in range(15):
            store.update(
                lambda jobs, i=i: jobs.__setitem__(f"{prefix}{i}", ScheduledPost(f"{prefix}{i}", "a", "t", now))
            )

    threads = [threading.Thread(target=add, args=(p,)) for p in "abcd"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(store.load()) == 60
