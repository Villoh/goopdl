"""Device profiles for Google Play API authentication.

Loads Aurora Store .properties files from the profiles/ directory.
Profiles are rotated during token acquisition for reliability.
"""

from pathlib import Path
from typing import Optional

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

FALLBACK_PROFILE = {
    "UserReadableName": "Generic ARM64 Device",
    "Build.HARDWARE": "qcom",
    "Build.RADIO": "unknown",
    "Build.BOOTLOADER": "unknown",
    "Build.FINGERPRINT": "google/sunfish/sunfish:13/TQ3A.230805.001/10316531:user/release-keys",
    "Build.BRAND": "google",
    "Build.DEVICE": "sunfish",
    "Build.VERSION.SDK_INT": "33",
    "Build.VERSION.RELEASE": "13",
    "Build.MODEL": "Pixel 4a",
    "Build.MANUFACTURER": "Google",
    "Build.PRODUCT": "sunfish",
    "Build.ID": "TQ3A.230805.001",
    "Build.TYPE": "user",
    "Build.TAGS": "release-keys",
    "Build.SUPPORTED_ABIS": "arm64-v8a,armeabi-v7a,armeabi",
    "Platforms": "arm64-v8a,armeabi-v7a,armeabi",
    "Screen.Density": "440",
    "Screen.Width": "1080",
    "Screen.Height": "2340",
    "Locales": "en-US",
    "SharedLibraries": (
        "android.ext.shared,android.test.base,android.test.mock,android.test.runner,"
        "com.android.future.usb.accessory,com.android.location.provider,"
        "com.android.media.remotedisplay,com.android.mediadrm.signer,"
        "com.android.nfc_extras,com.google.android.gms,com.google.android.maps,"
        "javax.obex,org.apache.http.legacy"
    ),
    "Features": (
        "android.hardware.audio.output,android.hardware.bluetooth,"
        "android.hardware.bluetooth_le,android.hardware.camera,"
        "android.hardware.camera.autofocus,android.hardware.camera.flash,"
        "android.hardware.camera.front,android.hardware.faketouch,"
        "android.hardware.fingerprint,android.hardware.location,"
        "android.hardware.location.gps,android.hardware.location.network,"
        "android.hardware.microphone,android.hardware.nfc,"
        "android.hardware.screen.landscape,android.hardware.screen.portrait,"
        "android.hardware.sensor.accelerometer,android.hardware.sensor.compass,"
        "android.hardware.sensor.gyroscope,android.hardware.sensor.light,"
        "android.hardware.sensor.proximity,android.hardware.telephony,"
        "android.hardware.touchscreen,android.hardware.touchscreen.multitouch,"
        "android.hardware.touchscreen.multitouch.distinct,"
        "android.hardware.touchscreen.multitouch.jazzhand,"
        "android.hardware.usb.accessory,android.hardware.usb.host,"
        "android.hardware.wifi,android.hardware.wifi.direct,"
        "android.software.app_widgets,android.software.backup,"
        "android.software.home_screen,android.software.input_methods,"
        "android.software.live_wallpaper,android.software.print,"
        "android.software.webview"
    ),
    "GSF.version": "223616055",
    "Vending.version": "82151710",
    "Vending.versionString": "21.5.17-21 [0] [PR] 326734551",
    "Roaming": "mobile-notroaming",
    "TimeZone": "America/New_York",
    "CellOperator": "310260",
    "SimOperator": "310260",
    "Client": "android-google",
    "GL.Version": "196610",
    "GL.Extensions": (
        "GL_OES_EGL_image,GL_OES_EGL_image_external,GL_OES_EGL_sync,"
        "GL_OES_vertex_half_float,GL_OES_framebuffer_object,"
        "GL_OES_rgb8_rgba8,GL_OES_compressed_ETC1_RGB8_texture,"
        "GL_EXT_texture_format_BGRA8888,GL_OES_texture_npot,"
        "GL_OES_packed_depth_stencil,GL_OES_depth24,"
        "GL_OES_depth_texture,GL_OES_texture_float,"
        "GL_OES_texture_half_float,GL_OES_element_index_uint,"
        "GL_OES_vertex_array_object"
    ),
}

# MCC (Mobile Country Code) + MNC for major markets used to register a
# device token as belonging to a specific country via the dispenser.
COUNTRY_MCC: dict[str, tuple[str, str]] = {
    "US": ("310", "38"),   "IN": ("404", "20"),   "GB": ("234", "30"),
    "DE": ("262", "01"),   "JP": ("440", "10"),   "CN": ("460", "00"),
    "BR": ("724", "05"),   "KR": ("450", "05"),   "FR": ("208", "01"),
    "AU": ("505", "01"),   "CA": ("302", "720"),  "RU": ("250", "01"),
    "MX": ("334", "020"),  "ID": ("510", "01"),   "TR": ("286", "01"),
    "SA": ("420", "01"),   "IT": ("222", "01"),   "ES": ("214", "01"),
    "TH": ("520", "01"),   "VN": ("452", "01"),   "MY": ("502", "12"),
    "PH": ("515", "02"),   "ZA": ("655", "01"),   "PK": ("410", "01"),
    "TW": ("466", "92"),   "SG": ("525", "05"),   "NZ": ("530", "24"),
    "SE": ("240", "01"),   "NO": ("242", "01"),   "NL": ("204", "04"),
    "PL": ("260", "01"),   "PT": ("268", "01"),   "CH": ("228", "01"),
    "AT": ("232", "01"),   "BE": ("206", "01"),   "IE": ("272", "01"),
    "GR": ("202", "01"),   "AR": ("722", "310"),  "EG": ("602", "01"),
    "NG": ("621", "30"),   "BD": ("470", "01"),   "HK": ("454", "00"),
}


def patch_profile_country(profile: dict, country: str) -> dict:
    """Return a copy of profile with CellOperator/SimOperator set for country."""
    cc = country.upper()
    if cc not in COUNTRY_MCC:
        return profile
    mcc, mnc = COUNTRY_MCC[cc]
    return {**profile, "CellOperator": mcc, "SimOperator": mnc}


# Tested priority order — profiles that work best with Aurora dispenser and
# restricted apps (banking apps like Chase). Pixel 9a first for arm64.
_PRIORITY_ARM64 = ["Pv", "D2", "eV", "iq", "Fj", "HE", "VP", "Hb", "p6", "B1"]
_PRIORITY_ARMV7 = ["XK", "Gj", "IV", "Gb"]


def _load_properties(filepath: Path) -> dict:
    """Parse a Java-style .properties file into a dict."""
    profile: dict[str, str] = {}
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                profile[key] = val
    return profile


def load_all_profiles() -> dict:
    """Load every .properties file from the profiles directory."""
    profiles: dict[str, dict] = {}
    if not PROFILES_DIR.exists():
        return profiles
    for fp in sorted(PROFILES_DIR.glob("*.properties")):
        profile = _load_properties(fp)
        platforms = profile.get("Platforms", "")
        if "arm64-v8a" in platforms:
            arch = "arm64"
        elif "armeabi-v7a" in platforms:
            arch = "armv7"
        elif "x86" in platforms:
            arch = "x86"
        else:
            arch = "unknown"
        profiles[fp.stem] = {
            "name": profile.get("UserReadableName", fp.stem),
            "arch": arch,
            "profile": profile,
        }
    return profiles


_ALL = load_all_profiles()

ARM64_PROFILES: list[tuple[str, dict]] = [
    (k, d["profile"]) for k, d in _ALL.items() if d["arch"] == "arm64"
]
ARMV7_PROFILES: list[tuple[str, dict]] = [
    (k, d["profile"]) for k, d in _ALL.items() if d["arch"] == "armv7"
]


def find_profile(name: str, arch: str = "arm64") -> Optional[tuple[str, dict]]:
    """Find a profile by exact key or case-insensitive substring of UserReadableName."""
    pool = ARM64_PROFILES if arch != "armv7" else ARMV7_PROFILES
    # exact key match first
    for key, profile in pool:
        if key == name:
            return key, profile
    # substring match on human name
    needle = name.lower()
    for key, profile in pool:
        if needle in profile.get("UserReadableName", "").lower():
            return key, profile
    return None


def get_latest_probe_profiles(arch: str = "arm64", n: int = 2) -> list[tuple[str, dict]]:
    """Return the n profiles most likely to see the latest staged rollout.

    Ranked by newest Android SDK + newest Play Store (Vending) version,
    since Google tends to roll out to newer OS/store versions first.
    """
    pool = ARM64_PROFILES if arch != "armv7" else ARMV7_PROFILES

    def _rank(item: tuple[str, dict]) -> tuple[int, int]:
        _, p = item
        sdk = int(p.get("Build.VERSION.SDK_INT", "0"))
        vending = int(p.get("Vending.version", "0"))
        return (sdk, vending)

    return sorted(pool, key=_rank, reverse=True)[:n]


def get_priority_profiles(arch: str = "arm64") -> list[tuple[str, dict]]:
    """Return profiles ordered by reliability (best first)."""
    if arch == "armv7":
        priority, pool = _PRIORITY_ARMV7, ARMV7_PROFILES
    else:
        priority, pool = _PRIORITY_ARM64, ARM64_PROFILES

    seen: set[str] = set()
    result: list[tuple[str, dict]] = []

    for key in priority:
        for pkey, profile in pool:
            if pkey == key and pkey not in seen:
                result.append((pkey, profile))
                seen.add(pkey)
                break

    for pkey, profile in pool:
        if pkey not in seen:
            result.append((pkey, profile))
            seen.add(pkey)

    return result
