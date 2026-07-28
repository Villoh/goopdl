"""Google Play Store FDFE API — details, purchase, delivery, search.

Uses a pure-Python protobuf decoder (no gpapi / protobuf library needed).
Field numbers are based on live probing from our research repo and
validated against gpapi's googleplay.proto definitions.
"""

from __future__ import annotations

import random
import re
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from gpdl.auth import (
    build_headers,
    _build_httpx_proxy,
    direct_auth_enabled,
    pick_pool_token,
    replace_pool_token,
)
from gpdl.download import DownloadSpec, download_batch
from gpdl.profiles import get_latest_probe_profiles
from gpdl.protobuf import ProtoDecoder, extract_strings, proto_to_dict

SEARCH_URL = f"https://android.clients.google.com/fdfe/search"

FDFE_URL = "https://android.clients.google.com/fdfe"
DETAILS_URL = f"{FDFE_URL}/details"
PURCHASE_URL = f"{FDFE_URL}/purchase"
DELIVERY_URL = f"{FDFE_URL}/delivery"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AppDetails:
    package: str
    title: str = ""
    developer: str = ""
    version_string: str = ""
    version_code: int = 0
    rating: Optional[str] = None
    downloads: Optional[str] = None
    play_url: str = ""


@dataclass
class SplitInfo:
    name: str
    url: str = ""
    size: int = 0
    sha1: str = ""
    sha256: str = ""


@dataclass
class AdditionalFile:
    file_type: int = 0  # 0 = main OBB, 1 = patch OBB, 2 = asset pack APK
    version_code: int = 0
    size: int = 0
    url: str = ""
    gzipped: bool = False
    cookies: list[dict] = field(default_factory=list)

    @property
    def is_asset_pack(self) -> bool:
        return self.file_type == 2

    @property
    def extension(self) -> str:
        return ".apk" if self.is_asset_pack else ".obb"

    @property
    def type_label(self) -> str:
        return {0: "main", 1: "patch", 2: "asset"}.get(self.file_type, "main")


@dataclass
class DeliveryResult:
    version_code: int = 0
    download_url: str = ""
    download_size: int = 0
    sha1: str = ""
    sha256: str = ""
    cookies: list[dict] = field(default_factory=list)
    splits: list[SplitInfo] = field(default_factory=list)
    additional_files: list[AdditionalFile] = field(default_factory=list)


class PlayAPIError(Exception):
    pass


class AuthExpiredError(PlayAPIError):
    """Raised when the API returns 401 — token needs refresh."""

    pass


class VersionUnavailableError(PlayAPIError):
    """Raised when Google Play cannot serve requested version code."""

    pass


# ---------------------------------------------------------------------------
# Protobuf helpers
# ---------------------------------------------------------------------------


def _first_bytes(fields: list[tuple[int, int, Any]], num: int) -> Optional[bytes]:
    """Return raw bytes of the first length-delimited field with number *num*."""
    for fn, wt, v in fields:
        if fn == num and wt == 2 and isinstance(v, (bytes, bytearray)):
            return bytes(v)
    return None


def _first_string(fields: list[tuple[int, int, Any]], num: int) -> str:
    for fn, wt, v in fields:
        if fn == num and wt == 2:
            return ProtoDecoder.decode_string(v)
    return ""


def _first_ascii(fields: list[tuple[int, int, Any]], num: int) -> str:
    value = _first_bytes(fields, num)
    if value is None:
        return ""
    try:
        return value.decode("ascii")
    except UnicodeDecodeError as error:
        raise PlayAPIError(f"delivery field {num} is not ASCII") from error


def _first_int(fields: list[tuple[int, int, Any]], num: int) -> Optional[int]:
    for fn, wt, v in fields:
        if fn == num and wt == 0:
            return int(v)
    return None


def _all_bytes(fields: list[tuple[int, int, Any]], num: int) -> list[bytes]:
    """Return all length-delimited occurrences of field *num*."""
    return [
        bytes(v)
        for fn, wt, v in fields
        if fn == num and wt == 2 and isinstance(v, (bytes, bytearray))
    ]


def _navigate(raw: bytes, *path: int) -> list[tuple[int, int, Any]]:
    """Walk a nested protobuf path, e.g. _navigate(raw, 1, 2, 4)."""
    data = raw
    for field_num in path:
        fields = ProtoDecoder(data).read_all_ordered()
        sub = _first_bytes(fields, field_num)
        if sub is None:
            return []
        data = sub
    return ProtoDecoder(data).read_all_ordered()


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def _proto_headers(auth: dict, country: Optional[str] = None) -> dict:
    headers = build_headers(auth, country=country)
    headers["Content-Type"] = "application/x-protobuf"
    headers["Accept"] = "application/x-protobuf"
    return headers


# ---------------------------------------------------------------------------
# Details
# ---------------------------------------------------------------------------
# ResponseWrapper(1) -> Payload(2) -> DetailsResponse(4) -> DocV2
# DocV2: 1=docid, 5=title, 6=creator, 13=DocDetails
# DocDetails(1) -> AppDetails: 3=versionCode, 4=versionString


def _first_float(fields: list[tuple[int, int, Any]], num: int) -> Optional[float]:
    for fn, wt, v in fields:
        if fn == num and wt == 5:
            return struct.unpack("<f", struct.pack("<I", v))[0]
    return None


@dataclass
class _ParsedDetails:
    docid: str = ""
    title: str = ""
    creator: str = ""
    version_code: int = 0
    version_string: str = ""
    rating: Optional[str] = None
    downloads: Optional[str] = None


def _parse_details_proto(raw: bytes) -> _ParsedDetails:
    """Parse details protobuf response into structured fields."""
    result = _ParsedDetails()
    doc_fields = _navigate(raw, 1, 2, 4)
    if not doc_fields:
        return result

    result.docid = _first_string(doc_fields, 1)
    result.title = _first_string(doc_fields, 5)
    result.creator = _first_string(doc_fields, 6)

    # AggregateRating (DocV2 field 14): sub 17 = display string, sub 2 = float
    rating_b = _first_bytes(doc_fields, 14)
    if rating_b:
        rf = ProtoDecoder(rating_b).read_all_ordered()
        result.rating = _first_string(rf, 17) or None
        if not result.rating:
            star = _first_float(rf, 2)
            if star and star > 0:
                result.rating = f"{star:.1f}"

    # DocDetails(13) -> AppDetails(1)
    doc_details_b = _first_bytes(doc_fields, 13)
    if doc_details_b:
        dd = ProtoDecoder(doc_details_b).read_all_ordered()
        app_details_b = _first_bytes(dd, 1)
        if app_details_b:
            ad = ProtoDecoder(app_details_b).read_all_ordered()
            result.version_code = _first_int(ad, 3) or 0
            result.version_string = _first_string(ad, 4)
            # Downloads: field 61 = short display (e.g. "10B+")
            result.downloads = _first_string(ad, 61) or None

    return result


def _fetch_details_raw(
    package: str,
    auth: dict,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
) -> bytes:
    """Fetch raw protobuf details response."""
    headers = _proto_headers(auth, country=country)
    url = f"{DETAILS_URL}?doc={package}"
    if country:
        url += f"&gl={country.upper()}"
    resp = httpx.get(url, headers=headers, timeout=30, proxy=_build_httpx_proxy(proxy))
    if resp.status_code == 404:
        raise PlayAPIError(f"App not found: {package}")
    if resp.status_code == 401:
        raise AuthExpiredError("Auth token expired.")
    if resp.status_code != 200:
        raise PlayAPIError(f"Failed to fetch details (HTTP {resp.status_code}).")
    return resp.content


def get_details_raw(
    package: str,
    auth: dict,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
) -> dict:
    """Return the full protobuf response decoded as a nested dict."""
    raw = _fetch_details_raw(
        package, auth, country=country, proxy=_build_httpx_proxy(proxy)
    )
    return proto_to_dict(raw)


def fetch_app_item(
    package: str,
    arch: str = "arm64",
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    profile: Optional[str] = None,
    dispenser_url: Optional[str] = None,
) -> dict:
    """Fetch, parse and return a PlayStoreAppItem dict in one call.

    Handles token pool management and expired-token retry internally.

    Example::

        from gpdl.api import fetch_app_item
        item = fetch_app_item("com.whatsapp", country="US", proxy="http://user:pass@host:port/")
    """
    auth = pick_pool_token(
        arch=arch,
        country=country,
        proxy=proxy,
        dispenser_url=dispenser_url,
        profile=profile,
    )
    if not auth:
        raise PlayAPIError("Could not obtain auth token — run: gpdl auth")
    try:
        raw = get_details_raw(package, auth, country=country, proxy=proxy)
    except AuthExpiredError:
        replace_pool_token(
            auth, arch=arch, country=country, proxy=proxy, dispenser_url=dispenser_url
        )
        auth = pick_pool_token(
            arch=arch,
            country=country,
            proxy=proxy,
            dispenser_url=dispenser_url,
            profile=profile,
        )
        if not auth:
            raise PlayAPIError("Token expired and replacement failed.")
        raw = get_details_raw(package, auth, country=country, proxy=proxy)
    return parse_app_item(raw, region=country or "")


def parse_app_item(raw: dict, region: str = "") -> dict:
    """Map proto_to_dict output to PlayStoreAppItem-compatible dict.

    Path reference (field numbers from --raw output):
      doc  = raw[1][2][4]          DocV2
      ad   = doc[13][1]            AppDetails
      rtg  = doc[14]               AggregateRating
      imgs = doc[10]               image list (type 1=screenshot, 2=feature, 4=icon)
      cats = doc[66][9][1]         category list
    """

    def _g(d: Any, *keys: Any) -> Any:
        for k in keys:
            if not isinstance(d, dict):
                return None
            d = d.get(str(k))
        return d

    try:
        doc = raw["1"]["2"]["4"]
    except (KeyError, TypeError):
        return {}

    ad: dict = _g(doc, 13, 1) or {}
    rtg: dict = doc.get("14") or {}
    imgs = doc.get("10") or []
    if not isinstance(imgs, list):
        imgs = [imgs]

    pkg = doc.get("1") or doc.get("2", "")

    icon_url = next(
        (i["5"] for i in imgs if isinstance(i, dict) and i.get("1") == 4 and "5" in i),
        None,
    )
    feature_gfx_url = next(
        (i["5"] for i in imgs if isinstance(i, dict) and i.get("1") == 2 and "5" in i),
        None,
    )
    screenshots = [
        i["5"] for i in imgs if isinstance(i, dict) and i.get("1") == 1 and "5" in i
    ]

    cats = _g(ad, 66, 9, 1) or []
    if not isinstance(cats, list):
        cats = [cats]
    cat = cats[0] if cats and isinstance(cats[0], dict) else {}

    def _parse_date(s: Any) -> Optional[str]:
        if not isinstance(s, str):
            return None
        try:
            return (
                datetime.strptime(s, "%b %d, %Y")
                .replace(tzinfo=timezone.utc)
                .isoformat()
            )
        except ValueError:
            return s

    def _rating_float(s: Any) -> Optional[float]:
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    # AggregateRating histogram: fields 3–7 are 1★–5★ counts (gpapi proto convention)
    histogram = {str(star): rtg.get(str(star + 2)) for star in range(1, 6)}

    rating_str = rtg.get("17")  # display string e.g. "4.5"

    # field 70 = precise real-time install count; field 53 = rounded bucket floor
    exact_downloads = ad.get("70") or ad.get("53")
    # field 13 = "10,000,000,000+ downloads" → strip suffix for bucket
    bucket_raw: str = ad.get("13") or ad.get("61") or ""
    downloads_bucket = (
        bucket_raw.replace(" downloads", "").replace(" installs", "").strip()
    )

    return {
        "id": f"{pkg}_playstore_{region.lower()}" if region else f"{pkg}_playstore",
        "package_name": pkg,
        "app_id": doc.get("2", pkg),
        "store": "playstore",
        "region": region.upper() or None,
        # display
        "title": doc.get("5"),
        "description": doc.get("7"),
        "developer": doc.get("6"),
        "dev_id": ad.get("1"),
        "icon_url": icon_url,
        "feature_graphic_url": feature_gfx_url,
        "screenshots": screenshots,
        "url": doc.get("17"),
        # metrics
        "rating": _rating_float(rating_str),
        "rating_count": rtg.get("2"),
        "rating_display": rating_str,
        "rating_histogram": histogram,
        "total_downloads": exact_downloads,
        "downloads_bucket": downloads_bucket,
        # app details
        "version": ad.get("4"),
        "version_sourced": False,
        "updated_on": _parse_date(ad.get("16")),
        "released_on": _parse_date(_g(ad, 64, 1)),
        "min_os_version": _g(ad, 82, 1, 1),
        "in_app_purchases": ad.get("67") or "",
        "content_rating": _g(doc, 50, 1) or "",
        "category_name": cat.get("1") or "",
        "category_code": cat.get("3") or "",
        # rich metadata
        "permissions": ad.get("10") or [],
    }


def get_details(
    package: str,
    auth: dict,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
) -> AppDetails:
    """Return structured app details."""
    parsed = _parse_details_proto(
        _fetch_details_raw(
            package, auth, country=country, proxy=_build_httpx_proxy(proxy)
        )
    )
    if not parsed.docid:
        raise PlayAPIError("App not found or unavailable for this device profile.")
    return AppDetails(
        package=parsed.docid or package,
        title=parsed.title,
        developer=parsed.creator,
        version_string=parsed.version_string,
        version_code=parsed.version_code,
        rating=parsed.rating,
        downloads=parsed.downloads,
        play_url=f"https://play.google.com/store/apps/details?id={package}",
    )


# ---------------------------------------------------------------------------
# Purchase
# ---------------------------------------------------------------------------


def purchase(
    package: str,
    version_code: int,
    auth: dict,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
) -> None:
    """Acquire a free app (equivalent of clicking 'Install')."""
    headers = build_headers(auth, country=country)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    body = f"doc={package}&ot=1&vc={version_code}"
    resp = httpx.post(
        PURCHASE_URL,
        headers=headers,
        content=body,
        timeout=30,
        proxy=_build_httpx_proxy(proxy),
    )
    if resp.status_code not in (200, 204):
        pass  # non-fatal — may already be "purchased"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------
# ResponseWrapper(1) -> Payload(21) -> DeliveryResponse(2) -> AppDeliveryData
# AppDeliveryData (field numbers from live probing):
#   1  = downloadSize         (varint, bytes)
#   2  = signature            (string)
#   3  = downloadUrl          (string)
#   4  = downloadAuthCookie   (repeated message: 1=name, 2=value)
#   15 = splitDeliveryData    (repeated message: 1=name, 2=size, 5=downloadUrl)
#   18 = additionalFile       (repeated message: 1=fileType, 2=size, 3=downloadUrl)
#   29 = versionCode          (varint)

_GOOGLE_CDN_SUFFIXES = (
    ".google.com",
    ".googleapis.com",
    ".ggpht.com",
    ".googleusercontent.com",
)


def _safe_cdn_url(url: str) -> str:
    """Return url only if it is HTTPS and hosted on a known Google domain, else ''."""
    if not url.startswith("https://"):
        return ""
    host = urlparse(url).hostname or ""
    if host == "android.clients.google.com" or any(
        host.endswith(s) for s in _GOOGLE_CDN_SUFFIXES
    ):
        return url
    return ""


def _parse_delivery(raw: bytes) -> DeliveryResult:
    """Parse a delivery response using ProtoDecoder."""
    for payload_fn in (21, 5, 4, 6):
        add_fields = _navigate(raw, 1, payload_fn, 2)
        if add_fields and _safe_cdn_url(_first_string(add_fields, 3)):
            return _extract_delivery_from_fields(add_fields)

    return _extract_delivery_from_tree(raw)


def _extract_delivery_from_fields(fields: list[tuple[int, int, Any]]) -> DeliveryResult:
    """Build DeliveryResult from parsed AppDeliveryData fields."""
    app_vc = _first_int(fields, 29) or 0

    result = DeliveryResult(
        version_code=app_vc,
        download_url=_safe_cdn_url(_first_string(fields, 3)),
        download_size=_first_int(fields, 1) or 0,
        sha1=_first_ascii(fields, 2),
        sha256=_first_ascii(fields, 19),
    )

    # Field 4 (repeated) — contains BOTH cookies and OBB file metadata.
    # Cookies have f1=string(name), f2=string(value).
    # OBB entries have f1=varint(fileType), f2=varint(versionCode),
    # f3=varint(size), f4=string(downloadUrl), f7=string(compressedUrl).
    for f4_b in _all_bytes(fields, 4):
        cf = ProtoDecoder(f4_b).read_all_ordered()
        f1_wt = next((wt for fn, wt, _ in cf if fn == 1), None)
        if f1_wt == 2:
            name = _first_string(cf, 1)
            value = _first_string(cf, 2)
            if name:
                result.cookies.append({"name": name, "value": value})
        elif f1_wt == 0:
            url = _safe_cdn_url(_first_string(cf, 4))
            if url:
                ft = _first_int(cf, 1) or 0
                result.additional_files.append(
                    AdditionalFile(
                        file_type=ft,
                        version_code=_first_int(cf, 2) or app_vc,
                        size=_first_int(cf, 3) or 0,
                        url=url,
                        gzipped=False,
                    )
                )

    # Splits (field 15, repeated: 1=name, 2=size, 4=SHA-1, 5=URL, 9=SHA-256)
    for split_b in _all_bytes(fields, 15):
        sf = ProtoDecoder(split_b).read_all_ordered()
        name = _first_string(sf, 1)
        url = _safe_cdn_url(_first_string(sf, 5))
        if url:
            result.splits.append(
                SplitInfo(
                    name=name or f"split{len(result.splits)}",
                    url=url,
                    size=_first_int(sf, 2) or 0,
                    sha1=_first_ascii(sf, 4),
                    sha256=_first_ascii(sf, 9),
                )
            )

    # Field 18 (repeated) — asset pack APKs (fileType=2, gzip-compressed).
    for af_b in _all_bytes(fields, 18):
        af = ProtoDecoder(af_b).read_all_ordered()
        url = _safe_cdn_url(_first_string(af, 3))
        if url:
            ft = _first_int(af, 1) or 0
            result.additional_files.append(
                AdditionalFile(
                    file_type=ft,
                    size=_first_int(af, 2) or 0,
                    url=url,
                    gzipped=ft == 2,
                )
            )

    return result


def _extract_delivery_from_tree(raw: bytes) -> DeliveryResult:
    """Fallback: scan protobuf strings for a valid Google CDN download URL."""
    result = DeliveryResult()
    for s in extract_strings(raw):
        if _safe_cdn_url(s):
            result.download_url = s
            break
    return result


def get_delivery(
    package: str,
    version_code: int,
    auth: dict,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
) -> DeliveryResult:
    """Fetch download URLs for base APK, splits, and OBB files."""
    headers = _proto_headers(auth, country=country)
    url = f"{DELIVERY_URL}?doc={package}&ot=1&vc={version_code}"
    if country:
        url += f"&gl={country.upper()}"
    resp = httpx.get(url, headers=headers, timeout=30, proxy=_build_httpx_proxy(proxy))
    if resp.status_code == 401:
        raise AuthExpiredError("Auth token expired.")
    if resp.status_code == 404:
        raise VersionUnavailableError("Requested version code is unavailable.")
    if resp.status_code != 200:
        raise PlayAPIError(f"Delivery failed (HTTP {resp.status_code}).")

    result = _parse_delivery(resp.content)

    if not result.download_url:
        raise PlayAPIError(
            "No download URL returned. The app may require purchase or "
            "is unavailable for this device."
        )

    return result


# ---------------------------------------------------------------------------
# List splits (from details metadata)
# ---------------------------------------------------------------------------


def list_splits(
    package: str,
    auth: dict,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
) -> list[str]:
    """Return split names from app details metadata."""
    headers = _proto_headers(auth, country=country)
    url = f"{DETAILS_URL}?doc={package}"
    if country:
        url += f"&gl={country.upper()}"
    resp = httpx.get(url, headers=headers, timeout=30, proxy=_build_httpx_proxy(proxy))
    if resp.status_code == 401:
        raise AuthExpiredError("Auth token expired.")
    if resp.status_code != 200:
        raise PlayAPIError(f"Failed to fetch details (HTTP {resp.status_code})")

    # Navigate to AppDetails: 1 -> 2 -> 4 -> 13 -> 1
    doc_fields = _navigate(resp.content, 1, 2, 4)
    if not doc_fields:
        return []

    splits: set[str] = set()
    doc_details_b = _first_bytes(doc_fields, 13)
    if not doc_details_b:
        return []

    dd = ProtoDecoder(doc_details_b).read_all_ordered()
    app_details_b = _first_bytes(dd, 1)
    if not app_details_b:
        return []

    ad = ProtoDecoder(app_details_b).read_all_ordered()

    # file entries (field 17 in AppDetails, each with splitId at field 9)
    for file_b in _all_bytes(ad, 17):
        ff = ProtoDecoder(file_b).read_all_ordered()
        sid = _first_string(ff, 9)
        if sid:
            splits.add(sid)

    if not splits:
        split_pattern = re.compile(r"^config\.[a-z]")
        for s in extract_strings(app_details_b):
            if split_pattern.match(s):
                splits.add(s)

    return sorted(splits)


# ---------------------------------------------------------------------------
# Search (FDFE protobuf API)
# ---------------------------------------------------------------------------


def _find_docv2(data: bytes, depth: int = 0, max_depth: int = 10) -> list[dict]:
    """Recursively find DocV2 entries (docid at f1, title at f5) in protobuf."""
    results: list[dict] = []
    if depth > max_depth:
        return results
    try:
        fields = ProtoDecoder(data).read_all_ordered()
    except Exception:
        return results

    f1_str = _first_string(fields, 1)
    f5_str = _first_string(fields, 5)
    if f1_str and "." in f1_str and " " not in f1_str and f5_str:
        return [
            {"package": f1_str, "title": f5_str, "creator": _first_string(fields, 6)}
        ]

    for _fn, wt, v in fields:
        if wt == 2 and isinstance(v, (bytes, bytearray)) and len(v) > 20:
            results.extend(_find_docv2(bytes(v), depth + 1, max_depth))
    return results


def search_apps(
    query: str,
    auth: dict,
    limit: int = 10,
    country: Optional[str] = None,
    proxy: Optional[str] = None,
) -> list[dict]:
    """Search Google Play via FDFE protobuf API. Returns list of {package, title, creator}."""
    headers = _proto_headers(auth, country=country)
    url = f"{SEARCH_URL}?q={query}&c=3"
    if country:
        url += f"&gl={country.upper()}"
    resp = httpx.get(url, headers=headers, timeout=30, proxy=_build_httpx_proxy(proxy))
    if resp.status_code == 401:
        raise AuthExpiredError("Auth token expired.")
    if resp.status_code != 200:
        raise PlayAPIError(f"Search failed (HTTP {resp.status_code})")

    docs = _find_docv2(resp.content)

    seen: set[str] = set()
    results: list[dict] = []
    for doc in docs:
        pkg = doc["package"]
        if pkg not in seen and len(results) < limit:
            seen.add(pkg)
            results.append(doc)
    return results


# ---------------------------------------------------------------------------
# Lightweight download (single pooled/cached token, no multi-profile probing)
# ---------------------------------------------------------------------------

_TOKEN_FETCH_ATTEMPTS = 3
_TOKEN_FETCH_COOLDOWN = 5  # seconds — dispenser 5xx/429s are usually transient


def _acquire_single_token(
    arch: str,
    country: Optional[str],
    proxy: Optional[str],
    dispenser_url: Optional[str],
    profile: str,
) -> Optional[dict]:
    """pick_pool_token(size=1), retrying a few times if the dispenser hiccups."""
    for attempt in range(_TOKEN_FETCH_ATTEMPTS):
        auth = pick_pool_token(
            arch=arch,
            country=country,
            proxy=proxy,
            dispenser_url=dispenser_url,
            profile=profile,
            size=1,
        )
        if auth:
            return auth
        if attempt < _TOKEN_FETCH_ATTEMPTS - 1:
            time.sleep(_TOKEN_FETCH_COOLDOWN)
    return None


def _fetch_latest_delivery(
    package: str,
    auth: dict,
    country: Optional[str],
    proxy: Optional[str],
) -> tuple[AppDetails, DeliveryResult]:
    """Fetch details for whatever version *auth*'s GSF ID sees, then acquire + deliver it."""
    details = get_details(package, auth, country=country, proxy=proxy)
    purchase(package, details.version_code, auth, country=country, proxy=proxy)
    delivery = get_delivery(
        package, details.version_code, auth, country=country, proxy=proxy
    )
    return details, delivery


def _build_specs(
    package: str,
    vc: int,
    output: Path,
    delivery: DeliveryResult,
    no_splits: bool,
    no_extras: bool,
) -> list[DownloadSpec]:
    """Turn a DeliveryResult into the DownloadSpec list download_batch expects."""
    base_name = f"{package}-{vc}.apk"
    specs = [
        DownloadSpec(
            url=delivery.download_url,
            dest=output / base_name,
            cookies=delivery.cookies,
            label=base_name,
            integrity_required=True,
            expected_size=delivery.download_size,
            sha1=delivery.sha1,
            sha256=delivery.sha256,
        )
    ]
    if delivery.splits and not no_splits:
        for split in delivery.splits:
            name = f"{package}-{vc}-{split.name}.apk"
            specs.append(
                DownloadSpec(
                    url=split.url,
                    dest=output / name,
                    label=name,
                    integrity_required=True,
                    expected_size=split.size,
                    sha1=split.sha1,
                    sha256=split.sha256,
                )
            )
    if not no_extras and delivery.additional_files:
        for af in delivery.additional_files:
            name = (
                f"{package}-{vc}-{af.type_label}{af.extension}"
                if af.is_asset_pack
                else f"{af.type_label}.{af.version_code}.{package}{af.extension}"
            )
            specs.append(
                DownloadSpec(
                    url=af.url,
                    dest=output / name,
                    cookies=af.cookies,
                    label=name,
                    gzipped=af.gzipped,
                )
            )
    return specs


def download_app(
    package: str,
    output: Path,
    arch: str = "arm64",
    country: Optional[str] = None,
    proxy: Optional[str] = None,
    dispenser_url: Optional[str] = None,
    profile: Optional[str] = None,
    no_splits: bool = True,
    no_extras: bool = True,
) -> tuple[Path, str]:
    """Download the latest APK a token can see, using one reused-or-fresh token.

    Reuses a valid token from the country's pool if one exists; otherwise
    fetches exactly one fresh token via *profile*, or — if *profile* is
    unset — a random pick from the two newest device profiles. The fresh
    dispenser call rotates proxy regions automatically (see fetch_token);
    *proxy* itself is passed through unpatched to the Google FDFE calls.
    Returns (apk_path, dotted version string) — not the numeric version code.
    """
    if profile:
        chosen_profile = profile
    else:
        candidates = get_latest_probe_profiles(arch, n=2)
        if not candidates:
            raise PlayAPIError("No device profiles available.")
        chosen_profile = random.choice(candidates)[0]

    auth = _acquire_single_token(arch, country, proxy, dispenser_url, chosen_profile)
    if not auth:
        message = (
            "Direct Google authentication failed."
            if direct_auth_enabled()
            else "Could not obtain an auth token — dispenser may be rate-limiting."
        )
        raise PlayAPIError(message)

    try:
        details, delivery = _fetch_latest_delivery(package, auth, country, proxy)
    except AuthExpiredError:
        auth = replace_pool_token(
            auth,
            arch=arch,
            country=country,
            proxy=proxy,
            dispenser_url=dispenser_url,
            profile=chosen_profile,
        )
        if not auth:
            raise PlayAPIError("Token expired and replacement failed.")
        details, delivery = _fetch_latest_delivery(package, auth, country, proxy)

    output.mkdir(parents=True, exist_ok=True)
    specs = _build_specs(
        package, details.version_code, output, delivery, no_splits, no_extras
    )
    download_batch(specs)
    return specs[0].dest, details.version_string
