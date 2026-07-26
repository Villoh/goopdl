"""Token dispenser authentication and Google Play header construction."""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from urllib.parse import urlparse, urlunparse
from rich.console import Console

from gplaydl.profiles import (
    FALLBACK_PROFILE,
    find_profile,
    get_priority_profiles,
    patch_profile_country,
)

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
                new_password = password[:idx] + region.lower() + password[idx + len(lower_code):]
                new_netloc = parsed.netloc.replace(parsed.password, new_password, 1)
                return urlunparse(parsed._replace(netloc=new_netloc))
    return proxy


def fetch_token(
    dispenser_url: Optional[str] = None,
    arch: str = "arm64",
    proxy: Optional[str] = None,
    country: Optional[str] = None,
    profile: Optional[str] = None,
) -> Optional[dict]:
    """Obtain an anonymous auth token from the dispenser.

    Rotates through device profiles until one yields an authToken.
    When *country* is set, patches each profile's CellOperator/SimOperator
    with the matching MCC/MNC so the GSF registration is tied to that region.
    Returns the full auth dict on success, None on failure.
    """
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
    for idx, (profile_name, profile) in enumerate(profiles):
        device = profile.get("UserReadableName", profile_name)
        payload = patch_profile_country(profile, country) if country else profile
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
