"""Azure Blob Storage ``ObjectStore``. Extra: ``evalshift-sdk[azure]``.

The account URL is derived as ``https://<account>.blob.core.windows.net``. Credentials come
from ``DefaultAzureCredential`` -- Managed Identity on AKS, Container Apps and App Service,
``az login`` locally. The client is built on the first ``put``, never at construction.
"""

from __future__ import annotations

from typing import Any


class AzureBlobStore:
    """Put blobs under ``az://<account>/<container>/<prefix>``."""

    def __init__(
        self, account: str, container: str, prefix: str = "", *, client: Any | None = None
    ) -> None:
        self.account = account
        self.container = container
        self.prefix = prefix.strip("/")
        self.account_url = f"https://{account}.blob.core.windows.net"
        self.uri = f"az://{account}/{container}/{self.prefix}".rstrip("/")
        self._client = client

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _get_client(self) -> Any:
        if self._client is None:
            from azure.identity import DefaultAzureCredential  # lazy: [azure] extra
            from azure.storage.blob import BlobServiceClient

            self._client = BlobServiceClient(self.account_url, credential=DefaultAzureCredential())
        return self._client

    def put(self, key: str, data: bytes) -> None:
        self._get_client().get_blob_client(self.container, self._key(key)).upload_blob(
            data, overwrite=True
        )


__all__ = ["AzureBlobStore"]
