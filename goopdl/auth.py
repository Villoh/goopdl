"""Token dispenser authentication and Google Play header construction.

Tokens are cached as 0600 JSON files (auth-*, token-pool-*, pool-index-*)
in a 0700 directory resolved by :func:`get_config_dir`: ``$GOOPDL_CONFIG_DIR``,
else ``$XDG_CONFIG_HOME/goopdl``, else ``~/.config/goopdl``. Account mode
stores only its device registration there (direct-device-*, no email, AAS
token or bearer); its bearer stays in process memory.

Note: older releases always used ``~/.config/goopdl``. If XDG_CONFIG_HOME
points elsewhere, existing cached tokens are not found there and are minted
again; move the files or set GOOPDL_CONFIG_DIR=~/.config/goopdl to keep them.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import math
import os
import random
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlparse, urlunparse

import httpx

from goopdl import _http
from goopdl.errors import GoopdlError
from goopdl.profiles import (
    FALLBACK_PROFILE,
    VALID_ARCHS,
    find_profile,
    get_priority_profiles,
    patch_profile_country,
)
from goopdl.protobuf import WIRETYPE_LENGTH_DELIMITED, ProtoDecoder

DEFAULT_DISPENSER_URL = "https://auroraoss.com/api/auth"

DISPENSER_PROXY_REGIONS = [
    "US",
    "CA",
    "MX",
    "BR",
    "AR",
    "CO",
    "CL",
    "GB",
    "DE",
    "FR",
    "IT",
    "ES",
    "NL",
    "PL",
    "SE",
    "TR",
    "RU",
    "JP",
    "IN",
    "CN",
    "KR",
    "SG",
    "AU",
    "ID",
    "TH",
    "VN",
    "PH",
    "AE",
    "SA",
    "IL",
    "ZA",
    "NG",
    "EG",
    "KE",
]

@dataclass(frozen=True)
class _Scope:
    """Per-context overrides set by goopdl.client.Client (see _use_scope)."""

    config_dir: Optional[Path] = None
    credentials: Optional[tuple[str, str]] = field(default=None, repr=False)
    # True: *credentials* (None = anonymous) replace the GOOPDL_* variables.
    explicit_credentials: bool = False


# A ContextVar, not a global: each thread and asyncio task sees its own scope.
_scope: ContextVar[Optional[_Scope]] = ContextVar("goopdl_scope", default=None)


@contextmanager
def _use_scope(scope: _Scope) -> Iterator[None]:
    """Apply *scope* to get_config_dir/_direct_credentials in this context."""
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def get_config_dir() -> Path:
    """Resolve the credential cache directory on use, never at import time.

    Order: a Client's config_dir, GOOPDL_CONFIG_DIR,
    $XDG_CONFIG_HOME/goopdl, ~/.config/goopdl.
    GOOPDL_CONFIG_DIR is ``~``-expanded; like XDG_CONFIG_HOME, a relative
    value is ignored (with a warning) so it never lands in the CWD.
    """
    scope = _scope.get()
    if scope is not None and scope.config_dir is not None:
        return scope.config_dir
    # Tests and embedders may still set or patch auth._CONFIG_DIR directly.
    override = globals().get("_CONFIG_DIR")
    if override is not None:
        return Path(override)
    env = os.path.expanduser(os.environ.get("GOOPDL_CONFIG_DIR", ""))
    if os.path.isabs(env):
        return Path(env)
    if env:
        logger.warning("Ignoring relative GOOPDL_CONFIG_DIR=%r", env)
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if os.path.isabs(xdg):  # The XDG spec says to ignore relative paths.
        return Path(xdg) / "goopdl"
    return Path.home() / ".config" / "goopdl"


# Library code only logs; the CLI attaches a handler. Never log secrets here.
logger = logging.getLogger(__name__)


def _sanitize_country(country: str) -> str:
    """Strip everything except uppercase A-Z and digits — prevents path traversal."""
    return re.sub(r"[^A-Z0-9]", "", country.upper())[:4]


class InvalidArchError(GoopdlError, ValueError):
    """Raised when an architecture is not one of profiles.VALID_ARCHS."""


def _arch_key(arch: str) -> str:
    """Return *arch* if it is a known architecture; it is used in cache paths."""
    if arch not in VALID_ARCHS:
        raise InvalidArchError(f"Invalid architecture: {arch!r}")
    return arch


def _profile_suffix(arch: str, profile: Optional[str]) -> str:
    """Use the canonical profile key for aliases; reject unsafe filename keys."""
    if not profile:
        return ""
    match = find_profile(profile, arch)
    key = match[0] if match else profile
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ValueError("Invalid profile cache key")
    # Unknown names must not alias a valid key on case-insensitive filesystems.
    return f"-profile-{key}" if match else f"-unknown-profile-{key}"


def _auth_path(
    arch: str, country: Optional[str] = None, profile: Optional[str] = None
) -> Path:
    suffix = f"-{_sanitize_country(country)}" if country else ""
    suffix += _profile_suffix(arch, profile)
    return get_config_dir() / f"auth-{_arch_key(arch)}{suffix}.json"


def save_auth(
    data: dict,
    arch: str = "arm64",
    country: Optional[str] = None,
    profile: Optional[str] = None,
) -> Path:
    """Persist auth data to disk (0600, atomically) and return the file path."""
    data["_cached_at"] = time.time()
    path = _auth_path(arch, country, profile)
    _atomic_pool_json(path, data)
    return path


def load_cached_auth(
    arch: str = "arm64",
    country: Optional[str] = None,
    profile: Optional[str] = None,
) -> Optional[dict]:
    """Return cached auth dict or None."""
    path = _auth_path(arch, country, profile)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_auth() -> None:
    """Remove cached credentials and indexes, preserving stable lock sidecars.

    Also forgets the in-memory account-mode sessions of this config directory
    and every in-memory dispenser 429 backoff window.
    """
    _clear_cached(None)


def _clear_cached(email: Optional[str]) -> None:
    """clear_auth; with *email*, only that account's device records and sessions."""
    directory = get_config_dir()
    acct = _account_hash(email) if email else None
    devices = f"direct-device-{acct}-*.json" if acct else "direct-device-*.json"
    for pattern in ("auth-*.json", "token-pool-*.json", "pool-index-*.json", devices):
        for f in directory.glob(pattern):
            f.unlink(missing_ok=True)
    if acct is None:
        _reset_dispenser_backoff()
    with _direct_guard:
        for store in (_direct_sessions, _direct_devices):
            for key in list(store):
                if key[0] == str(directory) and acct in (None, key[1]):
                    store.pop(key, None)


def _build_httpx_proxy(proxy: Optional[str | httpx.Proxy]) -> Optional[httpx.Proxy]:
    """Build an httpx.Proxy with explicit auth to avoid 407 on some servers."""
    if not proxy:
        return None
    if isinstance(proxy, httpx.Proxy):
        return proxy
    parsed = urlparse(proxy)
    if parsed.username and parsed.password:
        clean_url = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        return httpx.Proxy(url=clean_url, auth=(parsed.username, parsed.password))
    return httpx.Proxy(url=proxy)


def _interpolate_region(proxy: Optional[str], region: str) -> Optional[str]:
    """Swap the country code in a proxy URL for round-robin rotation.

    Supports an explicit {region} placeholder. When absent, detects a
    country code inside the password (SOAX-style, e.g. wifi;us;) and
    replaces it with the selected region.
    """
    if not proxy:
        return None
    if "{region}" in proxy:
        return proxy.replace("{region}", region)
    parsed = urlparse(proxy)
    if parsed.password:
        password = parsed.password
        for code in DISPENSER_PROXY_REGIONS:
            lower_code = code.lower()
            idx = password.lower().find(lower_code)
            if idx != -1:
                new_password = (
                    password[:idx] + region.lower() + password[idx + len(lower_code) :]
                )
                new_netloc = parsed.netloc.replace(parsed.password, new_password, 1)
                return urlunparse(parsed._replace(netloc=new_netloc))
    return proxy


class DirectAuthConfigurationError(GoopdlError, ValueError):
    """Direct Google authentication environment is only partially configured."""


class DirectAuthError(GoopdlError, RuntimeError):
    """Direct Google authentication failed at a non-secret protocol stage."""

    def __init__(
        self, message: str, *, stage: Optional[str] = None,
        status: Optional[int] = None, retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.status = status
        self.retry_after = retry_after


class PoolBusyError(GoopdlError, TimeoutError):
    """The token pool transaction lock could not be acquired in time."""


# Dispenser errors subclass DirectAuthError, the token-acquisition failure
# base: one ``except DirectAuthError`` covers fetch_token(raise_on_error=True).
# They cannot subclass api.PlayAPIError (api imports auth); api.download_app
# translates them into PlayAPIError/RateLimitedError for its callers.
class DispenserRateLimitedError(DirectAuthError):
    """The Aurora token dispenser answered HTTP 429."""

    status = 429

    def __init__(self, retry_after: Optional[float] = None):
        super().__init__(
            "Token dispenser rate limited token requests (HTTP 429).", status=429
        )
        self.retry_after = retry_after


class DispenserUnavailableError(DirectAuthError):
    """No token was issued; *reason* distinguishes profile and endpoint failures."""

    def __init__(self, status: Optional[int] = None, *, reason: Optional[str] = None):
        super().__init__(
            f"Token dispenser unavailable (HTTP {status})."
            if status
            else "Token dispenser unavailable."
        )
        self.status = status
        self.reason = reason or (
            "transport" if status is None else
            "server" if status >= 500 else "profile_rejected"
        )

    def __reduce__(self):
        return type(self), (self.status,), self.__dict__


def _direct_credentials() -> Optional[tuple[str, str]]:
    scope = _scope.get()
    if scope is not None and scope.explicit_credentials:
        return scope.credentials  # never falls back to os.environ
    return _env_credentials()


def _env_credentials() -> Optional[tuple[str, str]]:
    return _checked_credentials(
        os.environ.get("GOOPDL_ACCOUNT_EMAIL", ""),
        os.environ.get("GOOPDL_AAS_TOKEN", ""),
    )


def _checked_credentials(email: str, aas_token: str) -> Optional[tuple[str, str]]:
    """Strip and validate; None when both are empty. Messages carry no secrets."""
    email, aas_token = email.strip(), aas_token.strip()
    if not email and not aas_token:
        return None
    message = None
    if not email:
        message = "Missing required environment variable: GOOPDL_ACCOUNT_EMAIL"
    elif not aas_token:
        message = "Missing required environment variable: GOOPDL_AAS_TOKEN"
    elif not aas_token.startswith("aas_et/"):
        message = "GOOPDL_AAS_TOKEN must start with aas_et/ (not g.a or oauth2_4/)"
    if message:
        email = aas_token = None  # the traceback keeps this frame's locals
        raise DirectAuthConfigurationError(message)
    return email, aas_token


def direct_auth_enabled() -> bool:
    """Return whether complete env-only Google account authentication is enabled."""
    return _direct_credentials() is not None


def _varint(value: int) -> bytes:
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def _proto_varint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _proto_bytes(field: int, value: str | bytes) -> bytes:
    data = value.encode() if isinstance(value, str) else value
    return _varint((field << 3) | WIRETYPE_LENGTH_DELIMITED) + _varint(len(data)) + data


def _profile_bool(profile: dict, key: str) -> int:
    return int(profile.get(key, "false").lower() == "true")


def _device_configuration(profile: dict) -> bytes:
    fields = bytearray()
    for number, key in (
        (1, "TouchScreen"),
        (2, "Keyboard"),
        (3, "Navigation"),
        (4, "ScreenLayout"),
        (7, "Screen.Density"),
        (8, "GL.Version"),
        (12, "Screen.Width"),
        (13, "Screen.Height"),
        (20, "TotalMemoryBytes"),
        (21, "MaxNumOfCPUCores"),
    ):
        if profile.get(key):
            fields += _proto_varint(number, int(profile[key]))
    fields += _proto_varint(5, _profile_bool(profile, "HasHardKeyboard"))
    fields += _proto_varint(6, _profile_bool(profile, "HasFiveWayNavigation"))
    fields += _proto_varint(19, _profile_bool(profile, "LowRamDevice"))
    for number, key in (
        (9, "SharedLibraries"),
        (10, "Features"),
        (11, "Platforms"),
        (14, "Locales"),
        (15, "GL.Extensions"),
    ):
        for value in filter(None, profile.get(key, "").split(",")):
            fields += _proto_bytes(number, value)
    for feature in filter(None, profile.get("Features", "").split(",")):
        fields += _proto_bytes(26, _proto_bytes(1, feature) + _proto_varint(2, 0))
    fields += _proto_varint(16, 0)
    return bytes(fields)


def _google_auth_user_agent(profile: dict) -> str:
    return (
        f"GoogleAuth/1.4 ({profile.get('Build.DEVICE', '')} "
        f"{profile.get('Build.ID', '')})"
    )


def _finsky_user_agent(profile: dict) -> str:
    platforms = profile.get("Platforms", "").replace(",", ";")
    values = (
        ("api", "3"),
        ("versionCode", profile.get("Vending.version", "")),
        ("sdk", profile.get("Build.VERSION.SDK_INT", "")),
        ("device", profile.get("Build.DEVICE", "")),
        ("hardware", profile.get("Build.HARDWARE", "")),
        ("product", profile.get("Build.PRODUCT", "")),
        ("platformVersionRelease", profile.get("Build.VERSION.RELEASE", "")),
        ("model", profile.get("Build.MODEL", "").replace(" ", "%20")),
        ("buildId", profile.get("Build.ID", "")),
        ("isWideScreen", "0"),
        ("supportedAbis", platforms),
    )
    properties = ",".join(f"{key}={value}" for key, value in values)
    return f"Android-Finsky/{profile.get('Vending.versionString', '')} ({properties})"


def _checkin_request(profile: dict, device_config: bytes, locale: str) -> bytes:
    build = b"".join(
        _proto_bytes(number, profile.get(key, "").replace("\\:", ":"))
        for number, key in (
            (1, "Build.FINGERPRINT"),
            (2, "Build.HARDWARE"),
            (3, "Build.BRAND"),
            (4, "Build.RADIO"),
            (5, "Build.BOOTLOADER"),
            (6, "Client"),
        )
    )
    build += _proto_varint(7, int(time.time()))
    build += _proto_varint(8, int(profile.get("GSF.version", "0")))
    build += b"".join(
        _proto_bytes(number, profile.get(key, ""))
        for number, key in (
            (9, "Build.DEVICE"),
            (11, "Build.MODEL"),
            (12, "Build.MANUFACTURER"),
            (13, "Build.PRODUCT"),
        )
    )
    build += _proto_varint(10, int(profile.get("Build.VERSION.SDK_INT", "0")))
    build += _proto_varint(14, _profile_bool(profile, "OtaInstalled"))
    checkin = _proto_bytes(1, build) + _proto_varint(2, 0)
    for number, key in ((6, "CellOperator"), (7, "SimOperator"), (8, "Roaming")):
        checkin += _proto_bytes(number, profile.get(key, ""))
    checkin += _proto_varint(9, 0)
    return b"".join(
        (
            _proto_varint(2, 0),
            _proto_bytes(4, checkin),
            _proto_bytes(6, locale),
            _proto_bytes(12, profile.get("TimeZone", "UTC")),
            _proto_varint(14, 3),
            _proto_bytes(18, device_config),
            _proto_varint(20, 0),
        )
    )


def _field(data: bytes, number: int) -> object:
    for field_number, _wire_type, value in ProtoDecoder(data).read_all_ordered():
        if field_number == number:
            return value
    raise ValueError("Invalid Google authentication response")


def _response_string(data: bytes, *path: int) -> str:
    value: object = data
    for number in path:
        if not isinstance(value, bytes):
            raise ValueError("Invalid Google authentication response")
        value = _field(value, number)
    if not isinstance(value, bytes):
        raise ValueError("Invalid Google authentication response")
    return value.decode("utf-8")


def _direct_auth(
    email: str,
    aas_token: str,
    arch: str,
    country: Optional[str],
    proxy: Optional[str],
    profile_name: Optional[str],
) -> Optional[dict]:
    """Credential boundary for embedders as well as fetch_token callers."""
    try:
        return _direct_step(
            _direct_auth_impl, email, aas_token, arch, country, proxy, profile_name
        )
    finally:
        del email, aas_token, proxy


def _direct_step(step, *args):
    """Run a direct auth protocol step; only a rebuilt DirectAuthError escapes."""
    try:
        return step(*args)
    except DirectAuthError as exc:
        error = DirectAuthError(
            str(exc), stage=exc.stage, status=exc.status, retry_after=exc.retry_after
        )
    except httpx.HTTPError as exc:
        error = DirectAuthError(f"transport ({type(exc).__name__})", stage="transport")
    except Exception:
        error = DirectAuthError("an unexpected stage")
    finally:
        # Only this thin frame survives; all protocol tokens/cookies live inside.
        del args
    raise error


def _direct_http_error(
    stage: str, response: httpx.Response, detail: str = "",
) -> DirectAuthError:
    from goopdl.api import _parse_retry_after  # api imports auth

    status = response.status_code
    return DirectAuthError(
        f"{stage} (HTTP {status}{detail})", stage=stage, status=status,
        retry_after=_parse_retry_after(response.headers) if status == 429 else None,
    )


def _direct_auth_impl(
    email: str,
    aas_token: str,
    arch: str,
    country: Optional[str],
    proxy: Optional[str],
    profile_name: Optional[str],
) -> Optional[dict]:
    selected = _direct_profile(arch, profile_name)
    if not selected:
        return None
    device = _register_device(selected, country, proxy)
    return _exchange(email, aas_token, device, selected, country, proxy)[0]


def _direct_profile(arch: str, profile_name: Optional[str]) -> Optional[tuple[str, dict]]:
    """The profile account mode signs in with; None for an unknown name."""
    if profile_name:
        return find_profile(profile_name, arch)
    return (get_priority_profiles(arch) or [("fallback", FALLBACK_PROFILE)])[0]


def _device_context(
    selected: tuple[str, dict], country: Optional[str]
) -> tuple[dict, str]:
    profile = patch_profile_country(selected[1], country) if country else selected[1]
    locale = (
        _COUNTRY_LOCALE.get(country.upper(), f"en_{country.upper()}")
        if country
        else "en_US"
    )
    return profile, locale


def _session_bundle(device: dict, profile: dict, bearer: str) -> dict:
    return {
        "authToken": bearer,
        "gsfId": device["gsfId"],
        "deviceCheckInConsistencyToken": device["deviceCheckInConsistencyToken"],
        "deviceConfigToken": device["deviceConfigToken"],
        "dfeCookie": "",
        "deviceInfoProvider": {
            "userAgentString": _finsky_user_agent(profile),
            "mccMnc": profile.get("SimOperator", ""),
        },
    }


def _register_device(
    selected: tuple[str, dict], country: Optional[str], proxy: Optional[str]
) -> dict:
    """checkin + uploadDeviceConfig: a new device (gsfId) for the account."""
    profile, locale = _device_context(selected, country)
    auth_user_agent = _google_auth_user_agent(profile)
    device_config = _device_configuration(profile)
    httpx_proxy = _build_httpx_proxy(proxy)

    checkin = _http.post(
        "https://android.clients.google.com/checkin",
        content=_checkin_request(profile, device_config, locale),
        headers={
            "app": "com.google.android.gms",
            "Content-Type": "application/x-protobuffer",
            "Host": "android.clients.google.com",
            "User-Agent": auth_user_agent,
        },
        timeout=30,
        proxy=httpx_proxy,
    )
    if checkin.status_code != 200:
        raise _direct_http_error("checkin", checkin)
    try:
        android_id = _field(checkin.content, 7)
        consistency_token = _response_string(checkin.content, 12)
    except (EOFError, UnicodeDecodeError, ValueError) as exc:
        raise DirectAuthError("checkin response") from exc
    if not isinstance(android_id, int):
        raise DirectAuthError("checkin response")
    device = {
        "gsfId": format(android_id, "x"),
        "deviceCheckInConsistencyToken": consistency_token,
        "deviceConfigToken": "",
    }
    upload_headers = build_headers(_session_bundle(device, profile, ""), country=country)
    upload_headers.pop("Authorization")
    upload_headers["Content-Type"] = "application/x-protobuf"
    upload = _http.post(
        "https://android.clients.google.com/fdfe/uploadDeviceConfig",
        content=_proto_bytes(1, device_config),
        headers=upload_headers,
        timeout=30,
        proxy=httpx_proxy,
    )
    if upload.status_code != 200:
        raise _direct_http_error("device config upload", upload)
    try:
        device["deviceConfigToken"] = _response_string(upload.content, 1, 28, 1)
    except (EOFError, UnicodeDecodeError, ValueError) as exc:
        raise DirectAuthError("device config response") from exc
    return device


def _exchange(
    email: str, aas_token: str, device: dict, selected: tuple[str, dict],
    country: Optional[str], proxy: Optional[str],
) -> tuple[dict, float]:
    """/auth + toc for *device*: (bundle, seconds the bearer may be reused)."""
    profile, locale = _device_context(selected, country)
    auth_user_agent = _google_auth_user_agent(profile)
    httpx_proxy = _build_httpx_proxy(proxy)
    gsf_id = device["gsfId"]
    auth_response = _http.post(
        "https://android.clients.google.com/auth",
        data={
            "Email": email,
            "Token": aas_token,
            "service": "oauth2:https://www.googleapis.com/auth/googleplay",
            "app": "com.android.vending",
            "client_sig": "38918a453d07199354f8b19af05ec6562ced5788",
            "callerPkg": "com.google.android.gms",
            "callerSig": "38918a453d07199354f8b19af05ec6562ced5788",
            "androidId": gsf_id,
            "google_play_services_version": profile.get("GSF.version", ""),
            "sdk_version": profile.get("Build.VERSION.SDK_INT", ""),
            "device_country": (country or "US").lower(),
            "lang": locale.split("_", 1)[0].lower(),
            "oauth2_foreground": "1",
            "token_request_options": "CAA4AVAB",
            "check_email": "1",
            "system_partition": "1",
            "droidguard_results": "null",
        },
        headers={
            "app": "com.google.android.gms",
            "device": gsf_id,
            "User-Agent": auth_user_agent,
        },
        timeout=30,
        proxy=httpx_proxy,
    )
    auth_values = dict(
        line.split("=", 1) for line in auth_response.text.splitlines() if "=" in line
    )
    if auth_response.status_code != 200:
        reason = auth_values.get("Error", "")
        safe_reason = reason if reason in {
            "BadAuthentication", "NeedsBrowser", "TokenExpired", "ServiceUnavailable"
        } else ""
        detail = f", {safe_reason}" if safe_reason else ""
        raise _direct_http_error("token exchange", auth_response, detail)
    bearer = auth_values.get("Auth")
    if not bearer:
        raise DirectAuthError("token exchange response")
    ttl = float(_MAX_TOKEN_AGE)
    try:
        if "ExpiresInDurationSec" in auth_values:
            ttl = float(auth_values["ExpiresInDurationSec"]) - _DIRECT_EXPIRY_MARGIN
        elif "Expiry" in auth_values:
            ttl = float(auth_values["Expiry"]) - time.time() - _DIRECT_EXPIRY_MARGIN
    except ValueError:
        pass

    bundle = _session_bundle(device, profile, bearer)
    toc_headers = build_headers(bundle, country=country)
    toc = _http.get(
        "https://android.clients.google.com/fdfe/toc",
        headers=toc_headers,
        timeout=30,
        proxy=httpx_proxy,
    )
    if toc.status_code == 429:
        # toc is optional (it only adds dfeCookie): keep the token, like before.
        logger.warning(
            "Google toc request rate limited (HTTP 429); continuing without dfeCookie."
        )
    if toc.status_code == 200:
        try:
            bundle["dfeCookie"] = _response_string(toc.content, 1, 6, 22)
        except (EOFError, UnicodeDecodeError, ValueError):
            pass
    return bundle, ttl


def fetch_token(
    dispenser_url: Optional[str] = None,
    arch: str = "arm64",
    proxy: Optional[str] = None,
    country: Optional[str] = None,
    profile: Optional[str] = None,
    raise_on_error: bool = False,
    *,
    cooldown_retry: bool = True,
) -> Optional[dict]:
    """Obtain direct Google auth when configured, otherwise use the dispenser.

    Anonymous mode rotates profiles until one yields an authToken, stopping on
    throttling or two consecutive transport/server failures. With *country*,
    patches device operators to register in that region. A fixed *profile*
    retries once after a 5 s cooldown on transport/5xx failures unless
    *cooldown_retry* is False (callers that already hold usable tokens).
    Returns the auth dict on success, None on failure or an unknown profile. With *raise_on_error*, raises
    DispenserRateLimitedError (429), DispenserUnavailableError (failed attempts)
    or DirectAuthError (direct mode), without retaining credential frames.
    """
    try:
        return _fetch_token_impl(
            dispenser_url, arch, proxy, country, profile, raise_on_error,
            cooldown_retry,
        )
    except DirectAuthConfigurationError as exc:
        error = DirectAuthConfigurationError(str(exc))
    except DispenserRateLimitedError as exc:
        error = DispenserRateLimitedError(exc.retry_after)
    except DispenserUnavailableError as exc:
        error = DispenserUnavailableError(exc.status, reason=exc.reason)
    except DirectAuthError as exc:
        error = DirectAuthError(
            str(exc), stage=exc.stage, status=exc.status, retry_after=exc.retry_after
        )
    except GoopdlError as exc:
        # A pre-sanitized NetworkError is also an httpx.TransportError. Keep its
        # identity, but detach protocol frames just like newly minted errors.
        error = exc.with_traceback(None)
        error.__cause__ = error.__context__ = None
    finally:
        proxy = dispenser_url = None
    raise error


_MAX_DISPENSER_TRANSPORT_FAILURES = 2


def _fetch_token_impl(
    dispenser_url: Optional[str] = None,
    arch: str = "arm64",
    proxy: Optional[str] = None,
    country: Optional[str] = None,
    profile: Optional[str] = None,
    raise_on_error: bool = False,
    cooldown_retry: bool = True,
) -> Optional[dict]:
    """Protocol implementation; only fetch_token's sanitized frame may escape."""
    credentials = _direct_credentials()
    if credentials:
        try:
            return _direct_auth(
                *credentials,
                arch=arch,
                country=country,
                proxy=proxy,
                profile_name=profile,
            )
        except DirectAuthError as exc:
            logger.error("Direct Google authentication failed at %s.", exc)
            error = DirectAuthError(
                str(exc), stage=exc.stage, status=exc.status, retry_after=exc.retry_after
            )
        except GoopdlError:
            raise
        except Exception:
            # No exc_info: tracebacks can carry credentials from request locals.
            logger.error("Direct Google authentication failed unexpectedly.")
            error = DirectAuthError("an unexpected stage")
        # Raised outside the except blocks so neither __context__ nor the
        # traceback reach _direct_auth's frames (locals hold the AAS token).
        credentials = None
        if raise_on_error:
            raise error
        return None

    url = dispenser_url or DEFAULT_DISPENSER_URL
    backoff_key = _dispenser_backoff_key(url, proxy)
    remaining = _dispenser_backoff_remaining(backoff_key)
    if remaining is not None:
        # Treated as the 429 it repeats, without sending anything.
        logger.warning(
            "Dispenser returned HTTP 429 earlier; backing off for %ds.", remaining
        )
        if raise_on_error:
            raise DispenserRateLimitedError(remaining)
        return None
    headers = {
        "User-Agent": "com.aurora.store-4.6.1-70",
        "Content-Type": "application/json",
    }

    if profile:
        match = find_profile(profile, arch)
        if not match:
            logger.error("Profile not found: %s", profile)
            return None
        # One cooldown retry for this fixed profile on transport/5xx failures.
        profiles = [match, match] if cooldown_retry else [match]
    else:
        profiles = get_priority_profiles(arch) or [("fallback", FALLBACK_PROFILE)]

    # ponytail: random start so single-profile callers don't always land on
    # the same region (index 0) and pile all their requests on one exit IP
    region_start = random.randrange(len(DISPENSER_PROXY_REGIONS))
    last_status: Optional[int] = None
    last_reason = "transport"
    transport_failures = 0
    for idx, (profile_name, profile_data) in enumerate(profiles):
        if profile and transport_failures:
            time.sleep(5)
        device = profile_data.get("UserReadableName", profile_name)
        payload = (
            patch_profile_country(profile_data, country) if country else profile_data
        )
        try:
            region = DISPENSER_PROXY_REGIONS[
                (region_start + idx) % len(DISPENSER_PROXY_REGIONS)
            ]
            proxy_url = _interpolate_region(proxy, region)
            httpx_proxy = _build_httpx_proxy(proxy_url)
            resp = _http.post(
                url, json=payload, headers=headers, timeout=30, proxy=httpx_proxy
            )
            last_status = resp.status_code
            last_reason = "server" if resp.status_code >= 500 else "profile_rejected"
            if resp.status_code >= 500:
                logger.warning(
                    "Dispenser returned HTTP %s for %s", resp.status_code, device
                )
                transport_failures += 1
                if transport_failures >= _MAX_DISPENSER_TRANSPORT_FAILURES:
                    break
                continue
            transport_failures = 0
            if resp.status_code == 200:
                data = resp.json()
                if data.get("authToken"):
                    with _dispenser_backoff_lock:
                        _dispenser_backoff.pop(backoff_key, None)
                    logger.info("Authenticated with profile: %s", device)
                    data["_device_profile"] = device
                    return data
                logger.warning("No authToken in dispenser response for %s", device)
            else:
                logger.warning(
                    "Dispenser returned HTTP %s for %s", resp.status_code, device
                )
                if resp.status_code == 429:
                    from goopdl.api import _parse_retry_after  # api imports auth

                    retry_after = _parse_retry_after(resp.headers)
                    _dispenser_backoff_start(backoff_key, retry_after)
                    if raise_on_error:
                        raise DispenserRateLimitedError(retry_after)
                    return None
            if profile:
                break  # rejection is profile-specific, not a transient outage
        except GoopdlError:
            raise
        except httpx.TransportError as exc:
            logger.warning(
                "Dispenser transport failed for %s: %s", device, type(exc).__name__
            )
            last_status = None
            last_reason = "transport"
            transport_failures += 1
            if transport_failures >= _MAX_DISPENSER_TRANSPORT_FAILURES:
                break
        except Exception as exc:
            logger.warning(
                "Dispenser request failed for %s: %s", device, type(exc).__name__
            )
            if profile:
                break  # malformed responses do not earn the transient retry
            continue

    if raise_on_error:
        raise DispenserUnavailableError(last_status, reason=last_reason)
    return None


# ── dispenser 429 backoff (in memory, per process) ────────────────────────────
# After a dispenser 429, fetch_token sends nothing to that dispenser+proxy
# until the window ends: it answers as if the 429 repeated (retry_after = the
# seconds left). The window is Retry-After capped at _DISPENSER_BACKOFF_MAX,
# or _DISPENSER_BACKOFF_DEFAULT without a usable header (a heuristic: the
# dispenser does not say how long its limit lasts).
_DISPENSER_BACKOFF_DEFAULT = 60.0
_DISPENSER_BACKOFF_MAX = 900.0
# ponytail: unbounded; one entry per dispenser URL x proxy seen in a 429 window
# (expired ones are purged on lookup). Add an LRU if proxies rotate per call.
_dispenser_backoff: dict[str, float] = {}  # key hash -> time.monotonic() deadline
_dispenser_backoff_lock = threading.Lock()


def _dispenser_backoff_key(url: str, proxy: Optional[str]) -> str:
    """A hash, so neither the proxy nor its password is kept in the dict."""
    return hashlib.sha256(f"{url}\0{proxy or ''}".encode()).hexdigest()


def _dispenser_backoff_start(key: str, retry_after: Optional[float]) -> None:
    seconds = min(retry_after or _DISPENSER_BACKOFF_DEFAULT, _DISPENSER_BACKOFF_MAX)
    with _dispenser_backoff_lock:
        _dispenser_backoff[key] = time.monotonic() + seconds


def _dispenser_backoff_remaining(key: str) -> Optional[float]:
    """Whole seconds left in *key*'s window (at least 1), or None."""
    now = time.monotonic()
    with _dispenser_backoff_lock:
        for stale in [k for k, end in _dispenser_backoff.items() if end <= now]:
            del _dispenser_backoff[stale]
        end = _dispenser_backoff.get(key)
    return None if end is None else float(math.ceil(end - now))


def _reset_dispenser_backoff() -> None:
    """Forget every dispenser backoff window (clear_auth, tests)."""
    with _dispenser_backoff_lock:
        _dispenser_backoff.clear()


def _reset_dispenser_backoff_after_fork() -> None:
    global _dispenser_backoff_lock
    _dispenser_backoff.clear()
    _dispenser_backoff_lock = threading.Lock()


if hasattr(os, "register_at_fork"):  # a child must not inherit a held lock
    os.register_at_fork(after_in_child=_reset_dispenser_backoff_after_fork)


_MAX_TOKEN_AGE = 50 * 60  # 50 minutes — refresh before the ~1h Google expiry

# ── token pool ────────────────────────────────────────────────────────────────

# Number of GSF ID / token pairs maintained per region. Enough to detect
# staged-rollout version differences while staying well under Aurora's ~20
# requests/hour rate limit.
DEFAULT_PROBES = 5


def _pool_path(
    arch: str, country: Optional[str], profile: Optional[str] = None
) -> Path:
    suffix = f"-{_sanitize_country(country)}" if country else ""
    suffix += _profile_suffix(arch, profile)
    return get_config_dir() / f"token-pool-{_arch_key(arch)}{suffix}.json"


_POOL_LOCK_TIMEOUT = 180.0  # seconds; a cold five-token pool can take 5 x 30s
# Busy-lock poll: exponential backoff from MIN, doubling up to MAX (seconds).
# Each sleep is jittered to [delay/2, delay]: waiters that lost the same race
# would otherwise wake in lockstep and leave a freed lock idle. Own RNG, so lock
# contention never shifts the global random stream (e.g. region_start).
_LOCK_POLL_MIN = 0.001
_LOCK_POLL_MAX = 0.05
_LOCK_POLL_JITTER = random.Random()


def _pool_cache_error(exc: OSError) -> None:
    detail = ("lock acquisition timed out: TimeoutError"
              if isinstance(exc, TimeoutError) else type(exc).__name__)
    logger.error(
        "Token pool cache unavailable (%s); anonymous token acquisition stopped.",
        detail,
    )


def _ensure_config_dir(directory: Path) -> None:
    """Create the token directory 0700 and tighten an existing one we own.

    Older releases created it 0755; it holds tokens. Best-effort, POSIX only.
    """
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "nt":
        return
    try:
        info = directory.stat()
        if info.st_uid == os.getuid() and info.st_mode & 0o077:
            directory.chmod(0o700)
    except OSError:
        pass


@contextmanager
def _pool_lock(
    arch: str, country: Optional[str], profile: Optional[str] = None,
    *, blocking: bool = True, raise_on_error: bool = False,
) -> Iterator[bool]:
    """Local, per-pool transaction lock. Never unlink/replace the stable sidecar.

    Independent open handles serialize threads as well as processes. Closing the
    handle releases the native lock, including on exceptions or process death.
    """
    path = _pool_path(arch, country, profile).with_suffix(".lock")
    try:
        handle = _acquire_file_lock(path, blocking=blocking)
    except OSError as exc:
        _pool_cache_error(exc)
        if raise_on_error and isinstance(exc, TimeoutError):
            raise PoolBusyError("Token pool lock acquisition timed out") from None
        yield False
        return
    if handle is None:
        yield False
        return
    try:
        yield True
    finally:
        handle.close()


def _acquire_file_lock(path: Path, *, blocking: bool = True):
    """Open and lock the stable sidecar *path*; close the handle to release.

    None when *blocking* is False and another holder has it. Raises OSError,
    TimeoutError after the patchable _POOL_LOCK_TIMEOUT.
    """
    handle = None
    acquired = False
    try:
        _ensure_config_dir(path.parent)
        if path.is_symlink():
            raise OSError(errno.ELOOP, "Token pool lock must not be a symlink")
        fd = os.open(
            path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            # Also check platforms without O_NOFOLLOW before writing or locking.
            if path.is_symlink() or not os.path.samestat(path.lstat(), os.fstat(fd)):
                raise OSError(errno.ELOOP, "Token pool lock changed during open")
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "r+b", buffering=0)
        except BaseException:
            os.close(fd)
            raise
        if os.name == "nt":
            import msvcrt
        else:
            import fcntl
        deadline = time.monotonic() + _POOL_LOCK_TIMEOUT
        delay = _LOCK_POLL_MIN
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                contention = ((errno.EACCES,) if os.name == "nt"
                              else (errno.EACCES, errno.EAGAIN))
                if exc.errno not in contention:
                    raise
                if not blocking:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Token pool lock acquisition timed out") from exc
                time.sleep(min(_LOCK_POLL_JITTER.uniform(delay / 2, delay), remaining))
                delay = min(delay * 2, _LOCK_POLL_MAX)
        # Windows permits locking beyond EOF. Initialize only AFTER acquisition,
        # otherwise a simultaneous first opener could write another owner's byte.
        if acquired and os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
    except BaseException:
        if handle is not None:
            handle.close()
        raise
    if not acquired:
        handle.close()
        return None
    return handle


def _load_pool(
    arch: str, country: Optional[str], profile: Optional[str] = None
) -> list[dict]:
    """Read a snapshot without minting; a busy pool is silently unavailable.

    Coordination prevents an advisory Windows reader from blocking os.replace.
    """
    with _pool_lock(arch, country, profile, blocking=False) as acquired:
        if acquired:
            try:
                return _load_pool_unlocked(arch, country, profile)
            except OSError as exc:
                _pool_cache_error(exc)
    return []


def _load_pool_unlocked(
    arch: str, country: Optional[str], profile: Optional[str] = None
) -> list[dict]:
    try:
        tokens: list[dict] = json.loads(_pool_path(arch, country, profile).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    now = time.time()
    return [t for t in tokens if now - t.get("_cached_at", 0) < _MAX_TOKEN_AGE]


def _atomic_pool_json(path: Path, data: object) -> None:
    """Atomically publish a whole JSON document as 0600 (mkstemp's mode).

    Pool/index callers own the pool lock.
    """
    _ensure_config_dir(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=path.name + ".", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _save_pool(
    tokens: list[dict], arch: str, country: Optional[str], profile: Optional[str] = None
) -> None:
    _atomic_pool_json(_pool_path(arch, country, profile), tokens)


def ensure_pool(
    arch: str = "arm64",
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    dispenser_url: Optional[str] = None,
    profile: Optional[str] = None,
    size: int = DEFAULT_PROBES,
    raise_on_error: bool = False,
) -> list[dict]:
    """Ensure the arch+country+profile pool has exactly *size* valid tokens.

    Only hits the Aurora dispenser for the deficit — if the pool already has
    *size* unexpired tokens, no network call is made at all.
    Returns the full list of valid tokens (may be fewer than *size* if the
    dispenser is rate-limiting). With *raise_on_error*, propagate typed token
    failures when no usable token remains and PoolBusyError on lock timeout.
    """
    if direct_auth_enabled():
        tokens = []
        for _ in range(size):
            token = fetch_token(
                arch=arch, country=country, proxy=proxy, profile=profile,
                raise_on_error=raise_on_error,
            )
            if token is None:
                break
            tokens.append(token)
        return tokens

    with _pool_lock(arch, country, profile, raise_on_error=raise_on_error) as acquired:
        if acquired:
            try:
                return _ensure_pool_unlocked(
                    arch, country, proxy, dispenser_url, profile, size, raise_on_error
                )
            except OSError as exc:
                _pool_cache_error(exc)
    return []


def _ensure_pool_unlocked(
    arch: str, country: Optional[str], proxy: Optional[str],
    dispenser_url: Optional[str], profile: Optional[str], size: int,
    raise_on_error: bool = False,
) -> list[dict]:
    pool = _load_pool_unlocked(arch, country, profile)
    deficit = size - len(pool)
    if deficit > 0:
        label = country or "default"
        logger.info(
            "Pool [%s]: %d/%d valid — fetching %d more...",
            label, len(pool), size, deficit,
        )
        _save_pool(pool, arch, country, profile)
        for _ in range(deficit):
            try:
                t = fetch_token(
                    dispenser_url=dispenser_url,
                    arch=arch,
                    proxy=proxy,
                    profile=profile,
                    country=country,
                    raise_on_error=raise_on_error,
                    # Usable tokens already serve the caller: no 5 s cooldown.
                    cooldown_retry=not pool,
                )
            except DirectAuthError:
                # 429 included: fetch_token stopped at the first one, and the
                # next call retries the refill. Raise only with no token left.
                if pool:
                    logger.warning(
                        "Pool [%s]: refill stopped; using %d/%d valid tokens.",
                        label, len(pool), size,
                    )
                    break
                raise
            if t is None:
                break
            t.setdefault("_cached_at", time.time())
            pool.append(t)
            # Preserve each paid mint even if a later request is interrupted.
            _save_pool(pool, arch, country, profile)
    return pool


def _index_path(
    arch: str, country: Optional[str], profile: Optional[str] = None
) -> Path:
    suffix = f"-{_sanitize_country(country)}" if country else ""
    suffix += _profile_suffix(arch, profile)
    return get_config_dir() / f"pool-index-{_arch_key(arch)}{suffix}.json"


def _read_index(
    arch: str, country: Optional[str], profile: Optional[str] = None
) -> int:
    try:
        return json.loads(_index_path(arch, country, profile).read_text()).get("i", 0)
    except (FileNotFoundError, json.JSONDecodeError):
        return 0


def _write_index(
    i: int, arch: str, country: Optional[str], profile: Optional[str] = None
) -> None:
    _atomic_pool_json(_index_path(arch, country, profile), {"i": i})


def pick_pool_token(
    arch: str = "arm64",
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    dispenser_url: Optional[str] = None,
    profile: Optional[str] = None,
    size: int = DEFAULT_PROBES,
    raise_on_error: bool = False,
) -> Optional[dict]:
    """Ensure the pool is full, then return the next token in round-robin order.

    Expired tokens are pruned and refetched by ensure_pool before picking.
    The round-robin index is persisted to disk so it survives across invocations.
    *raise_on_error* raises fetch_token's typed errors when no token is left,
    or PoolBusyError if the patchable _POOL_LOCK_TIMEOUT expires.
    """
    if direct_auth_enabled():
        return _direct_session(
            arch, country, proxy, profile, raise_on_error=raise_on_error
        )

    with _pool_lock(arch, country, profile, raise_on_error=raise_on_error) as acquired:
        if acquired:
            try:
                pool = _ensure_pool_unlocked(
                    arch, country, proxy, dispenser_url, profile, size,
                    raise_on_error,
                )
                if not pool:
                    return None
                i = _read_index(arch, country, profile)
                token = pool[i % len(pool)]
                _write_index(i + 1, arch, country, profile)
                return token
            except OSError as exc:
                _pool_cache_error(exc)
    return None


def replace_pool_token(
    failed_token: dict,
    arch: str = "arm64",
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    dispenser_url: Optional[str] = None,
    profile: Optional[str] = None,
    raise_on_error: bool = False,
) -> Optional[dict]:
    """Remove *failed_token* from the pool and replace it with a fresh one.

    Called when a mid-request AuthExpiredError reveals a token died early.
    Reuses a current member if another caller already removed that credential.
    On replacement failure, returns a surviving token without refilling (or
    None if none remain). HTTP 429 returns a survivor too, even with
    *raise_on_error*, and raises DispenserRateLimitedError only when none
    remain. With *raise_on_error*, propagate other typed token failures as well
    as PoolBusyError on lock timeout, so probe loops can skip the failed member.
    """
    if direct_auth_enabled():
        return _direct_session(
            arch, country, proxy, profile, failed=failed_token,
            raise_on_error=raise_on_error,
        )

    with _pool_lock(arch, country, profile, raise_on_error=raise_on_error) as acquired:
        if acquired:
            try:
                pool = _load_pool_unlocked(arch, country, profile)
                failed = (failed_token.get("gsfId"), failed_token.get("authToken"))
                survivors = [
                    t for t in pool
                    if (t.get("gsfId"), t.get("authToken")) != failed
                ]
                if len(survivors) == len(pool) and survivors:
                    # Another caller already replaced this exact credential.
                    return survivors[0]
                _save_pool(survivors, arch, country, profile)
                try:
                    # With no survivor, keep 429 distinguishable from a failed
                    # replacement even for legacy callers (it raises); never
                    # refill just to pick a survivor.
                    # Survivors already serve the caller: no cooldown retry.
                    new_token = fetch_token(
                        dispenser_url=dispenser_url, arch=arch, proxy=proxy,
                        profile=profile, country=country, raise_on_error=True,
                        cooldown_retry=not survivors,
                    )
                except DispenserRateLimitedError:
                    if survivors:
                        return survivors[0]  # the next call retries the refill
                    raise
                except DirectAuthError:
                    if raise_on_error:
                        raise
                    new_token = None
                if new_token:
                    new_token["_cached_at"] = time.time()
                    survivors.append(new_token)
                    _save_pool(survivors, arch, country, profile)
                    return new_token
                return survivors[0] if survivors and not raise_on_error else None
            except OSError as exc:
                _pool_cache_error(exc)
    return None


# ── account-mode sessions ─────────────────────────────────────────────────────
# The bearer stays in process memory: one per (config dir, account, arch,
# country, profile), reused until it expires or Play rejects it with 401.
# fetch_token/ensure_pool stay fresh on purpose (cohort probes).

_DIRECT_EXPIRY_MARGIN = 300  # seconds before Expiry, like microG


@dataclass
class _DirectEntry:
    bundle: dict = field(repr=False)
    expires_at: float
    replaced: bool = False  # minted after a 401: another 401 retires the device


# ponytail: unbounded dicts; keys are bounded by dirs x accounts x arch x
# country x profile. Add an LRU if a backend multiplexes thousands of accounts.
_direct_sessions: dict[tuple, _DirectEntry] = {}
_direct_devices: dict[tuple, dict] = {}
_direct_locks: dict[tuple, threading.Lock] = {}
_direct_guard = threading.Lock()


def _reset_direct_cache() -> None:
    """Forget every in-memory session, device and lock (also after fork)."""
    global _direct_guard
    _direct_sessions.clear()
    _direct_devices.clear()
    _direct_locks.clear()
    _direct_guard = threading.Lock()


if hasattr(os, "register_at_fork"):  # a child must not inherit held locks
    os.register_at_fork(after_in_child=_reset_direct_cache)


def _account_hash(email: str) -> str:
    digest = hashlib.sha256(b"goopdl-direct-device-v1\0" + email.casefold().encode())
    return digest.hexdigest()[:16]


def _session_copy(bundle: dict) -> dict:
    return {**bundle, "deviceInfoProvider": dict(bundle["deviceInfoProvider"])}


def _direct_session(
    arch: str, country: Optional[str], proxy: Optional[str], profile: Optional[str],
    *, failed: Optional[dict] = None, force: bool = False,
    raise_on_error: bool = False,
) -> Optional[dict]:
    """Account-mode token for pick/replace_pool_token and ensure_auth.

    Reuses the cached bearer unless it expired, *force* is set or *failed*
    (the token Play rejected) is it. Failures are not cached; errors, the log
    line and frame hygiene match fetch_token's.
    """
    try:
        return _direct_session_impl(arch, country, proxy, profile, failed, force)
    except PoolBusyError:
        logger.error("Direct auth session lock acquisition timed out.")
        error: GoopdlError = PoolBusyError("Direct auth session lock acquisition timed out")
    except DirectAuthError as exc:
        logger.error("Direct Google authentication failed at %s.", exc)
        error = DirectAuthError(
            str(exc), stage=exc.stage, status=exc.status, retry_after=exc.retry_after
        )
    except GoopdlError as exc:
        error = exc.with_traceback(None)
        error.__cause__ = error.__context__ = None
        raise_on_error = True  # configuration errors raise, like fetch_token
    except Exception:
        # No exc_info: tracebacks can carry credentials from request locals.
        logger.error("Direct Google authentication failed unexpectedly.")
        error = DirectAuthError("an unexpected stage")
    finally:
        proxy = failed = None
    if raise_on_error:
        raise error
    return None


def _direct_session_impl(
    arch: str, country: Optional[str], proxy: Optional[str], profile: Optional[str],
    failed: Optional[dict], force: bool,
) -> Optional[dict]:
    email, aas_token = _direct_credentials()  # type: ignore[misc]
    try:
        selected = _direct_profile(arch, profile)
        if not selected:
            return None
        devkey = (str(get_config_dir()), _account_hash(email), arch,
                  country.upper() if country else None, selected[0])
        # The AAS token is part of the key: a rotated one never reuses a bearer.
        key = devkey + (hashlib.sha256(
            f"goopdl-direct-session-v1\0{email.casefold()}\0{aas_token}".encode()
        ).hexdigest(),)
        entry = _direct_sessions.get(key)
        if failed is None and not force and entry and entry.expires_at > time.time():
            return _session_copy(entry.bundle)
        with _direct_guard:
            lock = _direct_locks.setdefault(devkey, threading.Lock())
        if not lock.acquire(timeout=_POOL_LOCK_TIMEOUT):
            raise PoolBusyError("Direct auth session lock acquisition timed out")
        try:
            entry = _direct_sessions.get(key)
            replaced = reset = False
            if entry and (force or (
                failed is not None and entry.bundle["authToken"] == failed.get("authToken")
            )):
                del _direct_sessions[key]  # compare-and-invalidate
                reset = failed is not None and entry.replaced  # second 401 in a row
                replaced, entry = failed is not None, None
            if entry and entry.expires_at > time.time():
                return _session_copy(entry.bundle)  # another caller renewed it
            device = _direct_device(devkey, selected, country, proxy, reset)
            bundle, ttl = _direct_step(
                _exchange, email, aas_token, device, selected, country, proxy
            )
            _direct_sessions[key] = _DirectEntry(bundle, time.time() + ttl, replaced)
            return _session_copy(bundle)
        finally:
            lock.release()
    finally:
        email = aas_token = None  # type: ignore[assignment]


_DEVICE_SCHEMA = 1
_DEVICE_FIELDS = ("gsfId", "deviceCheckInConsistencyToken", "deviceConfigToken")


def _device_path(
    acct: str, arch: str, country: Optional[str], profile_key: str
) -> Optional[Path]:
    """direct-device-<account hash>-<arch>[-CC]-profile-<key>.json, or None."""
    suffix = f"-{_sanitize_country(country)}" if country else ""
    try:
        suffix += _profile_suffix(arch, profile_key)
        return get_config_dir() / f"direct-device-{acct}-{_arch_key(arch)}{suffix}.json"
    except ValueError:  # unknown arch or unsafe key: keep this one in memory
        return None


def _load_device(path: Path, acct: str) -> Optional[dict]:
    """A valid persisted registration, or None (a bad one is logged and ignored)."""
    if path.is_symlink():
        raise OSError(errno.ELOOP, "Direct auth device record must not be a symlink")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:  # truncated, not JSON or not UTF-8
        data = None
    if (
        isinstance(data, dict) and data.get("v") == _DEVICE_SCHEMA
        and data.get("acct") == acct
        and all(isinstance(data.get(k), str) and data[k].isprintable()
                for k in _DEVICE_FIELDS)
        and re.fullmatch(r"[0-9a-f]{1,16}", data["gsfId"]) and data["deviceConfigToken"]
    ):
        return {k: data[k] for k in _DEVICE_FIELDS}
    logger.warning("Discarding an unusable direct-auth device record (%s).", path.name)
    return None


def _direct_device(
    devkey: tuple, selected: tuple[str, dict], country: Optional[str],
    proxy: Optional[str], reset: bool,
) -> dict:
    """The account's device for *devkey*; registers one if none (or *reset*).

    The registration (never the email, AAS token or bearer) is shared with
    other processes through a 0600 file; without a usable disk it stays in
    memory, as account mode never required one.
    """
    _, acct, arch, _, profile_key = devkey
    path = _device_path(acct, arch, country, profile_key)
    known = None if reset else _direct_devices.get(devkey)
    device = None
    if path is not None:
        try:
            # One registration across threads (devkey lock) and processes.
            handle = _acquire_file_lock(path.with_suffix(".lock"))
            assert handle is not None  # blocking: acquired or raised
            try:
                if reset:
                    path.unlink(missing_ok=True)
                device = _load_device(path, acct)
                if device is None:
                    device = known or _new_device(selected, country, proxy)
                    _atomic_pool_json(path, {"v": _DEVICE_SCHEMA, "acct": acct,
                                             **device, "created_at": time.time()})
                else:
                    logger.debug("Reusing the direct-auth device ***%s.",
                                 device["gsfId"][-4:])
            finally:
                handle.close()
        except TimeoutError:
            raise PoolBusyError("Direct auth device lock acquisition timed out") from None
        except OSError as exc:
            logger.warning(
                "Direct auth device cache unavailable (%s); continuing in memory.",
                type(exc).__name__,
            )
    if device is None:
        device = known or _new_device(selected, country, proxy)
    _direct_devices[devkey] = device
    return device


def _new_device(
    selected: tuple[str, dict], country: Optional[str], proxy: Optional[str]
) -> dict:
    device = _direct_step(_register_device, selected, country, proxy)
    logger.debug("Registered a direct-auth device ***%s.", device["gsfId"][-4:])
    return device


def _direct_device_known(
    arch: str, country: Optional[str], profile: Optional[str] = None
) -> bool:
    """Whether account mode already registered a device for *profile* (no I/O lock)."""
    email = _direct_credentials()[0]  # type: ignore[index]
    acct = _account_hash(email)
    email = None
    selected = _direct_profile(arch, profile)
    if not selected:
        return False
    devkey = (str(get_config_dir()), acct, arch,
              country.upper() if country else None, selected[0])
    path = _device_path(acct, arch, country, selected[0])
    return devkey in _direct_devices or (path is not None and path.is_file())


def ensure_auth(
    arch: str = "arm64",
    dispenser_url: Optional[str] = None,
    force_refresh: bool = False,
    proxy: Optional[str] = None,
    country: Optional[str] = None,
    profile: Optional[str] = None,
) -> Optional[dict]:
    """Return cached auth or fetch a new token transparently.

    Each arch+country+profile gets its own cache file.
    Proactively refreshes tokens older than 50 minutes.
    Pass *force_refresh=True* to ignore cache entirely (e.g. after a 401).
    """
    if direct_auth_enabled():
        return _direct_session(arch, country, proxy, profile, force=force_refresh)

    if not force_refresh:
        cached = load_cached_auth(arch, country, profile)
        if cached and cached.get("authToken"):
            age = time.time() - cached.get("_cached_at", 0)
            if age < _MAX_TOKEN_AGE:
                return cached
            logger.info("Token expired — refreshing...")
    else:
        logger.info("Refreshing token...")

    data = fetch_token(
        dispenser_url=dispenser_url,
        arch=arch,
        proxy=proxy,
        country=country,
        profile=profile,
    )
    if data:
        save_auth(data, arch, country, profile)
    return data


_COUNTRY_LOCALE: dict[str, str] = {
    "CN": "zh_CN",
    "TW": "zh_TW",
    "HK": "zh_HK",
    "JP": "ja_JP",
    "KR": "ko_KR",
    "RU": "ru_RU",
    "DE": "de_DE",
    "FR": "fr_FR",
    "ES": "es_ES",
    "IT": "it_IT",
    "PT": "pt_BR",
    "BR": "pt_BR",
    "AR": "es_AR",
    "MX": "es_MX",
    "SA": "ar_SA",
    "TR": "tr_TR",
    "PL": "pl_PL",
    "NL": "nl_NL",
    "SE": "sv_SE",
    "NO": "nb_NO",
    "TH": "th_TH",
    "VN": "vi_VN",
    "ID": "in_ID",
}


def build_headers(auth: dict, country: Optional[str] = None) -> dict[str, str]:
    """Construct HTTP headers for Google Play FDFE requests."""
    device_info = auth.get("deviceInfoProvider", {})
    cc = country.upper() if country else None
    # ponytail: default en_XX for unknown countries, specific mapping for known ones
    locale = _COUNTRY_LOCALE.get(cc, f"en_{cc}") if cc else "en_US"

    headers = {
        "Authorization": f"Bearer {auth['authToken']}",
        "User-Agent": device_info.get(
            "userAgentString",
            (
                "Android-Finsky/41.2.29-23 [0] [PR] 639844241 "
                "(api=3,versionCode=84122900,sdk=34,device=lynx,"
                "hardware=lynx,product=lynx,platformVersionRelease=14,"
                "model=Pixel%207a,buildId=UQ1A.231205.015,"
                "isWideScreen=0,supportedAbis=arm64-v8a;armeabi-v7a;armeabi)"
            ),
        ),
        "X-DFE-Device-Id": auth.get("gsfId", ""),
        "Accept-Language": locale.replace("_", "-"),
        "X-DFE-Encoded-Targets": (
            "CAESN/qigQYC2AMBFfUbyA7SM5Ij/CvfBoIDgxXrBPsDlQUdMfOLAfoFrwEH"
            "gAcBrQYhoA0cGt4MKK0Y2gI"
        ),
        "X-DFE-Client-Id": "am-android-google",
        "X-DFE-Network-Type": "4",
        "X-DFE-Content-Filters": "",
        "X-Limit-Ad-Tracking-Enabled": "false",
        "X-Ad-Id": "",
        "X-DFE-UserLanguages": locale,
        "X-DFE-Request-Params": "timeoutMs=4000",
        "X-DFE-Cookie": auth.get("dfeCookie", ""),
        "X-DFE-No-Prefetch": "true",
    }

    if auth.get("deviceCheckInConsistencyToken"):
        headers["X-DFE-Device-Checkin-Consistency-Token"] = auth[
            "deviceCheckInConsistencyToken"
        ]
    if auth.get("deviceConfigToken"):
        headers["X-DFE-Device-Config-Token"] = auth["deviceConfigToken"]
    if device_info.get("mccMnc"):
        headers["X-DFE-MCCMNC"] = device_info["mccMnc"]

    return headers


def __getattr__(name: str):
    # _CONFIG_DIR used to be an import-time constant; keep it readable.
    if name == "_CONFIG_DIR":
        return get_config_dir()
    if name in ("Console", "console"):
        import warnings

        warnings.warn(
            f"goopdl.auth.{name} is deprecated; use rich.console.Console directly.",
            DeprecationWarning,
            stacklevel=2,
        )
        from rich.console import Console

        if name == "Console":
            return Console
        # Compatibility only: library internals never print through this console.
        if "_compat_console" not in globals():
            globals()["_compat_console"] = Console()
        return globals()["_compat_console"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
