"""Phase 11A tests: BlobStore (Local, S3, Azure), env factory, and two-instance shared-blob Store."""
import shutil
import time
from io import BytesIO

import pytest

from core.data.blobs import (
    AzureBlobStore,
    LocalBlobStore,
    S3BlobStore,
    create_blob_store,
)
from indexer import Store


# ---------------------------------------------------------------- 1. LocalBlobStore
def test_local_blob_store_crud_and_traversal(tmp_path):
    bs = LocalBlobStore(tmp_path / "blobs")
    assert bs.name == "local"
    assert bs.put("docs/a.txt", b"hello", "text/plain") == "docs/a.txt"
    assert bs.exists("docs/a.txt") is True
    assert bs.get("docs/a.txt") == b"hello"
    assert bs.signed_url("docs/a.txt") is None

    bs.put("pages/u1/p001.jpg", b"jpg1")
    bs.put("pages/u1/p002.jpg", b"jpg2")
    assert bs.delete_prefix("pages/u1/") == 2
    assert bs.get("pages/u1/p001.jpg") is None
    assert bs.delete("docs/a.txt") is True
    assert bs.delete("docs/a.txt") is False

    for bad in ("../secret", "/abs/path", "a\\b", "a/../b", ""):
        with pytest.raises(ValueError):
            bs.put(bad, b"x")


# ---------------------------------------------------------------- 2. S3BlobStore (mocked client)
def test_s3_blob_store_with_mock_client():
    objects = {}

    class FakeS3:
        def put_object(self, Bucket, Key, Body, ContentType):
            objects[(Bucket, Key)] = Body

        def get_object(self, Bucket, Key):
            if (Bucket, Key) not in objects:
                raise RuntimeError("NoSuchKey")
            return {"Body": BytesIO(objects[(Bucket, Key)])}

        def head_object(self, Bucket, Key):
            if (Bucket, Key) not in objects:
                raise RuntimeError("404 Not Found")

        def delete_object(self, Bucket, Key):
            objects.pop((Bucket, Key), None)

        def list_objects_v2(self, Bucket, Prefix):
            return {"Contents": [{"Key": k} for (b, k) in objects if b == Bucket and k.startswith(Prefix)]}

        def delete_objects(self, Bucket, Delete):
            for obj in Delete["Objects"]:
                objects.pop((Bucket, obj["Key"]), None)

        def generate_presigned_url(self, op, Params, ExpiresIn):
            return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?exp={ExpiresIn}"

    s3 = S3BlobStore("my-bucket", region="us-east-1", prefix="vr", client=FakeS3())
    assert s3.name == "s3"
    s3.put("docs/x.pdf", b"pdf-bytes", "application/pdf")
    assert s3.exists("docs/x.pdf") is True
    assert s3.get("docs/x.pdf") == b"pdf-bytes"
    assert s3.signed_url("docs/x.pdf", 120) == "https://s3.example/my-bucket/vr/docs/x.pdf?exp=120"
    assert s3.delete_prefix("docs/") == 1
    assert s3.get("docs/x.pdf") is None
    assert s3.delete("docs/x.pdf") is False


# ---------------------------------------------------------------- 3. AzureBlobStore (mocked client)
def test_azure_blob_store_with_mock_client():
    blobs = {}

    class FakeBlobClient:
        def __init__(self, key):
            self.key = key

        def upload_blob(self, data, overwrite=True, content_settings=None):
            blobs[self.key] = data

        def download_blob(self):
            if self.key not in blobs:
                raise RuntimeError("BlobNotFound")
            return type("DL", (), {"readall": lambda s=None: blobs[self.key]})()

        def exists(self):
            return self.key in blobs

        def delete_blob(self):
            blobs.pop(self.key, None)

    class FakeContainerClient:
        def get_blob_client(self, key):
            return FakeBlobClient(key)

        def list_blobs(self, name_starts_with=""):
            return [{"name": k} for k in list(blobs) if k.startswith(name_starts_with)]

        def delete_blob(self, name):
            blobs.pop(name, None)

    class FakeServiceClient:
        def get_container_client(self, container):
            return FakeContainerClient()

    az = AzureBlobStore("my-container", prefix="prod", client=FakeServiceClient())
    assert az.name == "azure"
    az.put("pages/u1/p001.jpg", b"img")
    assert az.exists("pages/u1/p001.jpg") is True
    assert az.get("pages/u1/p001.jpg") == b"img"
    assert az.delete_prefix("pages/u1/") == 1
    assert az.get("pages/u1/p001.jpg") is None


# ---------------------------------------------------------------- 4. Env factory
def test_create_blob_store_env_routing(tmp_path):
    assert create_blob_store(tmp_path, {}).name == "local"
    s3 = create_blob_store(tmp_path, {"VR_RAG_BLOB_BACKEND": "s3", "VR_RAG_S3_BUCKET": "b1", "AWS_REGION": "us-east-1"})
    assert isinstance(s3, S3BlobStore) and s3.bucket == "b1"
    az = create_blob_store(tmp_path, {"VR_RAG_BLOB_BACKEND": "azure", "VR_RAG_AZURE_BLOB_CONTAINER": "c1"})
    assert isinstance(az, AzureBlobStore) and az.container == "c1"
    with pytest.raises(ValueError):
        create_blob_store(tmp_path, {"VR_RAG_BLOB_BACKEND": "ftp"})


# ---------------------------------------------------------------- 5. Two-instance shared-blob Store
def test_two_stores_share_blobs_without_local_disk_overlap(tmp_path):
    shared_blobs = LocalBlobStore(tmp_path / "shared_blobs")
    store_a = Store(tmp_path / "inst_a", blobs=shared_blobs)
    store_b = Store(tmp_path / "inst_b", blobs=shared_blobs)

    entry = store_a.add("sop.txt", b"Shared blob SOP content step one. " * 20, tenant_id="default")
    uid = entry["uid"]
    t0 = time.time()
    while time.time() - t0 < 20:
        cur = store_a.doc_entry(uid)
        if cur and cur["status"] == "ready" and shared_blobs.exists(f"pages/{uid}/p001.jpg"):
            break
        time.sleep(0.05)

    # Store B has zero files in inst_b/files or inst_b/pages/{uid}, yet reads both via shared BlobStore
    assert not (tmp_path / "inst_b" / "pages" / uid).exists()
    assert store_b.read_blob(entry["blob_key"]) is not None
    jpg = store_b.read_page_bytes(uid, 1)
    assert jpg is not None and jpg.startswith(b"\xff\xd8")

    # Delete on Store A removes blobs from shared store
    assert store_a.delete(uid) is True
    assert shared_blobs.get(entry["blob_key"]) is None
    assert shared_blobs.get(f"pages/{uid}/p001.jpg") is None
    shutil.rmtree(tmp_path, ignore_errors=True)
