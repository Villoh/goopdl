# Python library

**English** · [Español](es/library.md)

goopdl is also a Python package you can import from a script, a web service,
or a worker. This guide covers the public API: the [`Client`](#client) facade
(`goopdl.client`, also importable from `goopdl`), the functions in
`goopdl.api`, `goopdl.auth`, and `goopdl.download`, and `goopdl.errors`.
Everything else (`goopdl.cli`, underscore names) is internal and can change
without notice.

## Requirements

- Python 3.10 or newer.
- `pip install goopdl` installs five dependencies, all required today:
  `httpx>=0.27`, `typer>=0.16`, `rich>=13`, `websocket-client>=1.8`, and
  `keyring>=25`. Only the CLI uses `keyring`, to keep the token of
  `goopdl auth login` in the system keyring.
  The `typer` minimum went up from 0.9: if you pin `typer` yourself, pin 0.16
  or newer. See [installation](installation.md#as-a-library) for pinning.

Importing `goopdl`, `goopdl.client`, `goopdl.api`, `goopdl.auth`,
`goopdl.download`, or `goopdl.errors` does not import `rich`, `typer`,
`websocket-client`, or `keyring`, prints nothing, and touches no files. Only
these load them:

| Module or call | Loads |
| -------------- | ----- |
| `goopdl.cli` | `typer`, `rich` |
| `download.download_batch()` and `api.download_app()` (progress bars) | `rich` |
| `goopdl.browser_oauth` | `websocket-client` |
| CLI commands that read or save a Google account in the keyring | `keyring` |

## Client

`Client` is the entry point for backends. It takes its settings from a
`Config` object instead of environment variables, so one process can serve
several accounts and cache directories at once, from threads or asyncio. It
wraps the same functions the CLI uses, which stay available for step-by-step
control (see [quick start](#quick-start) onwards).

### Quick start

```python
from pathlib import Path

from goopdl import Client, Config

client = Client(Config(config_dir=Path("goopdl-cache").resolve(), country="US"))

details = client.details("com.whatsapp")
print(details.title, details.version_string, details.version_code)

result = client.download("com.whatsapp", Path("apks"), splits=True)
print(result.path, result.version_string, len(result.files))
```

`Client(config=None, **overrides)`

`Client(config, **overrides)` is short for `Client(config.replace(**overrides))`,
and `Client(country="US")` builds the `Config` for you. `download` returns a
`DownloadResult`: `path` is the base APK, `files` every file written (base APK
first), `version_string` the dotted version. Unlike `api.download_app`, it
draws no progress bars.

### Config

`Config(config_dir=None, credentials=None, arch="arm64", country=None, proxy=None, profile=None, dispenser_url=None)`

`Config.replace(**changes) -> Config`

A frozen dataclass. `replace` returns a validated copy.

| Field | Meaning |
| ----- | ------- |
| `config_dir` | Token cache directory. Must be absolute after `~` expansion (`ValueError` otherwise). `None` resolves it on each call like the CLI, see [token pool and cache](#token-pool-and-cache) |
| `credentials` | A `Credentials` for account mode, or `None` for the anonymous dispenser |
| `arch` | One of `profiles.VALID_ARCHS`, checked at once (`InvalidArchError`) |
| `country`, `proxy`, `profile`, `dispenser_url` | As in [concepts](#concepts) |

`repr(config)` hides the proxy (it may hold a password) and masks the
credentials.

### Credentials

`Credentials(email, aas_token)`

A Google account for [account mode](authentication.md#google-account). It is
checked like the environment variables: both values non-empty (they are
stripped), the token starting with `aas_et/`. Otherwise it raises
`DirectAuthConfigurationError`, whose message names the environment variable
it would be. `repr` and `str` show neither the token nor the full email.

```python
from goopdl import Credentials

account = Client(
    config_dir=Path("goopdl-cache/me").resolve(),
    credentials=Credentials("me@example.com", "aas_et/AKppINa..."),  # load it from your secret store
)
print(account.config.credentials)  # Credentials(email=m***@example.com, aas_token=<hidden>)
print(account.details("com.whatsapp").version_code)
```

**A Client never reads `GOOPDL_ACCOUNT_EMAIL` or `GOOPDL_AAS_TOKEN`.**
`credentials=None` means anonymous mode even when those variables are set, so
a server can't switch accounts because of its environment. For the CLI's
behaviour, ask for it:

`Credentials.from_env() -> Credentials | None`

```python
cli_like = Client(credentials=Credentials.from_env())  # None (anonymous) when neither is set
```

It raises `DirectAuthConfigurationError` if only one of the two is set.
`GOOPDL_CONFIG_DIR` and `XDG_CONFIG_HOME` still apply when `config_dir` is
`None`.

The library never reads the account saved by `goopdl auth login`
(`account.json` and the system keyring), neither in a `Client` nor in the
module functions. Only the CLI does.

### Methods

| Method | Returns | Does |
| ------ | ------- | ---- |
| `details` | `AppDetails` | Pool token + [`get_details`](#get_details) |
| `search` | `list[dict]` | Pool token + [`search_apps`](#search_apps) |
| `list_splits` | `list[str]` | Pool token + [`list_splits`](#list_splits) |
| `item` | `dict` | [`fetch_app_item`](#fetch_app_item) |
| `delivery` | `DeliveryResult` | Pool token, then [`purchase`](#purchase) and [`get_delivery`](#get_delivery) with that same token |
| `download` | `DownloadResult` | Like [`download_app`](#download_app), without progress bars |
| `adownload` | `DownloadResult` | `download` for asyncio |
| `clear_cache` | `None` | Delete what this client cached, see [clearing the cache](#clearing-the-cache) |

- `Client.details(package, *, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> AppDetails`
- `Client.search(query, *, limit=10, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> list[dict]`
- `Client.list_splits(package, *, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> list[str]`
- `Client.item(package, *, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> dict`
- `Client.delivery(package, version_code=None, *, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> DeliveryResult`
- `Client.download(package, output, *, splits=False, extras=False, on_progress=None, max_concurrent=4, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> DownloadResult`
- `Client.adownload(package, output, *, splits=False, extras=False, on_progress=None, max_concurrent=4, http_client=None, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> DownloadResult`
- `Client.clear_cache() -> None`
- `DownloadResult(path, version_string, files)`

Details:

- The keyword overrides replace the `Config` value for one call when they are
  not `None`. To clear a value (say, `country`), use a client built from
  `config.replace(country=None)`.
- `details`, `search`, `list_splits`, and `delivery` take a token from the
  pool (in account mode, sign in) and retry once with a fresh token on HTTP
  401, like the CLI.
- `delivery` without `version_code` asks `details` for the current version
  first. With one, it skips that request.
- `download` and `adownload` fetch the latest version only, like
  `download_app` (there is no `version_code` argument). `splits=True` adds the
  split APKs, `extras=True` the OBB files and asset packs. `on_progress` and
  `max_concurrent` work as in [`download_files`](#download_files).
- `download` raises `RuntimeError` inside a running event loop: await
  `adownload` there.
- Failures raise the same `GoopdlError` subclasses as the functions; no method
  returns `None` on failure. Valid pool tokens keep serving calls while the
  dispenser answers HTTP 429. Token errors are translated like in
  `download_app`: a dispenser 429 with no valid pool token left is a
  `RateLimitedError` with its
  `retry_after` (the `DispenserRateLimitedError` is the `__cause__`), a
  dispenser outage or failed sign-in a `PlayAPIError`. A busy pool raises
  `PoolBusyError`, except in `item`, which follows `fetch_app_item`.

### Clearing the cache

`clear_cache()` deletes what the client cached in its `config_dir`: the
anonymous token files and, in account mode, this account's
[device record](authentication.md#what-account-mode-saves) and its signed-in
token in memory. Other accounts sharing the directory keep theirs. Without
credentials it is `auth.clear_auth()` for that directory, which also deletes
every account's device record, like `goopdl auth clear-cache`. Lock files
stay, and so does the account saved by `goopdl auth login`, file and keyring
entry: only `goopdl auth logout` deletes it.

```python
account.clear_cache()  # the next call registers a new device and signs in again
```

### Async

`adownload` runs the blocking part (token, details, purchase, delivery) in a
worker thread with `asyncio.to_thread`, then downloads natively with
`download_files`. `http_client` is used for the CDN downloads and never
closed. The other methods are blocking: offload them yourself.

```python
import asyncio


async def latest(package):
    client = Client(config_dir=Path("goopdl-cache").resolve())
    details = await asyncio.to_thread(client.details, package)
    result = await client.adownload(
        package, Path("apks"),
        on_progress=lambda label, done, total: None,  # runs on the event loop
    )
    return details.version_code, result.path


print(asyncio.run(latest("com.whatsapp")))
```

Cancelling `adownload` during the download removes its `.part` files. During
the worker-thread part, the thread finishes its current step first, as with
any `asyncio.to_thread` call.

### Several tenants

One `Client` per tenant, each with its own cache directory and, if needed,
its own account. They can run at the same time:

```python
tenants = {
    "acme": Client(config_dir=Path("cache/acme").resolve(), country="US"),
    "globex": Client(config_dir=Path("cache/globex").resolve(), country="DE"),
}


async def download_for_all(package):
    return await asyncio.gather(
        *(client.adownload(package, Path("apks") / name) for name, client in tenants.items())
    )


for result in asyncio.run(download_for_all("com.whatsapp")):
    print(result.path)
```

### Threads and context

A `Client` holds no mutable state: share one between threads and tasks. Each
call applies its `Config` through a `contextvars` variable that it sets inside
the thread doing the work, so concurrent calls never see each other's cache
directory or account. The file locks of the [token pool](#token-pool-and-cache)
still serialize callers that share a cache directory.

The module functions in `goopdl.api` and `goopdl.auth` don't change: called
directly, they read the cache directory and the account from the environment
on each call, as before.

## Quick start

### One call

`download_app` gets a token, finds the latest version, and downloads the base
APK into a directory:

```python
from pathlib import Path

from goopdl import api

apk, version = api.download_app("com.whatsapp", Path("apks"))
print(apk, version)  # apks/com.whatsapp-<version code>.apk and the version string
```

It is synchronous and draws Rich progress bars on stdout. For a service, use
the [`Client`](#client), or the step-by-step pipeline below, and see
[async backends](#async-backends).

### Step by step

The same pipeline the CLI runs, one call per step (see
[how it works](how-it-works.md#the-download-pipeline)):

```python
from pathlib import Path

from goopdl import api, auth
from goopdl.download import DownloadSpec, download_batch

package = "com.whatsapp"
token = auth.pick_pool_token(arch="arm64", raise_on_error=True)
if token is None:
    raise SystemExit("no token: unknown profile or the cache directory is unusable")

details = api.get_details(package, token)
delivery_token = api.purchase(package, details.version_code, token)
delivery = api.get_delivery(
    package, details.version_code, token, delivery_token=delivery_token
)


def base_apk_spec(package, version_code, delivery, output):
    """Turn a DeliveryResult into a verified download of the base APK."""
    return DownloadSpec(
        url=delivery.gzipped_url or delivery.download_url,
        dest=output / f"{package}-{version_code}.apk",
        cookies=delivery.cookies,
        gzipped=bool(delivery.gzipped_url),
        integrity_required=True,
        expected_size=delivery.download_size,
        sha1=delivery.sha1,
        sha256=delivery.sha256,
    )


output = Path("apks")
output.mkdir(exist_ok=True)
spec = base_apk_spec(package, details.version_code, delivery, output)
download_batch([spec], require_integrity=True)
print(spec.dest)
```

The rest of this guide reuses `package`, `token`, `details`, `delivery`,
`output`, and `base_apk_spec` from this snippet.

## Concepts

### Auth dicts

Every Google Play call takes an `auth` dict: a bearer token (`authToken`) plus
the device identity it was issued for (`gsfId`, device tokens, user agent).
Treat it as an opaque secret. Pass it back unchanged, don't log it, and don't
edit it. goopdl adds `_device_profile` and `_cached_at` keys.

Tokens come from one of two places, picked automatically on every call:

| Mode | Source | Cached on disk |
| ---- | ------ | -------------- |
| Anonymous (default) | Aurora's token dispenser, or `dispenser_url` | Yes, see [token pool](#token-pool-and-cache) |
| Google account | `GOOPDL_ACCOUNT_EMAIL` and `GOOPDL_AAS_TOKEN` in the environment, or a `Client`'s [`Credentials`](#credentials) | Only the device registration, no token. The token stays in memory |

Account mode is described in [authentication](authentication.md#google-account).
The module functions read the variables from `os.environ` on each call, so for
them they apply to the whole process; a `Client` uses only its own
`credentials`. If only one is set, or the token doesn't start with `aas_et/`,
every token function raises `DirectAuthConfigurationError`. In account mode
`pick_pool_token`, `replace_pool_token`, and `ensure_auth` share one sign-in
per cache directory, account, architecture, country, and profile, held in
process memory until it expires or Google rejects it with HTTP 401.
Concurrent callers wait for a single sign-in. `fetch_token` and `ensure_pool`
still sign in on a new device on every call.

To get an AAS token from code there are two helpers. Both talk to Google and
need a person in the loop, so they belong in a setup script, not a request
handler:

```python
# Not run by the docs check: needs a real Google account and a browser.
from goopdl.aastoken import fetch_aas_token
from goopdl.browser_oauth import capture_oauth_credentials

email, oauth_token = capture_oauth_credentials(timeout=300)  # opens Chrome/Edge/Chromium/Brave
aas_token = fetch_aas_token(email, oauth_token)              # "aas_et/..."
```

`fetch_aas_token` takes only the one-time `oauth2_4/` token: anything else,
a password included, raises `ValueError` before any request. It raises
`AASTokenError` when Google rejects the token, and sanitized `httpx` errors on
HTTP or transport failures.
`capture_oauth_credentials` raises `BrowserOAuthError`.

### Token pool and cache

`pick_pool_token` keeps a pool of anonymous tokens per architecture, country,
and pinned profile, and hands them out round-robin. It requests only the
missing tokens, and a full pool makes no network calls. If the dispenser
can't top it up (HTTP 429 or an outage), the valid tokens left still serve
the call. Tokens are dropped after 50 minutes. The pool is explained in
[how it works](how-it-works.md#token-pool).

After a dispenser HTTP 429, the process stops asking that dispenser (the same
`dispenser_url` and `proxy`) until the server's `Retry-After` has passed,
capped at 15 minutes, or for 60 seconds without a usable header. The 60 seconds
are a heuristic: the dispenser doesn't say how long its limit lasts. Inside
that window the token functions send nothing and act as if the dispenser had
answered 429 again: valid pool tokens keep serving calls without a refill,
and with none left you get `DispenserRateLimitedError` (`RateLimitedError`
from `Client`, `fetch_app_item`, and `download_app`) with `retry_after` set to
the seconds left, rounded up. The window lives in this process's memory only:
it isn't saved, and other processes don't see it. `auth.clear_auth()` and
`clear_cache()` on a `Client` without credentials forget it. Account mode
doesn't use the dispenser, so it has no window.

The cache directory is resolved on every call by `auth.get_config_dir()`:

1. Inside a `Client` call, its `config_dir` if set
2. `$GOOPDL_CONFIG_DIR` (`~` is expanded)
3. `$XDG_CONFIG_HOME/goopdl`
4. `~/.config/goopdl`

A relative value in either variable is ignored (`GOOPDL_CONFIG_DIR` logs a
warning), so the cache never lands in the working directory.

```python
from goopdl import auth

print(auth.get_config_dir())
```

| File | Written by |
| ---- | ---------- |
| `token-pool-{arch}[-{country}][-profile-{key}].json` | `pick_pool_token`, `ensure_pool`, `replace_pool_token` |
| `pool-index-{arch}[-{country}][-profile-{key}].json` | `pick_pool_token` (round-robin position) |
| `token-pool-…lock` | Lock sidecars, never deleted |
| `auth-{arch}[-{country}][-profile-{key}].json` | `ensure_auth` (single cached token) |
| `direct-device-{account}-{arch}[-{country}]-profile-{key}.json` | Account mode: the registered device, keyed by a hash of the email |

On POSIX the directory is created `0700` (an existing one you own is tightened
to `0700`) and files are written atomically as `0600`. `auth.clear_auth()`
deletes the JSON files and keeps the lock sidecars. In account mode the only
file is the device registration (GSF ID, check-in consistency token, device
config token). The email, the AAS token, and the bearer token are never
written; if the directory isn't writable, the registration stays in memory.
See [what account mode saves](authentication.md#what-account-mode-saves).

### Device profiles and architectures

The device profile decides which build Google serves. `profile` takes a
profile key (`"Pv"`) or a case-insensitive substring of its name
(`"Pixel 9a"`). Without one, goopdl rotates through profiles in priority order.

```python
from goopdl import profiles

print(profiles.VALID_ARCHS)  # ('arm64', 'armv7', 'x86', 'x86_64', 'tv')
for key, props in profiles.get_priority_profiles("arm64")[:3]:
    print(key, props["UserReadableName"])
```

- `arch` must be one of `profiles.VALID_ARCHS`. Functions that touch the cache
  raise `InvalidArchError` (a `ValueError`) for anything else.
- An unknown profile makes `fetch_token` return `None`, even with
  `raise_on_error=True`, and logs `Profile not found`.
- Every profile gets its own pool file, so pinning a profile never reuses
  another profile's tokens.

### Country and proxy

`country` is a 2-letter code such as `"US"` or `"IN"`. It sets Google's `gl`
parameter and the language headers, patches the device's MCC/MNC for known
countries, and keys the cache files. On its own it only changes headers. For a
region's real variant, also send traffic from that region with `proxy`.

`proxy` is a URL string (`http://user:pass@host:8080`). It is used for the
dispenser and the Google Play API calls. For the dispenser it is rotated across
regions: put `{region}` in the URL, or goopdl swaps the country code it finds in
the proxy password. SOCKS proxies need `pip install "httpx[socks]"`.

`download_files` does not take a proxy. To route CDN downloads through one,
pass your own client: `client=httpx.AsyncClient(proxy=..., timeout=...)`.

## API reference

All functions in `goopdl.api` and `goopdl.auth` are synchronous and blocking,
and so are the `Client` methods except `adownload`. They use synchronous
`httpx` with a 30-second timeout, reuse keep-alive connections, and may
`time.sleep` (rate-limit backoff, dispenser cooldowns, lock waits).
`goopdl.download.download_files` and `Client.adownload` are the only
coroutines.

### `goopdl.api`

| Function | Returns |
| -------- | ------- |
| [`get_details`](#get_details) | `AppDetails` |
| [`get_details_raw`](#get_details_raw) | Raw details response as a nested dict |
| [`fetch_app_item`](#fetch_app_item) | Flat app metadata dict, token handled for you |
| [`search_apps`](#search_apps) | `list[dict]` of search hits |
| [`list_splits`](#list_splits) | `list[str]` of split names |
| [`purchase`](#purchase) | Delivery token (`str`, may be empty) |
| [`get_delivery`](#get_delivery) | `DeliveryResult` with download URLs |
| [`download_app`](#download_app) | `(apk_path, version_string)` |
| [`validate_package`](#validate_package) | The package name, or raises |

#### `get_details`

`get_details(package, auth, country=None, proxy=None) -> AppDetails`

```python
details = api.get_details("com.whatsapp", token, country="US")
print(details.title, details.developer, details.version_string, details.version_code)
```

`AppDetails` fields: `package`, `title`, `developer`, `version_string`,
`version_code`, `rating`, `downloads`, `play_url`. Raises
`AppNotAvailableError` on HTTP 404 or when Play returns no document for this
device profile.

#### `get_details_raw`

`get_details_raw(package, auth, country=None, proxy=None) -> dict`

The whole response, decoded generically. Keys are protobuf field numbers as
strings:

```python
raw = api.get_details_raw("com.whatsapp", token)
doc = raw["1"]["2"]["4"]  # DocV2; missing if Play sent no document
print(doc["5"])           # title
```

#### `fetch_app_item`

`fetch_app_item(package, arch="arm64", country=None, proxy=None, profile=None, dispenser_url=None) -> dict`

Picks a pool token, fetches raw details, replaces the token once on HTTP 401,
and maps the result to a flat dict (`title`, `version`, `rating`,
`screenshots`, `permissions`...). Returns `{}` when Play sends no document.

```python
item = api.fetch_app_item("com.whatsapp", country="US")
print(item.get("title"), item.get("version"))
```

It doesn't let dispenser errors through. If the dispenser answers HTTP 429
while replacing an expired token and no other valid pool token is left, you
get `RateLimitedError` with the same
`retry_after`, and its `__cause__` is the `DispenserRateLimitedError`. Any
other failure to get a token, a busy pool included, is a plain `PlayAPIError`.

```python
from goopdl.errors import RateLimitedError

try:
    item = api.fetch_app_item("com.whatsapp")
except RateLimitedError as exc:
    # Play's 429, or the dispenser's with no valid pool token left.
    print("rate limited, retry after", exc.retry_after)
```

#### `search_apps`

`search_apps(query, auth, limit=10, country=None, proxy=None) -> list[dict]`

```python
for hit in api.search_apps("messenger", token, limit=5):
    print(hit["package"], hit["title"], hit["creator"])
```

#### `list_splits`

`list_splits(package, auth, country=None, proxy=None) -> list[str]`

Split names from the details metadata, sorted. HTTP 404 raises a plain
`PlayAPIError` with `status_code=404`.

```python
print(api.list_splits("com.whatsapp", token))  # e.g. ['config.arm64_v8a', ...]
```

#### `purchase`

`purchase(package, version_code, auth, country=None, proxy=None) -> str`

"Buys" a free app and returns Play's delivery token, or `""`. Other non-200
answers are not errors: the app may already be owned, and `get_delivery`
reports the real problem. HTTP 401 and 429 raise.

```python
delivery_token = api.purchase("com.whatsapp", details.version_code, token)
```

#### `get_delivery`

`get_delivery(package, version_code, auth, country=None, proxy=None, delivery_token="", locales=None) -> DeliveryResult`

```python
delivery = api.get_delivery(
    "com.whatsapp", details.version_code, token,
    delivery_token=delivery_token, locales=["es", "fr"],
)
print(delivery.download_size, [s.name for s in delivery.splits])
```

- `locales` asks for extra language splits.
- On HTTP 429 it retries twice, waiting 1 s and then 3 s, or longer if
  `Retry-After` asks. Then it raises `RateLimitedError`. If `Retry-After` is
  over 30 seconds it raises at once.
- `DeliveryResult` holds `download_url`, `gzipped_url`, `download_size`,
  `sha1`, `sha256`, `cookies`, `splits` (`SplitInfo`), `additional_files`
  (`AdditionalFile`: OBB and asset packs), and `dex_metadata`.
- Every URL is HTTPS on a Google domain, or empty. See [security](#security).
- The URLs are signed and expire. Download them soon, and request a new
  delivery after a 403.

To download the splits too, build one spec per `SplitInfo`. Split names come
from the server, so keep them to safe file-name characters:

```python
import re


def split_specs(package, version_code, delivery, output):
    specs = []
    for split in delivery.splits:
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{package}-{version_code}-{split.name}.apk")
        specs.append(DownloadSpec(
            url=split.gzipped_url or split.url,
            dest=output / name,
            gzipped=bool(split.gzipped_url),
            integrity_required=True,
            expected_size=split.size,
            sha1=split.sha1,
            sha256=split.sha256,
        ))
    return specs


specs = [base_apk_spec(package, details.version_code, delivery, output)]
specs += split_specs(package, details.version_code, delivery, output)
```

OBB files and asset packs (`delivery.additional_files`) come without digests:
download them with `integrity_required=False`.

#### `download_app`

`download_app(package, output, arch="arm64", country=None, proxy=None, dispenser_url=None, profile=None, no_splits=True, no_extras=True) -> tuple[Path, str]`

```python
apk, version = api.download_app("com.whatsapp", Path("apks"), no_splits=False)
```

- Downloads the latest version visible to one token, the same as
  [`fast-download`](commands.md#fast-download). There is no version argument:
  for a specific version code, call `purchase` and `get_delivery` yourself.
- Without `profile`, picks one of the two newest profiles, preferring one that
  already has a warm pool.
- Tries up to three times to get a token for that profile, 5 seconds apart
  (not after a 429).
- Returns the base APK path and the version string, not the version code.
- Uses `download_batch`, so it prints progress bars and raises `RuntimeError`
  inside a running event loop.
- Raises any `GoopdlError`, not only `PlayAPIError`. A busy token pool raises
  `PoolBusyError` (1.4.x raised `PlayAPIError`), and the CDN raises
  `DownloadError` or `IntegrityError`. See [what raises what](#what-raises-what).

#### `validate_package`

`validate_package(package) -> str`

Every `api` function that takes a package name calls it first, before any
network request.

```python
from goopdl.errors import InvalidPackageError

api.validate_package("com.whatsapp")  # returns "com.whatsapp"
try:
    api.validate_package("whatsapp")
except InvalidPackageError as exc:
    print(exc)  # Invalid package name: 'whatsapp'
```

### `goopdl.auth`

All token functions accept `arch`, `country`, `proxy`, `dispenser_url`, and
`profile` with the meanings from [concepts](#concepts). They return `None` (or
`[]`) on failure unless you pass `raise_on_error=True`, which raises the typed
errors from [errors](#errors) instead. Use `raise_on_error=True` in services:
`None` doesn't tell a rate limit from an outage.

#### `fetch_token`

`fetch_token(dispenser_url=None, arch="arm64", proxy=None, country=None, profile=None, raise_on_error=False, *, cooldown_retry=True) -> dict | None`

One fresh token, not cached. Each call is a dispenser request (Aurora allows
about 20 per hour), so prefer `pick_pool_token`. Inside a
[429 window](#token-pool-and-cache) it sends nothing: it returns `None`, or
raises `DispenserRateLimitedError` with `raise_on_error=True`.

```python
fresh = auth.fetch_token(arch="arm64", country="US", raise_on_error=True)
```

Without `profile` it rotates profiles until one works, and stops on HTTP 429
or after two transport/5xx failures in a row. With `profile` it retries once
after 5 seconds on transport/5xx failures, unless `cooldown_retry=False`.

#### `pick_pool_token`

`pick_pool_token(arch="arm64", country=None, proxy=None, dispenser_url=None, profile=None, size=5, raise_on_error=False) -> dict | None`

Fills the pool up to `size` valid tokens, then returns the next one
round-robin. If the dispenser stops issuing tokens (HTTP 429 included), it
uses the valid ones it has; `raise_on_error=True` raises only when none is
left. Used in the [quick start](#step-by-step). It can return `None`
even with `raise_on_error=True` (unknown profile, unusable cache directory), so
check the result.

#### `ensure_pool`

`ensure_pool(arch="arm64", country=None, proxy=None, dispenser_url=None, profile=None, size=5, raise_on_error=False) -> list[dict]`

Warms the pool without picking a token, for example at worker start-up. It may
return fewer than `size` tokens if the dispenser stops issuing them (HTTP 429
included). With `raise_on_error=True` it raises only when no valid token is
left.

```python
tokens = auth.ensure_pool(arch="arm64", size=5, raise_on_error=True)
print(len(tokens))
```

#### `replace_pool_token`

`replace_pool_token(failed_token, arch="arm64", country=None, proxy=None, dispenser_url=None, profile=None, raise_on_error=False) -> dict | None`

Call it after `AuthExpiredError`, with the same `arch`, `country`, and
`profile` you picked the token with. It removes the dead token and returns a
fresh one. If the replacement fails it returns a surviving pool member (or
`None`). On a dispenser HTTP 429 it returns a surviving member too, even with
`raise_on_error=True`. Only when no valid token is left does it raise
`DispenserRateLimitedError`, even without `raise_on_error`.

```python
from goopdl.errors import AuthExpiredError


def details_with_refresh(package, token, arch="arm64"):
    try:
        return api.get_details(package, token), token
    except AuthExpiredError:
        token = auth.replace_pool_token(token, arch=arch, raise_on_error=True)
        if token is None:
            raise
        return api.get_details(package, token), token


details, token = details_with_refresh(package, token)
```

#### Other helpers

| Function | What it does |
| -------- | ------------ |
| `get_config_dir() -> Path` | The cache directory in effect now |
| `ensure_auth(arch="arm64", dispenser_url=None, force_refresh=False, proxy=None, country=None, profile=None) -> dict \| None` | One cached token in `auth-*.json`, refreshed after 50 minutes (account mode: the in-memory sign-in). Never raises dispenser errors |
| `clear_auth() -> None` | Delete every cached token and account device record, and forget this directory's in-memory sign-ins |
| `direct_auth_enabled() -> bool` | Whether account mode is configured. Raises `DirectAuthConfigurationError` if half-configured |

### `goopdl.download`

A `DownloadSpec` describes one file: `url`, `dest`, `cookies`, `label`,
`gzipped`, `integrity_required`, `expected_size`, `sha1`, `sha256`. goopdl
streams it to a unique `.part` file next to `dest`, checks it, then renames it
into place. A file already at `dest` with the right size and digest is not
downloaded again.

#### `download_files`

`download_files(specs, *, require_integrity=False, on_progress=None, max_concurrent=4, client=None) -> None`

The async downloader. It writes nothing to stdout.

```python
import asyncio

import httpx

from goopdl.download import download_files


def on_progress(label, done, total):
    print(f"{label}: {done}/{total or '?'} bytes")


async def main():
    timeout = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await download_files(
            specs, require_integrity=True, on_progress=on_progress,
            max_concurrent=2, client=client,
        )


asyncio.run(main())
```

- `on_progress(label, done, total)` runs inline on the event loop: once when a
  file starts, then per chunk of up to 64 KB. `done` counts bytes received
  (compressed bytes for gzipped files). `total` is the `Content-Length`, or
  `None`. A verified file already on disk reports `(label, size, size)` once. Keep it fast. If it
  raises, every download is cancelled and your exception propagates
  unchanged.
- `client` is optional and never closed by goopdl. Set its timeout yourself:
  httpx's default is 5 seconds. goopdl uses the timeout above when it creates
  its own client.
- `max_concurrent` limits parallel files within this call. It must be at least
  1 (`ValueError` otherwise).
- On the first failure, or if the task is cancelled, the other downloads are
  cancelled and no `.part` files are left behind.

#### `download_batch`

`download_batch(specs, require_integrity=False) -> None`

A synchronous wrapper around `download_files` with Rich progress bars on
stdout. It raises `RuntimeError` if called while an event loop is running;
from async code, await `download_files`.

#### Integrity

- A spec is verified when `integrity_required=True` or it has a `sha1` or
  `sha256`. Verification checks the final size against `expected_size` and the
  digest (SHA-256 if present, else SHA-1). A mismatch raises `IntegrityError`.
- A spec with `integrity_required=True` but no size or digest fails with
  `IntegrityError: missing integrity metadata`.
- `require_integrity=True` runs that check for every spec before the first
  byte is downloaded, instead of failing halfway through.
- Google sends digests for base APKs, splits, and `.dm` files, not for OBB
  files or asset packs.

#### `write_manifest`

`write_manifest(path, specs) -> None`

Writes the [integrity manifest](commands.md#integrity-manifest) JSON
atomically. Every spec needs digest metadata, so pass only verified files:

```python
from goopdl.download import write_manifest

write_manifest(output / "manifest.json", [s for s in specs if s.integrity_required])
```

## Async backends

### Offload the blocking calls

[`Client.adownload`](#async) does this for you. To control each step, note
that no `api` or `auth` function is async, and they can block for seconds or
minutes (a cold pool mints up to five tokens, each a dispenser request with a
30-second timeout). Run
them in a worker thread with `asyncio.to_thread`, and await `download_files`
directly:

```python
import asyncio
from pathlib import Path

from goopdl import api, auth
from goopdl.download import download_files


def resolve_latest(package, arch="arm64"):
    """Blocking: token, details, purchase, and delivery."""
    token = auth.pick_pool_token(arch=arch, raise_on_error=True)
    if token is None:
        raise RuntimeError("no token available")
    details = api.get_details(package, token)
    delivery_token = api.purchase(package, details.version_code, token)
    delivery = api.get_delivery(
        package, details.version_code, token, delivery_token=delivery_token
    )
    return details, delivery


async def download_latest(package, output):
    api.validate_package(package)  # fail fast, before using a thread
    details, delivery = await asyncio.to_thread(resolve_latest, package)
    output.mkdir(parents=True, exist_ok=True)
    spec = base_apk_spec(package, details.version_code, delivery, output)
    await download_files([spec], require_integrity=True)
    return spec.dest


print(asyncio.run(download_latest("com.whatsapp", Path("apks"))))
```

Don't call `download_app` or `download_batch` from a coroutine: they call
`asyncio.run` and raise `RuntimeError`. In a worker thread they run, but print
progress bars to your server's stdout.

### FastAPI example

```python
# Not run by the docs check: needs FastAPI. download_latest is defined above.
from pathlib import Path

from fastapi import FastAPI, HTTPException

from goopdl.errors import (
    AppNotAvailableError,
    DispenserRateLimitedError,
    GoopdlError,
    InvalidPackageError,
    RateLimitedError,
)

app = FastAPI()


@app.post("/apps/{package}/download")
async def download(package: str):
    try:
        path = await download_latest(package, Path("/srv/apks"))
    except InvalidPackageError:
        raise HTTPException(422, "invalid package name")
    except AppNotAvailableError:
        raise HTTPException(404, "app not available")
    except (RateLimitedError, DispenserRateLimitedError) as exc:
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        raise HTTPException(503, "rate limited", headers=headers)
    except GoopdlError as exc:
        raise HTTPException(502, type(exc).__name__)  # messages carry no secrets, but stay terse
    return {"file": path.name}
```

### Concurrency

- Calls are safe from several threads and processes at once. Each pool
  (arch + country + profile) has a lock file in the cache directory, and pool
  updates are written atomically.
- The lock is held while missing tokens are minted. Other callers for the same
  pool wait up to 180 seconds, then get `None` (or `PoolBusyError` with
  `raise_on_error=True`). `download_app` raises `PoolBusyError`;
  `fetch_app_item` raises `PlayAPIError`.
- The locks are OS file locks on a local disk. Don't share the cache directory
  over NFS or between hosts.
- `max_concurrent` applies per `download_files` call. Across requests, limit
  downloads with your own `asyncio.Semaphore`, and share one `AsyncClient`
  through `client=` to reuse connections.
- Synchronous calls to Google Play and the dispenser reuse keep-alive
  connections, shared by every thread of the process. A forked child starts
  with its own fresh connections.
- The thread pool behind `asyncio.to_thread` is small. Size it for calls that
  sleep through rate-limit backoff.

### Tenants

Use one [`Client` per tenant](#several-tenants), each with its own
`config_dir` and `credentials`: they share the process safely.

The module functions take the cache directory and account credentials from
environment variables that apply to the whole process. Don't change them per
request: other threads would read the new values mid-call. With the functions
alone, isolate tenants with one worker process each:

```bash
GOOPDL_CONFIG_DIR=/var/lib/goopdl/tenant-a python worker.py
GOOPDL_CONFIG_DIR=/var/lib/goopdl/tenant-b python worker.py
```

## Errors

### Hierarchy

Every goopdl exception derives from `GoopdlError`. Import them from
`goopdl.errors` (or `goopdl`). Importing that module doesn't load httpx; each
class loads its own module on first access. `dir(goopdl)` and tab completion
list them too.

```text
GoopdlError
├── PlayAPIError                       .status_code
│   ├── NetworkError                   also httpx.TransportError
│   ├── AuthExpiredError
│   ├── RateLimitedError               .retry_after, status_code=429
│   ├── VersionUnavailableError
│   │   └── AppNotSupportedError
│   ├── AppNotAvailableError
│   ├── AppNotPurchasedError
│   └── MalformedResponseError
├── InvalidPackageError                also ValueError
├── InvalidArchError                   also ValueError
├── DirectAuthConfigurationError       also ValueError
├── DirectAuthError                    also RuntimeError; .stage, .status, .retry_after
│   ├── DispenserRateLimitedError      .retry_after, status=429
│   └── DispenserUnavailableError      .status, .reason
├── PoolBusyError                      also TimeoutError
├── AASTokenError                      also ValueError
├── BrowserOAuthError                  also RuntimeError
└── IntegrityError                     also RuntimeError
    └── DownloadError                  .status_code, .retryable, .retry_after
```

`NetworkError` is still an `httpx.TransportError` (and so an
`httpx.HTTPError`), so old `except httpx.HTTPError` handlers keep working.

The dispenser errors are not `PlayAPIError`s: catch `DirectAuthError` for any
token failure. `download_app` and `fetch_app_item` translate some of them, see
below.

### What raises what

| Error | Raised by | When | Retry? |
| ----- | --------- | ---- | ------ |
| `InvalidPackageError` | `api` functions that take a package | Malformed application ID, before any request | No |
| `InvalidArchError` | Pool and cache functions, `download_app`, `fetch_app_item`, `Config` | `arch` not in `VALID_ARCHS` | No |
| `DirectAuthConfigurationError` | Every token function, `Credentials`, `Credentials.from_env` | Account mode half-configured | No, fix the environment or the values |
| `NetworkError` | Google Play calls in `api` | Timeout, DNS, refused or dropped connection | Yes, with backoff |
| `AuthExpiredError` | `get_details*`, `search_apps`, `list_splits`, `purchase`, `get_delivery` | HTTP 401 | Once, after `replace_pool_token` |
| `RateLimitedError` | Same, plus `fetch_app_item` and `download_app` | HTTP 429 from Play (`get_delivery` already retried twice). In `download_app`, also a dispenser or account-mode 429; in `fetch_app_item`, a dispenser 429 while replacing an expired token with no valid pool token left. For the dispenser's, `__cause__` is the `DispenserRateLimitedError` | Only after `retry_after`; stop if it is `None` |
| `AppNotAvailableError` | `get_details`, `get_details_raw`, `fetch_app_item` | HTTP 404, or no document for this device | Not with the same profile and country |
| `VersionUnavailableError` | `get_delivery` | HTTP 404, or no download URL | No |
| `AppNotSupportedError` | `get_delivery` | The profile can't receive this version | With another profile or arch |
| `AppNotPurchasedError` | `get_delivery` | Paid app, or not offered in the region | No |
| `MalformedResponseError` | `api` parsers | Undecodable protobuf | Not with another token |
| `PlayAPIError` | All of `api` | Any other HTTP status (`.status_code`); no token in `fetch_app_item` / `download_app` | 5xx later, others no |
| `DispenserRateLimitedError` | Token functions with `raise_on_error=True`; `replace_pool_token` even without it. Pool functions raise it only when no valid pool token is left. `fetch_app_item` and `download_app` wrap it in `RateLimitedError` | Dispenser HTTP 429, or a call inside the [window](#token-pool-and-cache) a 429 opened, with no request; then `retry_after` is the time left | Only after `retry_after` |
| `DispenserUnavailableError` | Token functions with `raise_on_error=True` | `.reason`: `transport`, `server` (5xx), or `profile_rejected` | `transport`/`server` later; `profile_rejected` with another profile |
| `DirectAuthError` | Token functions in account mode with `raise_on_error=True` | Sign-in failed at `.stage` (`checkin`, `device config upload`, `token exchange`) with `.status`. A 429 on the optional `toc` call is not an error: the token comes without `dfeCookie` and a warning is logged | 429 after `.retry_after`; transport/5xx later; not 400/401/403 |
| `PoolBusyError` | Pool functions with `raise_on_error=True`; `download_app` always | Pool lock not acquired in 180 s | Later |
| `IntegrityError` | `download_files`, `download_batch`, `write_manifest` | Missing metadata, size or digest mismatch | No |
| `DownloadError` | `download_files`, `download_batch` | CDN HTTP or transport failure, or a malformed redirect `Location` (never retryable) | If `.retryable`; on 403 request a new delivery |

`download_app` and `fetch_app_item` don't let dispenser errors through. A 429
becomes `RateLimitedError` with the same `retry_after` and the original as
`__cause__`. In `download_app` a dispenser outage becomes `PlayAPIError` with
`status_code`; in `fetch_app_item` any other failure to get a token is a
`PlayAPIError`. Not everything they raise is a `PlayAPIError`, though:
`download_app` can also raise `PoolBusyError`, `DownloadError`,
`IntegrityError`, `InvalidPackageError`, `InvalidArchError`, and
`DirectAuthConfigurationError`, and `fetch_app_item` the last three. End with
`except GoopdlError`.

Also expect `ValueError` from `download_files` (bad `max_concurrent`),
`RuntimeError` from `download_batch` in an event loop, `OSError` from your
output directory, and from `fetch_aas_token`, sanitized `httpx` errors.

### Retrying

Retry only what the error says is retryable, and wait as long as the server
asks. Don't retry a 429 without a `Retry-After`: back off at a higher level, or
switch network or account. `get_delivery` has already retried twice when it
raises.

```python
import time

from goopdl.errors import DispenserRateLimitedError, NetworkError, RateLimitedError


def call_with_backoff(fn, *args, attempts=3, max_wait=60.0, **kwargs):
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (RateLimitedError, DispenserRateLimitedError) as exc:
            wait = exc.retry_after
            if wait is None or wait > max_wait or attempt == attempts - 1:
                raise  # no hint from the server, or too long: let the caller decide
        except NetworkError:
            if attempt == attempts - 1:
                raise
            wait = 2 ** attempt
        time.sleep(wait)


details = call_with_backoff(api.get_details, package, token)
```

It works for `fetch_app_item` and `download_app` too: they report every 429,
the dispenser's included, as `RateLimitedError`. Inside the dispenser's
[429 window](#token-pool-and-cache), `retry_after` is the time left, so
sleeping that long is enough. `DispenserRateLimitedError`
only reaches you from the token functions in `goopdl.auth`.

For downloads, `DownloadError` tells you what to do. A 403 means the signed
URLs expired, so a new delivery is needed. Other retryable errors can reuse the
same URLs:

```python
from goopdl.errors import DownloadError


async def download_with_retries(package, output, attempts=3):
    details, delivery = await asyncio.to_thread(resolve_latest, package)
    for attempt in range(attempts):
        spec = base_apk_spec(package, details.version_code, delivery, output)
        try:
            await download_files([spec], require_integrity=True)
            return spec.dest
        except DownloadError as exc:
            if attempt == attempts - 1:
                raise
            if exc.status_code == 403:
                details, delivery = await asyncio.to_thread(resolve_latest, package)
            elif exc.retryable:
                await asyncio.sleep(exc.retry_after or 2 ** attempt)
            else:
                raise


print(asyncio.run(download_with_retries("com.whatsapp", Path("apks"))))
```

Catch `DownloadError` before `IntegrityError`, since it is a subclass. An
`IntegrityError` that isn't a `DownloadError` means the bytes or metadata are
wrong: don't retry it blindly.

## Security

- **Tokens are secrets.** Never log the `auth` dict, its `authToken`, or
  `DeliveryResult` URLs and cookies (the URLs are signed).
- **Errors carry no secrets, with one limit.** Public exception messages
  contain no tokens, URLs, or cookies. When `download_app` or
  `fetch_app_item` fail, the traceback holds only their own frame, with the
  proxy cleared: no pool token, delivery token, or signed CDN URL. The only
  chained exception kept is a sanitized goopdl `__cause__`, such as
  `DispenserRateLimitedError`. `get_delivery` drops the delivery token, the
  proxy, and the response before raising, and a half-configured account mode
  raises `DirectAuthConfigurationError` without the email or AAS token in
  any frame. `Client` methods behave like `download_app`: the email and AAS
  token are in no frame they leave behind, except inside the `Client`,
  `Config`, and `Credentials` objects you created. What you pass in yourself
  stays, by design: the `auth` dict is
  still in the frame of the function you gave it to (`get_details`,
  `get_delivery`, …), and the `auth` dict and the proxy URL are still in your
  own frames. If your error reporter captures local variables (Sentry's
  `include_local_variables`, for example), turn that off or scrub `authToken`
  and proxy credentials.
- **Input validation.** Package names are checked by `validate_package` and
  architectures against `VALID_ARCHS` before they reach a URL or a file name.
  Country codes are reduced to letters and digits in cache file names.
- **Google hosts only.** Delivery URLs are kept only if they are HTTPS on
  port 443 under `android.clients.google.com`, `*.google.com`,
  `*.googleapis.com`, `*.ggpht.com`, `*.googleusercontent.com`, or
  `*.gvt1.com`. The last one is where a real delivery ends up:
  `play.googleapis.com/download/by-token` answers 302 and the file is served
  from `rr<N>---sn-<id>.gvt1.com`.
  `download_files` follows at most five redirects, only to those hosts, and
  drops `Authorization`, `Proxy-Authorization`, and `Cookie` headers on
  cross-host redirects. It does not check the initial `spec.url`, so only pass
  URLs that came from `get_delivery`.
- **Output paths.** `dest` is used as given. If you build file names from
  server data (split names), sanitize them as in
  [`get_delivery`](#get_delivery).

### Logging

goopdl logs to the `goopdl` logger tree, with a `NullHandler` on `goopdl`, so
it is silent until your application configures logging. It never logs tokens.
Messages use INFO (pool refills, chosen profile), WARNING (dispenser failures,
naming the device profile), and ERROR. To see everything:

```python
import logging

logging.basicConfig(level=logging.WARNING)
logging.getLogger("goopdl").setLevel(logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include full request URLs
```

Keep `httpx` at WARNING or above: at INFO it logs every request URL, including
delivery tokens and signed CDN URLs.

## Migrating from 1.4.x

Existing calls keep their signatures (new parameters are optional). These
behaviours changed:

| Change | 1.4.x | Now | Migration |
| ------ | ----- | --- | --------- |
| Corrupt protobuf | Raw `ValueError` / `EOFError` | `MalformedResponseError` (a `PlayAPIError`) | `except PlayAPIError` covers it |
| Package names | Sent to Google as given | `InvalidPackageError` (a `ValueError`) if there is no dot or a bad character | Call `api.validate_package()` at your input boundary |
| Unknown `arch` | Used in cache file names | `InvalidArchError` (a `ValueError`) | Check against `profiles.VALID_ARCHS` |
| Cache directory | Always `~/.config/goopdl` | `auth.get_config_dir()`: `$GOOPDL_CONFIG_DIR`, `$XDG_CONFIG_HOME/goopdl`, `~/.config/goopdl` | `export GOOPDL_CONFIG_DIR=~/.config/goopdl` keeps old tokens |
| `download_app` with a busy token pool | `PlayAPIError` | `PoolBusyError` (a `TimeoutError`, not a `PlayAPIError`) | `except PoolBusyError`, or end with `except GoopdlError` |
| `typer` dependency | `typer>=0.9` | `typer>=0.16` | Raise your pin if it is lower |
| CDN failures | `IntegrityError("download failed …")` | `DownloadError`, a subclass of `IntegrityError` | `except DownloadError` before `except IntegrityError` |
| Transport failures in `api` | Raw httpx exceptions (`httpx.ConnectError`, …) | `NetworkError`, still an `httpx.TransportError` | `except NetworkError`; handlers for specific httpx subclasses no longer match |
| `purchase` on HTTP 429 | Returned `""` | Raises `RateLimitedError` (401 already raised `AuthExpiredError`) | Handle it like any other 429 |
| CDN redirects | Followed anywhere | At most five, Google hosts only | None, unless you rely on other hosts |
| Console output | `goopdl.auth` printed to stderr with Rich | Token functions log instead of printing | Configure [logging](#logging) |
| `goopdl.auth.console` / `Console` | Rich objects | Deprecated, `DeprecationWarning` | `rich.console.Console(stderr=True)` |
| `goopdl.download.Progress`, `BarColumn`, … | Re-exported from Rich | Deprecated, `DeprecationWarning` | `from rich.progress import Progress` |

New and backwards compatible: the `GoopdlError` base, `raise_on_error` on the
token functions, `download_files`, and `RateLimitedError` from `download_app`
and `fetch_app_item` where 1.4.x raised a plain `PlayAPIError`.

## Troubleshooting

| Symptom | Cause and fix |
| ------- | ------------- |
| `RuntimeError: download_batch() cannot run inside a running event loop` | Await `download_files` instead, see [async backends](#async-backends) |
| Progress bars in your server logs | `download_app` / `download_batch` draw them. Use `Client.download`, `Client.adownload`, or `download_files` |
| `pick_pool_token` returns `None` | Pass `raise_on_error=True` to get the typed reason. Still `None`: unknown profile or unusable cache directory (see the logs) |
| `PoolBusyError` | Another thread or process is filling the same pool. Retry later |
| Tokens are minted again after upgrading | The cache moved, see [migration](#migrating-from-14x) |
| `DirectAuthConfigurationError` | Set both `GOOPDL_ACCOUNT_EMAIL` and `GOOPDL_AAS_TOKEN`, or neither. From `Credentials`: pass both values, the token starting with `aas_et/` |
| A `Client` ignores `GOOPDL_ACCOUNT_EMAIL` | By design, see [credentials](#credentials). Use `Credentials.from_env()` |
| `DownloadError` with `status_code == 403` | The delivery URLs expired. Call `get_delivery` again |
| `AppNotAvailableError` for an app that exists | It is hidden from this device profile or country. Try another `profile`, `arch`, or `country` with a matching `proxy` |

## Known limits

- The snippets in this guide are checked against a mocked Google Play, on
  Linux. They are not run against the real service as part of that check, and
  the library is not tested on Windows or macOS.
- Delivery URLs on hosts outside the allowlist are dropped. If Google moves
  its CDN, `get_delivery` raises `VersionUnavailableError` until goopdl is
  updated.
- There is no async API for tokens and metadata: `Client.adownload` and
  `download_files` are the only coroutines. Use `asyncio.to_thread` for the
  rest.
- For the module functions, the cache directory and account credentials are
  per process (environment variables), not per call. A `Client` sets them per
  call.
- `Client.download` and `Client.adownload` only fetch the latest version. For
  a specific version code, use `Client.delivery` and `download_files`.
- The synchronous calls use goopdl's own per-process connection pool; you
  can't pass them a client. Only `download_files` and `Client.adownload`
  accept a shared client, for the CDN downloads.
