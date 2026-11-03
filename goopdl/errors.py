"""Common exception root for goopdl.

Every goopdl exception derives from :class:`GoopdlError`, so library callers
can ``except GoopdlError`` once. Concrete classes stay defined in their own
modules (``goopdl.api``, ``goopdl.auth``...) and are re-exported here lazily,
so importing this module never pulls in httpx, rich or websocket-client.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any


class GoopdlError(Exception):
    """Base class for all errors raised by goopdl."""


_LAZY = {
    "PlayAPIError": "goopdl.api",
    "NetworkError": "goopdl.api",
    "AuthExpiredError": "goopdl.api",
    "RateLimitedError": "goopdl.api",
    "VersionUnavailableError": "goopdl.api",
    "AppNotSupportedError": "goopdl.api",
    "AppNotAvailableError": "goopdl.api",
    "AppNotPurchasedError": "goopdl.api",
    "InvalidPackageError": "goopdl.api",
    "DirectAuthError": "goopdl.auth",
    "DirectAuthConfigurationError": "goopdl.auth",
    "DispenserRateLimitedError": "goopdl.auth",
    "DispenserUnavailableError": "goopdl.auth",
    "InvalidArchError": "goopdl.auth",
    "AASTokenError": "goopdl.aastoken",
    "BrowserOAuthError": "goopdl.browser_oauth",
    "IntegrityError": "goopdl.download",
    "DownloadError": "goopdl.download",
}

__all__ = [
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
    "DispenserRateLimitedError",
    "DispenserUnavailableError",
    "InvalidArchError",
    "AASTokenError",
    "BrowserOAuthError",
    "IntegrityError",
    "DownloadError",
]

if TYPE_CHECKING:
    from goopdl.aastoken import AASTokenError
    from goopdl.api import (
        AppNotAvailableError,
        AppNotPurchasedError,
        AppNotSupportedError,
        AuthExpiredError,
        InvalidPackageError,
        NetworkError,
        PlayAPIError,
        RateLimitedError,
        VersionUnavailableError,
    )
    from goopdl.auth import (
        DirectAuthConfigurationError,
        DirectAuthError,
        DispenserRateLimitedError,
        DispenserUnavailableError,
        InvalidArchError,
    )
    from goopdl.browser_oauth import BrowserOAuthError
    from goopdl.download import DownloadError, IntegrityError


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
