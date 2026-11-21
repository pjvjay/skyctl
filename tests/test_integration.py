"""
Contract tests against real emulators or real cloud accounts.

Skipped unless the matching environment variable is set; CI's
`integration` job starts the emulators and sets them. These exist because
in-process doubles prove API shape but not concurrency semantics (moto, for
one, does not evaluate S3 preconditions atomically). Real cloud accounts
are the final authority: point the same variables at them in a sandbox.

    SKYCTL_IT_S3_ENDPOINT      e.g. http://localhost:4566   (LocalStack; unset + AWS creds = real S3)
    SKYCTL_IT_S3_BUCKET        bucket to use (created if missing)
    SKYCTL_IT_AZURE_CONN       Azurite / storage connection string (Blob and Queue endpoints)
    SKYCTL_IT_GCS_ENDPOINT     e.g. http://localhost:4443   (fake-gcs-server)
"""

import os
import uuid

import pytest

from conftest import require
from skyctl.adapters import ObjectJobStore
from skyctl.testing.contracts import (
    check_cas_under_contention,
    check_job_store_lease,
    check_object_store,
    check_work_queue,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def s3_store():
    bucket = os.getenv("SKYCTL_IT_S3_BUCKET")
    if not bucket:
        pytest.skip("SKYCTL_IT_S3_BUCKET not set")
    boto3 = require("boto3")
    from skyctl.adapters.s3 import S3ObjectStore

    client = boto3.client(
        "s3", endpoint_url=os.getenv("SKYCTL_IT_S3_ENDPOINT") or None, region_name=os.getenv("AWS_REGION", "us-east-1")
    )
    try:
        client.create_bucket(Bucket=bucket)
    except Exception:
        pass
    return S3ObjectStore(bucket, prefix=f"it-{uuid.uuid4().hex[:8]}", client=client)


@pytest.fixture
def azure_store():
    conn = os.getenv("SKYCTL_IT_AZURE_CONN")
    if not conn:
        pytest.skip("SKYCTL_IT_AZURE_CONN not set")
    from azure.storage.blob import BlobServiceClient

    from skyctl.adapters.azure_blob import AzureBlobObjectStore

    name = f"it{uuid.uuid4().hex[:10]}"
    cc = BlobServiceClient.from_connection_string(conn).create_container(name)
    return AzureBlobObjectStore(name, prefix="skyctl", container_client=cc)


@pytest.fixture
def gcs_store():
    endpoint = os.getenv("SKYCTL_IT_GCS_ENDPOINT")
    if not endpoint:
        pytest.skip("SKYCTL_IT_GCS_ENDPOINT not set")
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import storage

    from skyctl.adapters.gcs import GcsObjectStore

    client = storage.Client(project="it", credentials=AnonymousCredentials(), client_options={"api_endpoint": endpoint})
    bucket = client.create_bucket(f"it-{uuid.uuid4().hex[:10]}")
    return GcsObjectStore(bucket.name, prefix="skyctl", bucket_client=bucket)


@pytest.mark.parametrize("fixture", ["s3_store", "azure_store", "gcs_store"])
def test_contract(fixture, request):
    store = request.getfixturevalue(fixture)
    check_object_store(store)
    check_job_store_lease(ObjectJobStore(store))


@pytest.mark.parametrize("fixture", ["s3_store", "azure_store", "gcs_store"])
def test_cas_under_contention(fixture, request):
    check_cas_under_contention(request.getfixturevalue(fixture), threads=6, increments=5)


def test_azure_storage_queue_contract():
    """The Azure queue the renderers use, against Azurite's queue service (or a real account)."""
    conn = os.getenv("SKYCTL_IT_AZURE_CONN")
    if not conn:
        pytest.skip("SKYCTL_IT_AZURE_CONN not set")
    from azure.storage.queue import QueueServiceClient

    from skyctl.adapters.queues import AzureStorageQueue

    svc = QueueServiceClient.from_connection_string(conn)
    name = f"it{uuid.uuid4().hex[:10]}"
    main, poison = svc.create_queue(name), svc.create_queue(f"{name}-poison")
    check_work_queue(AzureStorageQueue("devstoreaccount1", name, client=main, poison_client=poison))
