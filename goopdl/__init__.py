"""GPlay APK Downloader — download APKs from Google Play Store."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from goopdl import errors as _errors
from goopdl.errors import GoopdlError

__version__ = "1.4.1"

__all__ = [
    "__version__",
    "GoopdlError",
    "PlayAPIError",
    "NetworkError",
    "AuthExpiredError",
    "RateLimitedError",
    "VersionUnavailableError",
    "AppNotSupportedError",
    "AppNotAvailableError",
    "AppNotPurchasedError",
    "InvalidPackageError",
    "DirectAuthError",
    "DirectAuthConfigurationError",
    "AASTokenError",
    "BrowserOAuthError",
    "IntegrityError",
]

if TYPE_CHECKING:
    from goopdl.errors import (
        AASTokenError,
        AppNotAvailableError,
        AppNotPurchasedError,
        AppNotSupportedError,
        AuthExpiredError,
        BrowserOAuthError,
        DirectAuthConfigurationError,
        DirectAuthError,
        IntegrityError,
        InvalidPackageError,
        NetworkError,
        PlayAPIError,
        RateLimitedError,
        VersionUnavailableError,
    )


def __getattr__(name: str) -> Any:
    # Exceptions live next to the code that raises them; resolve lazily so
    # ``import goopdl`` stays free of httpx/rich/websocket imports.
    if name in _errors.__all__:
        return getattr(_errors, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
