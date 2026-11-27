"""BlobStore factory and exports (Phase 11A)."""
import os
from pathlib import Path
from typing import Mapping, Optional

from .azure import AzureBlobStore
from .base import BlobStore, validate_blob_key, validate_blob_prefix
from .local import LocalBlobStore
from .s3 import S3BlobStore


def create_blob_store(data_dir: Path, env: Optional[Mapping[str, str]] = None) -> BlobStore:
    env = os.environ if env is None else env
    backend = (env.get("VR_RAG_BLOB_BACKEND") or "local").strip().lower()
    if backend == "local":
        return LocalBlobStore(Path(data_dir) / "blobs")
    if backend == "s3":
        return S3BlobStore(
            bucket=(env.get("VR_RAG_S3_BUCKET") or "").strip(),
            region=(env.get("VR_RAG_S3_REGION") or env.get("AWS_REGION") or "").strip(),
            endpoint_url=(env.get("VR_RAG_S3_ENDPOINT") or "").strip(),
            prefix=(env.get("VR_RAG_S3_PREFIX") or "").strip(),
        )
    if backend == "azure":
        return AzureBlobStore(
            container=(env.get("VR_RAG_AZURE_BLOB_CONTAINER") or "").strip(),
            connection_string=(env.get("VR_RAG_AZURE_STORAGE_CONNECTION_STRING") or "").strip(),
            account_url=(env.get("VR_RAG_AZURE_BLOB_ACCOUNT_URL") or "").strip(),
            prefix=(env.get("VR_RAG_AZURE_BLOB_PREFIX") or "").strip(),
        )
    raise ValueError(f"unknown VR_RAG_BLOB_BACKEND {backend!r}")


__all__ = [
    "BlobStore",
    "LocalBlobStore",
    "S3BlobStore",
    "AzureBlobStore",
    "validate_blob_key",
    "validate_blob_prefix",
    "create_blob_store",
]
