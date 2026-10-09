"""Each provider adapter against a stub client: the call shape, the key prefix, lazy import.

The real client libraries are dev dependencies so the adapter modules import under mypy and
pytest, but no test here touches the network: every client is a stub.
"""

from __future__ import annotations

from typing import Any

import pytest

from evalshift.stores import ObjectStore
from evalshift.stores import uri as uri_module
from evalshift.stores.azure import AzureBlobStore
from evalshift.stores.gcs import GCSStore
from evalshift.stores.s3 import S3Store
from evalshift.stores.uri import (
    MissingStoreDependencyError,
    open_store,
    parse_store_uri,
    require_store_modules,
)

# --- S3 ------------------------------------------------------------------------------------


class FakeS3Client:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_s3_put_shape_with_prefix() -> None:
    client = FakeS3Client()
    store = S3Store("acme-evals", "support-agent/", client=client)
    store.put("captures/s/cap_1.json", b"{}")
    assert client.calls == [
        {
            "Bucket": "acme-evals",
            "Key": "support-agent/captures/s/cap_1.json",
            "Body": b"{}",
            "ContentType": "application/json",
        }
    ]
    assert store.uri == "s3://acme-evals/support-agent"


def test_s3_put_without_prefix() -> None:
    client = FakeS3Client()
    S3Store("acme-evals", client=client).put("toolsets/ab.json", b"[]")
    assert client.calls[0]["Key"] == "toolsets/ab.json"
    assert S3Store("acme-evals").uri == "s3://acme-evals"


def test_s3_satisfies_protocol() -> None:
    assert isinstance(S3Store("b", client=FakeS3Client()), ObjectStore)


# --- GCS -----------------------------------------------------------------------------------


class FakeBlob:
    def __init__(self, name: str, sink: list[tuple[str, bytes, str]]) -> None:
        self.name = name
        self._sink = sink

    def upload_from_string(self, data: bytes, content_type: str) -> None:
        self._sink.append((self.name, data, content_type))


class FakeBucket:
    def __init__(self, name: str, sink: list[tuple[str, bytes, str]]) -> None:
        self.name = name
        self._sink = sink

    def blob(self, key: str) -> FakeBlob:
        return FakeBlob(key, self._sink)


class FakeGCSClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes, str]] = []
        self.buckets: list[str] = []

    def bucket(self, name: str) -> FakeBucket:
        self.buckets.append(name)
        return FakeBucket(name, self.uploads)


def test_gcs_put_shape() -> None:
    client = FakeGCSClient()
    store = GCSStore("acme-evals", "p", client=client)
    store.put("captures/s/cap_1.json", b"{}")
    assert client.buckets == ["acme-evals"]
    assert client.uploads == [("p/captures/s/cap_1.json", b"{}", "application/json")]
    assert store.uri == "gs://acme-evals/p"


# --- Azure ---------------------------------------------------------------------------------


class FakeBlobClient:
    def __init__(self, container: str, key: str, sink: list[tuple[str, str, bytes, bool]]) -> None:
        self._container, self._key, self._sink = container, key, sink

    def upload_blob(self, data: bytes, overwrite: bool) -> None:
        self._sink.append((self._container, self._key, data, overwrite))


class FakeBlobServiceClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes, bool]] = []

    def get_blob_client(self, container: str, blob: str) -> FakeBlobClient:
        return FakeBlobClient(container, blob, self.uploads)


def test_azure_put_shape() -> None:
    client = FakeBlobServiceClient()
    store = AzureBlobStore("acmeprod", "evals", "support-agent", client=client)
    store.put("captures/s/cap_1.json", b"{}")
    assert client.uploads == [("evals", "support-agent/captures/s/cap_1.json", b"{}", True)]
    assert store.uri == "az://acmeprod/evals/support-agent"
    assert store.account_url == "https://acmeprod.blob.core.windows.net"


# --- open_store ----------------------------------------------------------------------------


def test_open_store_dispatches_on_scheme() -> None:
    assert isinstance(open_store("s3://b/p"), S3Store)
    assert isinstance(open_store("gs://b/p"), GCSStore)
    assert isinstance(open_store("az://a/c/p"), AzureBlobStore)


def test_open_store_does_not_construct_a_client() -> None:
    # Lazy by contract: parsing EVALSHIFT_SINK at import must never touch the credential chain.
    store = open_store("s3://b/p")
    assert isinstance(store, S3Store)
    assert store._client is None


def test_open_store_missing_library_names_the_package(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(uri_module, "_installed", lambda module: False)
    with pytest.raises(MissingStoreDependencyError) as info:
        open_store("gs://b/p")
    err = info.value
    assert str(err) == (
        "gs:// store needs google-cloud-storage, which is not installed; "
        "run: pip install google-cloud-storage"
    )
    assert (err.scheme, err.module, err.package, err.packages, err.extra) == (
        "gs",
        "google.cloud.storage",
        "google-cloud-storage",
        "google-cloud-storage",
        "gcs",
    )
    assert "[gcs]" not in str(err)


def test_open_store_azure_names_both_packages_when_identity_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # azure-storage-blob alone is not enough: DefaultAzureCredential lives in azure-identity,
    # and the first put would fail in the background. One pip command installs both.
    monkeypatch.setattr(uri_module, "_installed", lambda module: module != "azure.identity")
    with pytest.raises(MissingStoreDependencyError) as info:
        open_store("az://a/c/p")
    assert info.value.package == "azure-identity"
    assert str(info.value).endswith("run: pip install azure-storage-blob azure-identity")


def test_require_store_modules_is_a_no_op_when_everything_is_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uri_module, "_installed", lambda module: True)
    require_store_modules("az")


def test_store_uri_packages_property() -> None:
    assert parse_store_uri("s3://b/p").packages == "boto3"
    assert parse_store_uri("gs://b/p").packages == "google-cloud-storage"
    assert parse_store_uri("az://a/c/p").packages == "azure-storage-blob azure-identity"


def test_open_store_rejects_bad_grammar() -> None:
    with pytest.raises(ValueError, match="accepted forms"):
        open_store("ftp://b/p")
