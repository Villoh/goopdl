"""Token dispenser authentication and Google Play header construction."""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

import httpx
from rich.console import Console

from gplaydl.profiles import (
    FALLBACK_PROFILE,
    find_profile,
    get_priority_profiles,
    patch_profile_country,
)
from gplaydl.protobuf import WIRETYPE_LENGTH_DELIMITED, ProtoDecoder

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

_CONFIG_DIR = Path.home() / ".config" / "gplaydl"

console = Console(stderr=True)


def _sanitize_country(country: str) -> str:
    """Strip everything except uppercase A-Z and digits — prevents path traversal."""
    return re.sub(r"[^A-Z0-9]", "", country.upper())[:4]


def _auth_path(arch: str, country: Optional[str] = None) -> Path:
    suffix = f"-{_sanitize_country(country)}" if country else ""
    return _CONFIG_DIR / f"auth-{arch}{suffix}.json"


def save_auth(data: dict, arch: str = "arm64", country: Optional[str] = None) -> Path:
    """Persist auth data to disk and return the file path."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data["_cached_at"] = time.time()
    path = _auth_path(arch, country)
    path.write_text(json.dumps(data, indent=2))
    return path


def load_cached_auth(
    arch: str = "arm64", country: Optional[str] = None
) -> Optional[dict]:
    """Return cached auth dict or None."""
    path = _auth_path(arch, country)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def clear_auth() -> None:
    """Remove all cached auth files."""
    if _CONFIG_DIR.exists():
        for f in _CONFIG_DIR.glob("auth-*.json"):
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


class DirectAuthConfigurationError(ValueError):
    """Direct Google authentication environment is only partially configured."""


def _direct_credentials() -> Optional[tuple[str, str]]:
    email = os.environ.get("GPLAYDL_ACCOUNT_EMAIL", "").strip()
    aas_token = os.environ.get("GPLAYDL_AAS_TOKEN", "").strip()
    if not email and not aas_token:
        return None
    if not email:
        raise DirectAuthConfigurationError(
            "Missing required environment variable: GPLAYDL_ACCOUNT_EMAIL"
        )
    if not aas_token:
        raise DirectAuthConfigurationError(
            "Missing required environment variable: GPLAYDL_AAS_TOKEN"
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
        return None
    android_id = _field(checkin.content, 7)
    consistency_token = _response_string(checkin.content, 12)
    if not isinstance(android_id, int):
        return None
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
        return None
    config_token = _response_string(upload.content, 1, 28, 1)

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
    if auth_response.status_code != 200:
        return None
    auth_values = dict(
        line.split("=", 1) for line in auth_response.text.splitlines() if "=" in line
    )
    bearer = auth_values.get("Auth")
    if not bearer:
        return None

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
    if toc.status_code != 200:
        return None
    bundle["dfeCookie"] = _response_string(toc.content, 1, 6, 22)
    return bundle


def fetch_token(
    dispenser_url: Optional[str] = None,
    arch: str = "arm64",
    proxy: Optional[str] = None,
    country: Optional[str] = None,
    profile: Optional[str] = None,
) -> Optional[dict]:
    """Obtain direct Google auth when configured, otherwise use the dispenser.

    Anonymous mode rotates through device profiles until one yields an authToken.
    When *country* is set, patches each profile's CellOperator/SimOperator
    with the matching MCC/MNC so the GSF registration is tied to that region.
    Returns the full auth dict on success, None on failure.
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
        except Exception:
            console.print("  [red]Direct Google authentication failed.[/red]")
            return None

    url = dispenser_url or DEFAULT_DISPENSER_URL
    headers = {
        "User-Agent": "com.aurora.store-4.6.1-70",
        "Content-Type": "application/json",
    }

    if profile:
        match = find_profile(profile, arch)
        if not match:
            console.print(f"[red]Profile not found: {profile}[/red]")
            return None
        profiles = [match]
    else:
        profiles = get_priority_profiles(arch) or [("fallback", FALLBACK_PROFILE)]

    # ponytail: random start so single-profile callers don't always land on
    # the same region (index 0) and pile all their requests on one exit IP
    region_start = random.randrange(len(DISPENSER_PROXY_REGIONS))
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
                    console.print(
                        f"  Authenticated with profile: [bold]{device}[/bold]"
                    )
                    data["_device_profile"] = device
                    return data
                console.print(
                    f"  [yellow]No authToken in dispenser response for {device}[/yellow]"
                )
            else:
                console.print(
                    f"  [yellow]Dispenser returned HTTP {resp.status_code} for {device}[/yellow]"
                )
        except Exception as exc:
            console.print(
                f"  [red]Dispenser request failed for {device}: "
                f"{type(exc).__name__}[/red]"
            )
            continue

    return None


_MAX_TOKEN_AGE = 50 * 60  # 50 minutes — refresh before the ~1h Google expiry

# ── token pool ────────────────────────────────────────────────────────────────

# Number of GSF ID / token pairs maintained per region. Enough to detect
# staged-rollout version differences while staying well under Aurora's ~20
# requests/hour rate limit.
DEFAULT_PROBES = 5


def _pool_path(arch: str, country: Optional[str]) -> Path:
    suffix = f"-{_sanitize_country(country)}" if country else ""
    return _CONFIG_DIR / f"token-pool-{arch}{suffix}.json"


def _load_pool(arch: str, country: Optional[str]) -> list[dict]:
    """Return unexpired tokens from the on-disk pool for this arch+country."""
    try:
        tokens: list[dict] = json.loads(_pool_path(arch, country).read_text())
    except (OSError, json.JSONDecodeError):
        return []
    now = time.time()
    return [t for t in tokens if now - t.get("_cached_at", 0) < _MAX_TOKEN_AGE]


def _save_pool(tokens: list[dict], arch: str, country: Optional[str]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _pool_path(arch, country).write_text(json.dumps(tokens))


def ensure_pool(
    arch: str = "arm64",
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    dispenser_url: Optional[str] = None,
    profile: Optional[str] = None,
    size: int = DEFAULT_PROBES,
) -> list[dict]:
    """Ensure the regional pool has exactly *size* valid tokens.

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

    pool = _load_pool(arch, country)
    deficit = size - len(pool)
    if deficit > 0:
        label = country or "default"
        console.print(
            f"[dim]  Pool [{label}]: {len(pool)}/{size} valid — fetching {deficit} more...[/dim]"
        )
        for _ in range(deficit):
            t = fetch_token(
                dispenser_url=dispenser_url,
                arch=arch,
                proxy=proxy,
                profile=profile,
                country=country,
            )
            if t is None:
                break
            t.setdefault("_cached_at", time.time())
            pool.append(t)
        _save_pool(pool, arch, country)
    return pool


def _index_path(arch: str, country: Optional[str]) -> Path:
    suffix = f"-{_sanitize_country(country)}" if country else ""
    return _CONFIG_DIR / f"pool-index-{arch}{suffix}.json"


def _read_index(arch: str, country: Optional[str]) -> int:
    try:
        return json.loads(_index_path(arch, country).read_text()).get("i", 0)
    except (OSError, json.JSONDecodeError):
        return 0


def _write_index(i: int, arch: str, country: Optional[str]) -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _index_path(arch, country).write_text(json.dumps({"i": i}))


def pick_pool_token(
    arch: str = "arm64",
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    dispenser_url: Optional[str] = None,
    profile: Optional[str] = None,
    size: int = DEFAULT_PROBES,
) -> Optional[dict]:
    """Ensure the pool is full, then return the next token in round-robin order.

    Expired tokens are pruned and refetched by ensure_pool before picking.
    The round-robin index is persisted to disk so it survives across invocations.
    """
    if direct_auth_enabled():
        return fetch_token(arch=arch, country=country, proxy=proxy, profile=profile)

    pool = ensure_pool(
        arch=arch,
        country=country,
        proxy=proxy,
        dispenser_url=dispenser_url,
        profile=profile,
        size=size,
    )
    if not pool:
        return None
    i = _read_index(arch, country)
    token = pool[i % len(pool)]
    _write_index(i + 1, arch, country)
    return token


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
    Returns the new token, or None if the dispenser is unavailable.
    """
    if direct_auth_enabled():
        return fetch_token(arch=arch, country=country, proxy=proxy, profile=profile)

    pool = _load_pool(arch, country)
    failed_gsf = failed_token.get("gsfId")
    pool = [t for t in pool if t.get("gsfId") != failed_gsf]
    new_token = fetch_token(
        dispenser_url=dispenser_url,
        arch=arch,
        proxy=proxy,
        profile=profile,
        country=country,
    )
    if new_token:
        new_token["_cached_at"] = time.time()
        pool.append(new_token)
    _save_pool(pool, arch, country)
    return new_token


def ensure_auth(
    arch: str = "arm64",
    dispenser_url: Optional[str] = None,
    force_refresh: bool = False,
    proxy: Optional[str] = None,
    country: Optional[str] = None,
    profile: Optional[str] = None,
) -> Optional[dict]:
    """Return cached auth or fetch a new token transparently.

    Each country gets its own cache file so tokens stay region-bound.
    Proactively refreshes tokens older than 50 minutes.
    Pass *force_refresh=True* to ignore cache entirely (e.g. after a 401).
    """
    if direct_auth_enabled():
        return fetch_token(arch=arch, country=country, proxy=proxy, profile=profile)

    if not force_refresh:
        cached = load_cached_auth(arch, country)
        if cached and cached.get("authToken"):
            age = time.time() - cached.get("_cached_at", 0)
            if age < _MAX_TOKEN_AGE:
                return cached
            console.print("[dim]Token expired — refreshing...[/dim]")
    else:
        console.print("[dim]Refreshing token...[/dim]")

    data = fetch_token(
        dispenser_url=dispenser_url,
        arch=arch,
        proxy=proxy,
        country=country,
        profile=profile,
    )
    if data:
        save_auth(data, arch, country)
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
