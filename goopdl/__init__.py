"""GPlay APK Downloader — download APKs from Google Play Store."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from goopdl import errors as _errors
from goopdl.errors import GoopdlError

__version__ = "1.4.1"

# Library default: stay silent unless the host application configures logging.
logging.getLogger(__name__).addHandler(logging.NullHandler())

_CLIENT_NAMES = ("Client", "Config", "Credentials", "DownloadResult")

__all__ = [
    "__version__",
    "Client",
    "Config",
    "Credentials",
    "DownloadResult",
    "GoopdlError",
    "PlayAPIError",
    "NetworkError",
    "AuthExpiredError",
    "RateLimitedError",
    "VersionUnavailableError",
    "AppNotSupportedError",
    "AppNotAvailableError",
    "AppNotPurchasedError",
    "MalformedResponseError",
    "InvalidPackageError",
    "DirectAuthError",
    "DirectAuthConfigurationError",
    "DispenserRateLimitedError",
    "DispenserUnavailableError",
    "PoolBusyError",
    "InvalidArchError",
    "AASTokenError",
    "BrowserOAuthError",
    "IntegrityError",
    "DownloadError",
]

if TYPE_CHECKING:
    from goopdl.client import Client, Config, Credentials, DownloadResult
    from goopdl.errors import (
        AASTokenError,
        AppNotAvailableError,
        AppNotPurchasedError,
        AppNotSupportedError,
        AuthExpiredError,
        BrowserOAuthError,
        DirectAuthConfigurationError,
        DirectAuthError,
        DispenserRateLimitedError,
        DispenserUnavailableError,
        DownloadError,
        IntegrityError,
        InvalidArchError,
        InvalidPackageError,
        MalformedResponseError,
        NetworkError,
        PlayAPIError,
        PoolBusyError,
        RateLimitedError,
        VersionUnavailableError,
    )


def __getattr__(name: str) -> Any:
    # Exceptions live next to the code that raises them; resolve lazily so
    # ``import goopdl`` stays free of httpx/rich/websocket imports.
    if name in _errors.__all__:
        return getattr(_errors, name)
    if name in _CLIENT_NAMES:
        from goopdl import client

        return getattr(client, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    # Advertise the lazily loaded exceptions too (tab completion, dir()).
    return sorted(set(globals()) | set(__all__))
