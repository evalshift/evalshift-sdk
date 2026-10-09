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


def parse_store_uri(uri: str) -> StoreURI:
    """Parse ``uri`` against the grammar above.

    Raises:
        ValueError: for an unknown scheme, a missing bucket or container, or a URI that
            carries credentials (``@``) or query parameters (``?``). The message names the
            accepted forms.
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
    if parts.scheme not in _EXTRA_FOR_SCHEME:
        raise ValueError(f"unsupported store URI {text!r}; accepted forms: {STORE_URI_FORMS}")
    if not parts.netloc:
        raise ValueError(f"store URI {text!r} names no bucket; accepted forms: {STORE_URI_FORMS}")
    scheme = cast(Scheme, parts.scheme)
    path = parts.path.strip("/")
    if scheme == "az":
        container, _, prefix = path.partition("/")
        if not container:
            raise ValueError(
                f"Azure store URI {text!r} names no container; "
                "expected az://<account>/<container>/<prefix>"
            )
        return StoreURI(scheme, parts.netloc, container, prefix.strip("/"))
    return StoreURI(scheme, parts.netloc, None, path)


#: Every module a scheme's adapter imports on its first put. Azure needs two: the blob client and
#: ``azure.identity`` for ``DefaultAzureCredential`` -- both ship in the ``[azure]`` extra.
_REQUIRED_MODULES: dict[str, tuple[str, ...]] = {
    "s3": ("boto3",),
    "gs": ("google.cloud.storage",),
    "az": ("azure.storage.blob", "azure.identity"),
}


class MissingExtraError(ImportError):
    """The client library for a store URI's scheme is not installed."""

    def __init__(self, parsed: StoreURI, module: str) -> None:
        self.extra = parsed.extra
        super().__init__(
            f"{parsed.scheme}:// store needs the optional dependency {module!r}; "
            f'install it with: pip install "evalshift-sdk[{parsed.extra}]"'
        )


def _installed(module: str) -> bool:
    """Whether ``module`` can be imported, without importing it."""
    try:
        return importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:  # a parent package is missing (e.g. no `google` at all)
        return False


def open_store(uri: str) -> ObjectStore:
    """Parse ``uri`` and return the matching store, without building its client yet.

    Raises:
        ValueError: when ``uri`` does not match the grammar (see :func:`parse_store_uri`).
        MissingExtraError: when a module the scheme's adapter needs is not installed; it names
            the first missing one.
    """
    parsed = parse_store_uri(uri)
    for module in _REQUIRED_MODULES[parsed.scheme]:
        if not _installed(module):
            raise MissingExtraError(parsed, module)
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
    "MissingExtraError",
    "Scheme",
    "StoreURI",
    "open_store",
    "parse_store_uri",
]
