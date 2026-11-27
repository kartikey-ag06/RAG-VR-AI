"""Azure Blob Storage BlobStore with lazy azure-storage-blob import (Phase 11A)."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from .base import validate_blob_key, validate_blob_prefix


class AzureBlobStore:
    name = "azure"

    def __init__(
        self,
        container: str,
        connection_string: str = "",
        account_url: str = "",
        prefix: str = "",
        client=None,
    ):
        if not container:
            raise ValueError("VR_RAG_AZURE_BLOB_CONTAINER is required for azure blob backend")
        self.container = container
        self.connection_string = connection_string
        self.account_url = account_url
        self.prefix = prefix.strip("/")
        self._service_client = client

    def _service(self):
        if self._service_client is None:
            from azure.storage.blob import BlobServiceClient  # lazy optional dependency
            if self.connection_string:
                self._service_client = BlobServiceClient.from_connection_string(self.connection_string)
            elif self.account_url:
                from azure.identity import DefaultAzureCredential
                self._service_client = BlobServiceClient(account_url=self.account_url, credential=DefaultAzureCredential())
            else:
                raise ValueError("Azure blob backend needs connection string or account URL")
        return self._service_client

    def _container(self):
        return self._service().get_container_client(self.container)

    def _full_key(self, key: str) -> str:
        k = validate_blob_key(key)
        return f"{self.prefix}/{k}" if self.prefix else k

    def _full_prefix(self, prefix: str) -> str:
        p = validate_blob_prefix(prefix)
        return f"{self.prefix}/{p}" if self.prefix else p

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        k = validate_blob_key(key)
        bc = self._container().get_blob_client(self._full_key(k))
        try:
            from azure.storage.blob import ContentSettings
            bc.upload_blob(data, overwrite=True, content_settings=ContentSettings(content_type=content_type))
        except ImportError:
            bc.upload_blob(data, overwrite=True)
        return k

    def get(self, key: str) -> Optional[bytes]:
        bc = self._container().get_blob_client(self._full_key(key))
        try:
            return bc.download_blob().readall()
        except Exception as e:
            if "BlobNotFound" in str(e) or "404" in str(e):
                return None
            raise

    def exists(self, key: str) -> bool:
        return bool(self._container().get_blob_client(self._full_key(key)).exists())

    def delete(self, key: str) -> bool:
        bc = self._container().get_blob_client(self._full_key(key))
        if not bc.exists():
            return False
        bc.delete_blob()
        return True

    def delete_prefix(self, prefix: str) -> int:
        cc = self._container()
        fp = self._full_prefix(prefix)
        count = 0
        for blob in cc.list_blobs(name_starts_with=fp):
            name = blob["name"] if isinstance(blob, dict) else blob.name
            cc.delete_blob(name)
            count += 1
        return count

    def signed_url(self, key: str, ttl_seconds: int = 300) -> Optional[str]:
        fk = self._full_key(key)
        svc = self._service()
        cred = getattr(svc, "credential", None)
        account_key = getattr(cred, "account_key", None)
        if not account_key:
            return None
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas
        sas = generate_blob_sas(
            account_name=svc.account_name,
            container_name=self.container,
            blob_name=fk,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(seconds=max(1, int(ttl_seconds))),
        )
        return f"{svc.url.rstrip('/')}/{self.container}/{fk}?{sas}"
