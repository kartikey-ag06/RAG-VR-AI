"""S3-compatible BlobStore (AWS S3, Cloudflare R2, MinIO) with lazy boto3 import (Phase 11A)."""
from typing import Optional

from .base import validate_blob_key, validate_blob_prefix


class S3BlobStore:
    name = "s3"

    def __init__(self, bucket: str, region: str = "", endpoint_url: str = "", prefix: str = "", client=None):
        if not bucket:
            raise ValueError("VR_RAG_S3_BUCKET is required for s3 blob backend")
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        self.prefix = prefix.strip("/")
        self._client = client

    def _s3(self):
        if self._client is None:
            import boto3  # lazy optional dependency (Phase 11A)
            kw = {}
            if self.region:
                kw["region_name"] = self.region
            if self.endpoint_url:
                kw["endpoint_url"] = self.endpoint_url
            self._client = boto3.client("s3", **kw)
        return self._client

    def _full_key(self, key: str) -> str:
        k = validate_blob_key(key)
        return f"{self.prefix}/{k}" if self.prefix else k

    def _full_prefix(self, prefix: str) -> str:
        p = validate_blob_prefix(prefix)
        return f"{self.prefix}/{p}" if self.prefix else p

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        k = validate_blob_key(key)
        self._s3().put_object(Bucket=self.bucket, Key=self._full_key(k), Body=data, ContentType=content_type)
        return k

    def get(self, key: str) -> Optional[bytes]:
        try:
            r = self._s3().get_object(Bucket=self.bucket, Key=self._full_key(key))
            body = r["Body"]
            return body.read() if hasattr(body, "read") else bytes(body)
        except Exception as e:
            if "NoSuchKey" in str(e) or "404" in str(e):
                return None
            raise

    def exists(self, key: str) -> bool:
        try:
            self._s3().head_object(Bucket=self.bucket, Key=self._full_key(key))
            return True
        except Exception as e:
            if "404" in str(e) or "NoSuchKey" in str(e) or "Not Found" in str(e):
                return False
            raise

    def delete(self, key: str) -> bool:
        if not self.exists(key):
            return False
        self._s3().delete_object(Bucket=self.bucket, Key=self._full_key(key))
        return True

    def delete_prefix(self, prefix: str) -> int:
        fp = self._full_prefix(prefix)
        resp = self._s3().list_objects_v2(Bucket=self.bucket, Prefix=fp)
        keys = [obj["Key"] for obj in resp.get("Contents") or []]
        if not keys:
            return 0
        self._s3().delete_objects(Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in keys]})
        return len(keys)

    def signed_url(self, key: str, ttl_seconds: int = 300) -> Optional[str]:
        return self._s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._full_key(key)},
            ExpiresIn=max(1, int(ttl_seconds)),
        )
