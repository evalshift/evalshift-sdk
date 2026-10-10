"""Amazon S3 (and S3-compatible) ``ObjectStore``.

Needs ``boto3`` (``pip install boto3``).

Also covers MinIO, Cloudflare R2, Backblaze B2 and Ceph: boto3 honours ``AWS_ENDPOINT_URL``
natively, so no endpoint knob is needed here. Credentials come from boto3's default chain --
on ECS/Fargate the task role, on EC2 the instance profile, locally ``aws sso login`` or the
usual env vars. The client is built on the first ``put``, never at construction, so parsing
``EVALSHIFT_CAPTURE_STORE`` at process start touches neither the network nor the credential chain.
"""

from __future__ import annotations

from typing import Any

from evalshift.stores.uri import require_store_modules


class S3Store:
    """Put objects under ``s3://<bucket>/<prefix>``."""

    def __init__(self, bucket: str, prefix: str = "", *, client: Any | None = None) -> None:
        if client is None:
            require_store_modules("s3")  # loud here, in user code, not on the first background put
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.uri = f"s3://{bucket}/{self.prefix}".rstrip("/")
        self._client = client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _get_client(self) -> Any:
        if self._client is None:
            import boto3  # lazy: optional, presence checked in __init__

            self._client = boto3.client("s3")
        return self._client

    def put(self, key: str, data: bytes) -> None:
        self._get_client().put_object(
            Bucket=self.bucket, Key=self._key(key), Body=data, ContentType="application/json"
        )


__all__ = ["S3Store"]
