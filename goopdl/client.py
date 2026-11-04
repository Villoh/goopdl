"""Client facade for backends: explicit config, no environment, no global state.

A :class:`Client` applies its :class:`Config` (cache directory, account
credentials, arch, country, proxy, profile, dispenser) to every call through
a context variable, so several clients with different accounts and cache
directories can run in the same process, from threads and from asyncio.

Unlike the CLI and the module-level functions, a Client never reads
GOOPDL_ACCOUNT_EMAIL / GOOPDL_AAS_TOKEN: ``credentials=None`` is anonymous
mode even when they are set. Use ``Credentials.from_env()`` to opt in.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from goopdl import api, auth
from goopdl.api import (
    AppDetails,
    AuthExpiredError,
    DeliveryResult,
    PlayAPIError,
    RateLimitedError,
    _detached,
)
from goopdl.auth import (
    DirectAuthConfigurationError,
    DirectAuthError,
    DispenserRateLimitedError,
    DispenserUnavailableError,
)
from goopdl.download import MAX_CONCURRENT, download_files

if TYPE_CHECKING:
    from goopdl.download import ProgressCallback

__all__ = ["Client", "Config", "Credentials", "DownloadResult"]

T = TypeVar("T")


def _mask_email(email: str) -> str:
    local, at, domain = email.partition("@")
    return f"{local[:1]}***{at}{domain}"


@dataclass(frozen=True)
class Credentials:
    """A Google account for direct authentication (see docs/authentication.md).

    Validated like the GOOPDL_* variables: both non-empty, the token starts
    with ``aas_et/``; otherwise DirectAuthConfigurationError. Values are
    stripped. repr/str hide the token and most of the email.
    """

    email: str = field(repr=False)
    aas_token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.email, str) or not isinstance(self.aas_token, str):
            raise TypeError("Credentials email and aas_token must be str")
        checked = auth._checked_credentials(self.email, self.aas_token)
        if checked is None:
            raise DirectAuthConfigurationError(
                "Missing required environment variable: GOOPDL_ACCOUNT_EMAIL"
            )
        object.__setattr__(self, "email", checked[0])
        object.__setattr__(self, "aas_token", checked[1])

    def __repr__(self) -> str:
        return f"Credentials(email={_mask_email(self.email)}, aas_token=<hidden>)"

    __str__ = __repr__

    @classmethod
    def from_env(cls) -> Credentials | None:
        """Read GOOPDL_ACCOUNT_EMAIL/GOOPDL_AAS_TOKEN like the CLI does.

        None when neither is set; DirectAuthConfigurationError when only one is.
        """
        checked = auth._env_credentials()
        if checked is None:
            return None
        return cls(*checked)


@dataclass(frozen=True)
class Config:
    """Settings a Client applies to every call.

    *config_dir* None resolves the cache directory like the CLI
    (GOOPDL_CONFIG_DIR, XDG_CONFIG_HOME, ~/.config/goopdl) on each call; a
    given one must be absolute after ``~`` expansion. *credentials* None is
    anonymous (dispenser) mode, whatever the environment says.
    """

    config_dir: Path | None = None
    credentials: Credentials | None = None
    arch: str = "arm64"
    country: str | None = None
    proxy: str | None = field(default=None, repr=False)  # may embed a password
    profile: str | None = None
    dispenser_url: str | None = None

    def __post_init__(self) -> None:
        if self.config_dir is not None:
            directory = Path(os.path.expanduser(os.fspath(self.config_dir)))
            if not directory.is_absolute():
                raise ValueError(
                    f"config_dir must be an absolute path, got {str(directory)!r}"
                )
            object.__setattr__(self, "config_dir", directory)
        if self.credentials is not None and not isinstance(self.credentials, Credentials):
            raise TypeError("credentials must be a goopdl.Credentials or None")
        auth._arch_key(self.arch)  # InvalidArchError

    def replace(self, **changes: Any) -> Config:
        """Return a copy with *changes* applied (validated again)."""
        return dataclasses.replace(self, **changes)


@dataclass(frozen=True)
class DownloadResult:
    """*path* is the base APK; *files* every file written, base APK first."""

    path: Path
    version_string: str
    files: tuple[Path, ...]


def _scope(cfg: Config) -> auth._Scope:
    creds = cfg.credentials
    return auth._Scope(
        config_dir=cfg.config_dir,
        credentials=(creds.email, creds.aas_token) if creds else None,
        explicit_credentials=True,
    )


def _run(cfg: Config, fn: Callable[..., T], *args: Any) -> T:
    """Run fn(cfg, *args) inside cfg's scope; failures leave no protocol frames.

    The scope is set here, in whatever thread runs this, never inherited.
    """
    try:
        with auth._use_scope(_scope(cfg)):
            return fn(cfg, *args)
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except BaseException as exc:
        error = _detached(exc)
    finally:
        cfg = fn = args = None  # type: ignore[assignment]
    # Raised outside except: only goopdl causes (already detached) survive.
    raise error from error.__cause__


def _token_error(exc: DirectAuthError) -> PlayAPIError:
    """Translate token failures like download_app does."""
    if isinstance(exc, DispenserRateLimitedError):
        error: PlayAPIError = RateLimitedError(
            "Token dispenser rate limited token requests (HTTP 429).",
            retry_after=exc.retry_after,
        )
        error.__cause__ = exc
        return error
    if isinstance(exc, DispenserUnavailableError):
        error = PlayAPIError(str(exc), status_code=exc.status)
        error.__cause__ = exc
        return error
    if exc.status == 429:
        return RateLimitedError(
            f"Direct Google authentication failed at {exc}.", retry_after=exc.retry_after
        )
    return PlayAPIError(
        f"Direct Google authentication failed at {exc}.", status_code=exc.status
    )


def _with_pool_token(cfg: Config, call: Callable[[dict], T]) -> T:
    """call(token) with a pool token; once more with a fresh one on HTTP 401."""
    common: dict[str, Any] = {
        "arch": cfg.arch, "country": cfg.country, "proxy": cfg.proxy,
        "dispenser_url": cfg.dispenser_url, "profile": cfg.profile,
        "raise_on_error": True,
    }
    error = None
    try:
        token = auth.pick_pool_token(**common)
    except DirectAuthError as exc:
        error = _token_error(exc)
    if error is not None:
        raise error from error.__cause__  # outside except: no __context__
    if not token:
        raise PlayAPIError(
            "Could not obtain an auth token (unknown profile or unusable cache directory)."
        )
    try:
        return call(token)
    except AuthExpiredError:
        pass
    try:
        token = auth.replace_pool_token(token, **common)
    except DirectAuthError as exc:
        error = _token_error(exc)
    if error is not None:
        raise error from error.__cause__
    if not token:
        raise PlayAPIError("Token expired and replacement failed.")
    return call(token)


def _details(cfg: Config, package: str) -> AppDetails:
    api.validate_package(package)
    return _with_pool_token(
        cfg, lambda t: api.get_details(package, t, country=cfg.country, proxy=cfg.proxy)
    )


def _search(cfg: Config, query: str, limit: int) -> list[dict]:
    return _with_pool_token(
        cfg,
        lambda t: api.search_apps(
            query, t, limit=limit, country=cfg.country, proxy=cfg.proxy
        ),
    )


def _list_splits(cfg: Config, package: str) -> list[str]:
    api.validate_package(package)
    return _with_pool_token(
        cfg, lambda t: api.list_splits(package, t, country=cfg.country, proxy=cfg.proxy)
    )


def _item(cfg: Config, package: str) -> dict:
    return api.fetch_app_item(
        package, arch=cfg.arch, country=cfg.country, proxy=cfg.proxy,
        profile=cfg.profile, dispenser_url=cfg.dispenser_url,
    )


def _delivery(cfg: Config, package: str, version_code: int | None) -> DeliveryResult:
    api.validate_package(package)

    def flow(token: dict) -> DeliveryResult:
        vc = version_code
        if vc is None:
            vc = api.get_details(
                package, token, country=cfg.country, proxy=cfg.proxy
            ).version_code
        delivery_token = api.purchase(
            package, vc, token, country=cfg.country, proxy=cfg.proxy
        )
        return api.get_delivery(
            package, vc, token, country=cfg.country, proxy=cfg.proxy,
            delivery_token=delivery_token,
        )

    return _with_pool_token(cfg, flow)


def _prepare(cfg: Config, package: str, output: Path, splits: bool, extras: bool):
    return api._prepare_download(
        package, Path(output), cfg.arch, cfg.country, cfg.proxy,
        cfg.dispenser_url, cfg.profile, not splits, not extras,
    )


def _result(specs: list, version_string: str) -> DownloadResult:
    return DownloadResult(
        path=specs[0].dest,
        version_string=version_string,
        files=tuple(spec.dest for spec in specs),
    )


def _download(
    cfg: Config, package: str, output: Path, splits: bool, extras: bool,
    on_progress: ProgressCallback | None, max_concurrent: int,
) -> DownloadResult:
    specs, version_string = _prepare(cfg, package, output, splits, extras)
    asyncio.run(
        download_files(specs, on_progress=on_progress, max_concurrent=max_concurrent)
    )
    return _result(specs, version_string)


class Client:
    """Synchronous and async goopdl operations bound to one :class:`Config`.

    ``Client(config, **overrides)`` is ``Client(config.replace(**overrides))``.
    Holds no mutable state: share one instance between threads and tasks.
    Per-call keyword overrides (country, proxy, arch, profile, dispenser_url)
    replace the Config value when not None. Failures raise GoopdlError
    subclasses, never return None; token failures are translated like
    api.download_app (dispenser 429 -> RateLimitedError, ``__cause__`` kept).
    """

    __slots__ = ("_config",)

    def __init__(self, config: Config | None = None, **overrides: Any) -> None:
        config = config if config is not None else Config()
        if not isinstance(config, Config):
            raise TypeError("config must be a goopdl.Config or None")
        self._config = config.replace(**overrides) if overrides else config

    @property
    def config(self) -> Config:
        return self._config

    def __repr__(self) -> str:
        return f"Client({self._config!r})"

    def _cfg(self, **overrides: Any) -> Config:
        changes = {k: v for k, v in overrides.items() if v is not None}
        return self._config.replace(**changes) if changes else self._config

    def details(
        self, package: str, *, country: str | None = None, proxy: str | None = None,
        arch: str | None = None, profile: str | None = None,
        dispenser_url: str | None = None,
    ) -> AppDetails:
        """Current listing details (api.get_details with a pool token)."""
        cfg = self._cfg(country=country, proxy=proxy, arch=arch, profile=profile,
                        dispenser_url=dispenser_url)
        return _run(cfg, _details, package)

    def search(
        self, query: str, *, limit: int = 10, country: str | None = None,
        proxy: str | None = None, arch: str | None = None,
        profile: str | None = None, dispenser_url: str | None = None,
    ) -> list[dict]:
        """Search hits: ``{package, title, creator}`` dicts (api.search_apps)."""
        cfg = self._cfg(country=country, proxy=proxy, arch=arch, profile=profile,
                        dispenser_url=dispenser_url)
        return _run(cfg, _search, query, limit)

    def list_splits(
        self, package: str, *, country: str | None = None, proxy: str | None = None,
        arch: str | None = None, profile: str | None = None,
        dispenser_url: str | None = None,
    ) -> list[str]:
        """Split names from the details metadata (api.list_splits)."""
        cfg = self._cfg(country=country, proxy=proxy, arch=arch, profile=profile,
                        dispenser_url=dispenser_url)
        return _run(cfg, _list_splits, package)

    def item(
        self, package: str, *, country: str | None = None, proxy: str | None = None,
        arch: str | None = None, profile: str | None = None,
        dispenser_url: str | None = None,
    ) -> dict:
        """Flat app metadata dict (api.fetch_app_item); ``{}`` without a document."""
        cfg = self._cfg(country=country, proxy=proxy, arch=arch, profile=profile,
                        dispenser_url=dispenser_url)
        return _run(cfg, _item, package)

    def delivery(
        self, package: str, version_code: int | None = None, *,
        country: str | None = None, proxy: str | None = None,
        arch: str | None = None, profile: str | None = None,
        dispenser_url: str | None = None,
    ) -> DeliveryResult:
        """purchase + get_delivery with one token; None = the current version."""
        cfg = self._cfg(country=country, proxy=proxy, arch=arch, profile=profile,
                        dispenser_url=dispenser_url)
        return _run(cfg, _delivery, package, version_code)

    def download(
        self, package: str, output: Path | str, *, splits: bool = False,
        extras: bool = False, on_progress: ProgressCallback | None = None,
        max_concurrent: int = MAX_CONCURRENT, country: str | None = None,
        proxy: str | None = None, arch: str | None = None,
        profile: str | None = None, dispenser_url: str | None = None,
    ) -> DownloadResult:
        """Latest version, like api.download_app, without progress bars on stdout.

        Raises RuntimeError inside a running event loop: await adownload().
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "Client.download() cannot run inside a running event loop; "
                "use 'await client.adownload(...)' instead."
            )
        cfg = self._cfg(country=country, proxy=proxy, arch=arch, profile=profile,
                        dispenser_url=dispenser_url)
        return _run(cfg, _download, package, output, splits, extras, on_progress,
                    max_concurrent)
