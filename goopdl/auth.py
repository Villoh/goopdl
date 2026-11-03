"""Token dispenser authentication and Google Play header construction.

Tokens are cached as 0600 JSON files (auth-*, token-pool-*, pool-index-*)
in a 0700 directory resolved by :func:`get_config_dir`: ``$GOOPDL_CONFIG_DIR``,
else ``$XDG_CONFIG_HOME/goopdl``, else ``~/.config/goopdl``.

Note: older releases always used ``~/.config/goopdl``. If XDG_CONFIG_HOME
points elsewhere, existing cached tokens are not found there and are minted
again; move the files or set GOOPDL_CONFIG_DIR=~/.config/goopdl to keep them.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import random
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urlparse, urlunparse

import httpx

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

def get_config_dir() -> Path:
    """Resolve the credential cache directory on use, never at import time.

    Order: GOOPDL_CONFIG_DIR, $XDG_CONFIG_HOME/goopdl, ~/.config/goopdl.
    GOOPDL_CONFIG_DIR is ``~``-expanded; like XDG_CONFIG_HOME, a relative
    value is ignored (with a warning) so it never lands in the CWD.
    """
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


def __getattr__(name: str):
    # _CONFIG_DIR used to be an import-time constant; keep it readable.
    if name == "_CONFIG_DIR":
        return get_config_dir()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    """Remove cached credentials and indexes, preserving stable lock sidecars."""
    for pattern in ("auth-*.json", "token-pool-*.json", "pool-index-*.json"):
        for f in get_config_dir().glob(pattern):
            f.unlink(missing_ok=True)


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


# Dispenser errors subclass DirectAuthError, the token-acquisition failure
# base: one ``except DirectAuthError`` covers fetch_token(raise_on_error=True).
# They cannot subclass api.PlayAPIError (api imports auth); api.download_app
# translates them into PlayAPIError/RateLimitedError for its callers.
class DispenserRateLimitedError(DirectAuthError):
    """The Aurora token dispenser answered HTTP 429."""

    status = 429

    def __init__(self, retry_after: Optional[float] = None):
        super().__init__("Token dispenser rate limited token requests (HTTP 429).")
        self.retry_after = retry_after


class DispenserUnavailableError(DirectAuthError):
    """No dispenser attempt yielded a token; *status* is the last HTTP error."""

    def __init__(self, status: Optional[int] = None):
        super().__init__(
            f"Token dispenser unavailable (HTTP {status})."
            if status
            else "Token dispenser unavailable."
        )
        self.status = status


def _direct_credentials() -> Optional[tuple[str, str]]:
    email = os.environ.get("GOOPDL_ACCOUNT_EMAIL", "").strip()
    aas_token = os.environ.get("GOOPDL_AAS_TOKEN", "").strip()
    if not email and not aas_token:
        return None
    if not email:
        raise DirectAuthConfigurationError(
            "Missing required environment variable: GOOPDL_ACCOUNT_EMAIL"
        )
    if not aas_token:
        raise DirectAuthConfigurationError(
            "Missing required environment variable: GOOPDL_AAS_TOKEN"
        )
    if not aas_token.startswith("aas_et/"):
        raise DirectAuthConfigurationError(
            "GOOPDL_AAS_TOKEN must start with aas_et/ (not g.a or oauth2_4/)"
        )
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
    if profile_name:
        selected = find_profile(profile_name, arch)
        if not selected:
            return None
    else:
        profiles = get_priority_profiles(arch) or [("fallback", FALLBACK_PROFILE)]
        selected = profiles[0]
    profile = patch_profile_country(selected[1], country) if country else selected[1]
    locale = (
        _COUNTRY_LOCALE.get(country.upper(), f"en_{country.upper()}")
        if country
        else "en_US"
    )
    auth_user_agent = _google_auth_user_agent(profile)
    user_agent = _finsky_user_agent(profile)
    device_config = _device_configuration(profile)
    httpx_proxy = _build_httpx_proxy(proxy)

    checkin = httpx.post(
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
        raise DirectAuthError(f"checkin (HTTP {checkin.status_code})")
    try:
        android_id = _field(checkin.content, 7)
        consistency_token = _response_string(checkin.content, 12)
    except (EOFError, UnicodeDecodeError, ValueError) as exc:
        raise DirectAuthError("checkin response") from exc
    if not isinstance(android_id, int):
        raise DirectAuthError("checkin response")
    gsf_id = format(android_id, "x")

    partial = {
        "authToken": "",
        "gsfId": gsf_id,
        "deviceCheckInConsistencyToken": consistency_token,
        "deviceConfigToken": "",
        "dfeCookie": "",
        "deviceInfoProvider": {
            "userAgentString": user_agent,
            "mccMnc": profile.get("SimOperator", ""),
        },
    }
    upload_headers = build_headers(partial, country=country)
    upload_headers.pop("Authorization")
    upload_headers["Content-Type"] = "application/x-protobuf"
    upload = httpx.post(
        "https://android.clients.google.com/fdfe/uploadDeviceConfig",
        content=_proto_bytes(1, device_config),
        headers=upload_headers,
        timeout=30,
        proxy=httpx_proxy,
    )
    if upload.status_code != 200:
        raise DirectAuthError(f"device config upload (HTTP {upload.status_code})")
    try:
        config_token = _response_string(upload.content, 1, 28, 1)
    except (EOFError, UnicodeDecodeError, ValueError) as exc:
        raise DirectAuthError("device config response") from exc

    auth_response = httpx.post(
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
        safe_reason = reason if reason.isidentifier() else ""
        detail = f", {safe_reason}" if safe_reason else ""
        raise DirectAuthError(
            f"token exchange (HTTP {auth_response.status_code}{detail})"
        )
    bearer = auth_values.get("Auth")
    if not bearer:
        raise DirectAuthError("token exchange response")

    bundle = {
        **partial,
        "authToken": bearer,
        "deviceConfigToken": config_token,
    }
    toc_headers = build_headers(bundle, country=country)
    toc = httpx.get(
        "https://android.clients.google.com/fdfe/toc",
        headers=toc_headers,
        timeout=30,
        proxy=httpx_proxy,
    )
    if toc.status_code == 200:
        try:
            bundle["dfeCookie"] = _response_string(toc.content, 1, 6, 22)
        except (EOFError, UnicodeDecodeError, ValueError):
            pass
    return bundle


def fetch_token(
    dispenser_url: Optional[str] = None,
    arch: str = "arm64",
    proxy: Optional[str] = None,
    country: Optional[str] = None,
    profile: Optional[str] = None,
    raise_on_error: bool = False,
) -> Optional[dict]:
    """Obtain direct Google auth when configured, otherwise use the dispenser.

    Anonymous mode rotates through device profiles until one yields an authToken.
    When *country* is set, patches each profile's CellOperator/SimOperator
    with the matching MCC/MNC so the GSF registration is tied to that region.
    Returns the full auth dict on success, None on failure. With
    *raise_on_error*, failures raise instead: DispenserRateLimitedError (429),
    DispenserUnavailableError (every attempt failed) or DirectAuthError (direct
    mode). An unknown *profile* still returns None.
    """
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
            error = DirectAuthError(str(exc))  # stage only; messages are safe
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
    headers = {
        "User-Agent": "com.aurora.store-4.6.1-70",
        "Content-Type": "application/json",
    }

    if profile:
        match = find_profile(profile, arch)
        if not match:
            logger.error("Profile not found: %s", profile)
            return None
        profiles = [match]
    else:
        profiles = get_priority_profiles(arch) or [("fallback", FALLBACK_PROFILE)]

    # ponytail: random start so single-profile callers don't always land on
    # the same region (index 0) and pile all their requests on one exit IP
    region_start = random.randrange(len(DISPENSER_PROXY_REGIONS))
    last_status: Optional[int] = None
    for idx, (profile_name, profile_data) in enumerate(profiles):
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
            resp = httpx.post(
                url, json=payload, headers=headers, timeout=30, proxy=httpx_proxy
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("authToken"):
                    logger.info("Authenticated with profile: %s", device)
                    data["_device_profile"] = device
                    return data
                logger.warning("No authToken in dispenser response for %s", device)
            else:
                logger.warning(
                    "Dispenser returned HTTP %s for %s", resp.status_code, device
                )
                last_status = resp.status_code
                if resp.status_code == 429:
                    if raise_on_error:
                        from goopdl.api import _parse_retry_after  # api imports auth

                        raise DispenserRateLimitedError(
                            _parse_retry_after(resp.headers)
                        )
                    return None
        except DispenserRateLimitedError:
            raise
        except Exception as exc:
            logger.warning(
                "Dispenser request failed for %s: %s", device, type(exc).__name__
            )
            continue

    if raise_on_error:
        raise DispenserUnavailableError(last_status)
    return None


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
    *, blocking: bool = True,
) -> Iterator[bool]:
    """Local, per-pool transaction lock. Never unlink/replace the stable sidecar.

    Independent open handles serialize threads as well as processes. Closing the
    handle releases the native lock, including on exceptions or process death.
    """
    path = _pool_path(arch, country, profile).with_suffix(".lock")
    handle = None
    acquired = False
    try:
        _ensure_config_dir(path.parent)
        handle = path.open("a+b", buffering=0)
        if os.name == "nt":
            import msvcrt
        else:
            import fcntl
        deadline = time.monotonic() + _POOL_LOCK_TIMEOUT
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
                time.sleep(min(0.05, remaining))
        # Windows permits locking beyond EOF. Initialize only AFTER acquisition,
        # otherwise a simultaneous first opener could write another owner's byte.
        if acquired and os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"\0")
    except OSError as exc:
        if handle is not None:
            handle.close()
        _pool_cache_error(exc)
        yield False
        return
    except BaseException:
        if handle is not None:
            handle.close()
        raise
    if not acquired:
        handle.close()
        yield False
        return
    try:
        yield True
    finally:
        handle.close()


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
) -> list[dict]:
    """Ensure the arch+country+profile pool has exactly *size* valid tokens.

    Only hits the Aurora dispenser for the deficit — if the pool already has
    *size* unexpired tokens, no network call is made at all.
    Returns the full list of valid tokens (may be fewer than *size* if the
    dispenser is rate-limiting).
    """
    if direct_auth_enabled():
        tokens = []
        for _ in range(size):
            token = fetch_token(
                arch=arch, country=country, proxy=proxy, profile=profile
            )
            if token is None:
                break
            tokens.append(token)
        return tokens

    with _pool_lock(arch, country, profile) as acquired:
        if acquired:
            try:
                return _ensure_pool_unlocked(
                    arch, country, proxy, dispenser_url, profile, size
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
                )
            except DirectAuthError:
                if pool:
                    break  # a partial pool still serves the caller
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
    *raise_on_error* raises fetch_token's typed errors when no token is left.
    """
    if direct_auth_enabled():
        return fetch_token(
            arch=arch, country=country, proxy=proxy, profile=profile,
            raise_on_error=raise_on_error,
        )

    with _pool_lock(arch, country, profile) as acquired:
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
) -> Optional[dict]:
    """Remove *failed_token* from the pool and replace it with a fresh one.

    Called when a mid-request AuthExpiredError reveals a token died early.
    Reuses a current member if another caller already removed that credential.
    Returns None if replacement or cache access fails.
    """
    if direct_auth_enabled():
        return fetch_token(arch=arch, country=country, proxy=proxy, profile=profile)

    with _pool_lock(arch, country, profile) as acquired:
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
                new_token = fetch_token(
                    dispenser_url=dispenser_url, arch=arch, proxy=proxy,
                    profile=profile, country=country,
                )
                if new_token:
                    new_token["_cached_at"] = time.time()
                    survivors.append(new_token)
                    _save_pool(survivors, arch, country, profile)
                return new_token
            except OSError as exc:
                _pool_cache_error(exc)
    return None


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
        return fetch_token(arch=arch, country=country, proxy=proxy, profile=profile)

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
