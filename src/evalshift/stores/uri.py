"""Store URI grammar, shared verbatim with the CLI (``evalshift_cli.captures.store_uri``).

Three forms are accepted::

    s3://<bucket>/<prefix>
    gs://<bucket>/<prefix>
    az://<account>/<container>/<prefix>

``<prefix>`` is optional and may be empty; a trailing slash is stripped. Credentials never
belong in a URI: any ``@`` or ``?`` is rejected so nobody is tempted to embed a key or a
query parameter, and every adapter uses its provider's default credential chain instead.

Stdlib only (D-deps).
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import urlsplit

from evalshift.stores.base import ObjectStore

Scheme = Literal["s3", "gs", "az"]

#: The three accepted forms, quoted in every grammar error.
STORE_URI_FORMS = (
    "s3://<bucket>/<prefix>, gs://<bucket>/<prefix>, az://<account>/<container>/<prefix>"
)

_EXTRA_FOR_SCHEME: dict[str, str] = {"s3": "s3", "gs": "gcs", "az": "azure"}

#: The pip distribution that provides each client module. Shared verbatim with the CLI.
_PACKAGE_FOR_MODULE: dict[str, str] = {
    "boto3": "boto3",
    "google.cloud.storage": "google-cloud-storage",
    "azure.storage.blob": "azure-storage-blob",
    "azure.identity": "azure-identity",
}

#: The ``pip install`` argument that installs everything a scheme needs, in one command.
#: Named in every message instead of the extras: a user who installed the SDK the normal way
#: has no reason to know what an extra is. Shared verbatim with the CLI.
_PACKAGES_FOR_SCHEME: dict[str, str] = {
    "s3": "boto3",
    "gs": "google-cloud-storage",
    "az": "azure-storage-blob azure-identity",
}


@dataclass(frozen=True)
class StoreURI:
    """A parsed store URI.

    Attributes:
        scheme: ``"s3"``, ``"gs"`` or ``"az"``.
        bucket: The S3 / GCS bucket, or the Azure storage *account*.
        container: The Azure blob container; ``None`` for S3 and GCS.
        prefix: Key prefix under which ``captures/`` and ``toolsets/`` live. May be ``""``.
    """

    scheme: Scheme
    bucket: str
    container: str | None
    prefix: str

    @property
    def extra(self) -> str:
        """The pip extra that installs this scheme's client library."""
        return _EXTRA_FOR_SCHEME[self.scheme]

    @property
    def packages(self) -> str:
        """The ``pip install`` argument to install this scheme's client library."""
        return _PACKAGES_FOR_SCHEME[self.scheme]


def parse_store_uri(uri: str) -> StoreURI:
    """Parse ``uri`` against the grammar above.

    Raises:
        ValueError: for an unknown scheme, a missing bucket or container, or a URI that
            carries credentials (``@``) or query parameters (``?``). The message names the
            accepted forms and never echoes the URI, which may hold a pasted secret.
    """
    text = uri.strip()
    if "@" in text:
        raise ValueError(
            "credentials are not accepted in a store URI; use the provider's credential chain"
        )
    if "?" in text:
        # Never echo the URI: a query string is where SAS tokens and signatures live.
        raise ValueError(
            "query parameters are not accepted in a store URI; use the provider's credential chain"
        )
    parts = urlsplit(text)
    # Never echo the URI in a grammar error either: a pasted connection string (Azure's
    # `...;AccountKey=...`) carries no `@` or `?` and would otherwise land in logs verbatim.
    if not parts.scheme:
        raise ValueError(f"store URI has no scheme; accepted forms: {STORE_URI_FORMS}")
    if parts.scheme not in _EXTRA_FOR_SCHEME:
        raise ValueError(
            f"unsupported store URI scheme {parts.scheme!r}; accepted forms: {STORE_URI_FORMS}"
        )
    if not parts.netloc:
        raise ValueError(f"store URI names no bucket; accepted forms: {STORE_URI_FORMS}")
    scheme = cast(Scheme, parts.scheme)
    path = parts.path.strip("/")
    if scheme == "az":
        container, _, prefix = path.partition("/")
        if not container:
            raise ValueError(
                "Azure store URI names no container; expected az://<account>/<container>/<prefix>"
            )
        return StoreURI(scheme, parts.netloc, container, prefix.strip("/"))
    return StoreURI(scheme, parts.netloc, None, path)


#: Every module a scheme's adapter imports on its first put. Azure needs two: the blob client and
#: ``azure.identity`` for ``DefaultAzureCredential`` -- both installed by one pip command.
_REQUIRED_MODULES: dict[str, tuple[str, ...]] = {
    "s3": ("boto3",),
    "gs": ("google.cloud.storage",),
    "az": ("azure.storage.blob", "azure.identity"),
}


class MissingStoreDependencyError(ImportError):
    """The client library for a store scheme is not installed.

    Attributes:
        scheme: ``"s3"``, ``"gs"`` or ``"az"``.
        module: The first module that could not be found.
        package: The pip distribution that provides ``module``.
        packages: The ``pip install`` argument that installs everything the scheme needs.
        extra: The pip extra that pins the same packages, for callers that prefer it.

    The message names ``package`` and ``packages`` only -- never an extra, never a URI.
    """

    def __init__(self, scheme: Scheme, module: str) -> None:
        self.scheme = scheme
        self.module = module
        self.package = _PACKAGE_FOR_MODULE[module]
        self.packages = _PACKAGES_FOR_SCHEME[scheme]
        self.extra = _EXTRA_FOR_SCHEME[scheme]
        super().__init__(
            f"{scheme}:// store needs {self.package}, which is not installed; "
            f"run: pip install {self.packages}"
        )


def _installed(module: str) -> bool:
    """Whether ``module`` can be imported, without importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:  # a parent package is missing (e.g. no `google` at all)
        return False


def require_store_modules(scheme: Scheme) -> None:
    """Raise :class:`MissingStoreDependencyError` if any needed module is not importable.

    Uses ``importlib.util.find_spec`` only; safe at process start and store construction.
    """
    for module in _REQUIRED_MODULES[scheme]:
        if not _installed(module):
            raise MissingStoreDependencyError(scheme, module)


def open_store(uri: str) -> ObjectStore:
    """Parse ``uri`` and return the matching store, without building its client yet.

    Raises:
        ValueError: when ``uri`` does not match the grammar (see :func:`parse_store_uri`).
        MissingStoreDependencyError: when a needed module is not installed; it names the
            first missing one and the package to install.
    """
    parsed = parse_store_uri(uri)
    require_store_modules(parsed.scheme)
    if parsed.scheme == "s3":
        from evalshift.stores.s3 import S3Store

        return S3Store(parsed.bucket, parsed.prefix)
    if parsed.scheme == "gs":
        from evalshift.stores.gcs import GCSStore

        return GCSStore(parsed.bucket, parsed.prefix)
    from evalshift.stores.azure import AzureBlobStore

    return AzureBlobStore(parsed.bucket, parsed.container or "", parsed.prefix)


__all__ = [
    "STORE_URI_FORMS",
    "MissingStoreDependencyError",
    "Scheme",
    "StoreURI",
    "open_store",
    "parse_store_uri",
    "require_store_modules",
]
