"""Google Cloud Storage ``ObjectStore``.

Needs ``google-cloud-storage`` (``pip install google-cloud-storage``).

Credentials come from Application Default Credentials -- Workload Identity on GKE, the
service account on Cloud Run, ``gcloud auth application-default login`` locally. The client is
built on the first ``put``, never at construction.
"""

from __future__ import annotations

from typing import Any

from evalshift.stores.uri import require_store_modules


class GCSStore:
    """Put objects under ``gs://<bucket>/<prefix>``."""

    def __init__(self, bucket: str, prefix: str = "", *, client: Any | None = None) -> None:
        if client is None:
            require_store_modules("gs")  # loud here, in user code, not on the first background put
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.uri = f"gs://{bucket}/{self.prefix}".rstrip("/")
        self._client = client
        self._bucket: Any | None = None

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _get_bucket(self) -> Any:
        if self._bucket is None:
            if self._client is None:
                from google.cloud import storage  # lazy: optional, presence checked in __init__

                self._client = storage.Client()
            self._bucket = self._client.bucket(self.bucket)
        return self._bucket

    def put(self, key: str, data: bytes) -> None:
        self._get_bucket().blob(self._key(key)).upload_from_string(
            data, content_type="application/json"
        )


__all__ = ["GCSStore"]
