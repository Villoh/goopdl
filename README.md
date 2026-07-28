# gpdl

Download APKs from Google Play Store using anonymous authentication. Downloads base APKs, split APKs (App Bundles), OBB expansion files, and Play Asset Delivery packs — all by default.

> **Hard fork.** Independent project, hard-forked from [appknox/gplaydl](https://github.com/appknox/gplaydl), itself a fork of [rehmatworks/gplaydl](https://github.com/rehmatworks/gplaydl). MIT licensed, upstream copyright preserved.

## Install

```bash
pip install gpdl
```

## Features

- Anonymous authentication via Aurora Store's token dispenser (no Google account needed)
- Optional Google account AAS token retrieval without credential or token files
- Multiple device profiles with automatic rotation for reliable token acquisition
- Downloads base APK, split APKs, OBB files, and asset packs in one go
- Streaming gzip decompression for Play Asset Delivery packs
- Beautiful terminal UI with real-time download progress bars
- Architecture support: ARM64 (modern phones) and ARMv7 (older phones)
- Custom token dispenser URL support
- Search and browse app details from the command line
- Find the latest available version by sampling multiple fresh GSF IDs

## Deployment / Integration

For servers, Docker images, or any environment that uses `pip` directly:

```bash
pip install git+https://github.com/appknox/gpdl.git
```

In a **Dockerfile**:

```dockerfile
RUN pip3 install git+https://github.com/appknox/gpdl.git
```

To pin to a specific release tag or commit (recommended for production):

```dockerfile
RUN pip3 install git+https://github.com/appknox/gpdl.git@v2.0.0
# or by commit SHA:
RUN pip3 install git+https://github.com/appknox/gpdl.git@46d0b1f
```

In a **`requirements.txt`**:

```
git+https://github.com/appknox/gpdl.git
```

---

## Installation

**Requirements:** Python 3.9+, [uv](https://github.com/astral-sh/uv)

This package is not published to PyPI. Install directly from source:

```bash
git clone https://github.com/appknox/gpdl.git
cd gpdl
uv sync
uv pip install .
```

## Quick Start

```bash
# 1. Get an auth token (automatic, anonymous)
uv run gpdl auth

# 2. Download an app (base APK only)
uv run gpdl download com.whatsapp
```

All commands below use `uv run gpdl ...`. If you activate the virtual environment (`source .venv/bin/activate`) you can drop the `uv run` prefix.

## Commands

### `auth` — Acquire an authentication token

```bash
gpdl auth                                   # Default (arm64)
gpdl auth --arch armv7                      # Token for older ARM devices
gpdl auth -d https://my-server/api          # Use a custom dispenser
gpdl auth --clear                           # Remove all cached tokens
gpdl auth --country IN                      # Register device with India MCC/MNC
gpdl auth --proxy socks5://host:1080        # Route dispenser call through proxy
gpdl auth --profile "Galaxy S25 Ultra"      # Use a specific device profile
```

| Flag | Short | Default | Description |
| ------ | ------- | --------- | ------------- |
| `--arch` | | `arm64` | Architecture: `arm64` or `armv7` |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |
| `--clear` | | `false` | Remove all cached tokens |
| `--country` | `-c` | — | 2-letter country code; registers device with that region's MCC/MNC |
| `--proxy` | `-p` | — | Proxy URL for dispenser calls (e.g. `socks5://host:port`) |
| `--profile` | | — | Device profile key or name substring (e.g. `Pv` or `Samsung`). Run `gpdl profiles` to list all |

Tokens are cached at `~/.config/gpdl/auth-{arch}.json` and reused automatically by other commands.

#### Direct Google account authentication

Set both environment variables to bypass Aurora's dispenser and authenticate directly with Google:

```bash
export GPDL_ACCOUNT_EMAIL="account@example.com"
export GPDL_AAS_TOKEN="your-aas-token"
gpdl download com.whatsapp
```

No CLI flags, config files, or secret files are used. The AAS token is a persistent sensitive credential; changing the Google account password invalidates it. Temporary Google Play bearer tokens are kept in memory and never cached. If only one variable is configured, gpdl fails without falling back to anonymous authentication.

---

### `aastoken` — Print a Google account AAS token

#### Password or app-password mode

```bash
# Interactive: asks for email and hides password input
gpdl aastoken

# Arguments: convenient, but password remains in shell history
gpdl aastoken user@example.com password
```

Google commonly rejects both account passwords and 16-character app passwords with `BadAuthentication`. Keep 2-Step Verification enabled and use OAuth mode instead.

#### OAuth mode (recommended)

```bash
gpdl aastoken --oauth
```

Get the required one-time `oauth_token`:

1. Open [Google EmbeddedSetup](https://accounts.google.com/EmbeddedSetup).
2. Sign in and select **I agree**.
3. The page may remain frozen/loading forever. This is expected; the cookie is normally already available.
4. Open browser developer tools (`F12`).
5. In Chrome or Edge, open **Application → Cookies → `https://accounts.google.com`**.
6. Copy the value of the `oauth_token` cookie. It starts with `oauth2_4/`.
7. Run `gpdl aastoken --oauth`, enter the same email, then paste the token. Both inputs are handled interactively and the token is hidden.

The `oauth_token` is short-lived and single-use. The resulting AAS token is printed to the console. Credentials and tokens are used in memory only; no `credentials.txt` or `token.txt` files are created.

---

### `latest` — Find the latest available version

Probes multiple fresh GSF IDs (Google Services Framework IDs) from the token dispenser. Because Google stages rollouts by device cohort (tied to the GSF ID), sampling many IDs and taking the maximum version gives the most accurate latest version available.

```bash
gpdl latest com.instagram.android
gpdl latest com.instagram.android --stable 5
gpdl latest com.instagram.android --profile "Galaxy S25 Ultra"
gpdl latest com.instagram.android --country IN
gpdl latest com.instagram.android --proxy socks5://host:1080
gpdl latest com.instagram.android -s 4 -c US -p socks5://host:1080
```

| Flag | Short | Default | Description |
| ------ | ------- | --------- | ------------- |
| `--stable` | `-s` | `3` | Stop early when the highest version code is unchanged for this many consecutive probes |
| `--profile` | | top-ranked | Device profile for all probes (e.g. `Galaxy S25 Ultra`). Defaults to the highest SDK + Vending version profile |
| `--country` | `-c` | — | 2-letter country code sent with FDFE requests |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |
| `--proxy` | `-p` | — | Proxy URL for dispenser + FDFE calls (e.g. `socks5://host:port`) |
| `--arch` | | `arm64` | Architecture for token acquisition |

**How the pool works:** The tool maintains a regional pool of 5 GSF ID / token pairs on disk (`~/.config/gpdl/token-pool-{arch}-{country}.json`). Before every `latest` or `download` call, it checks how many valid (unexpired) tokens exist in that region's pool and fetches only the deficit from Aurora's dispenser — so if the pool is already full, zero dispenser calls are made. Tokens are valid for 50 minutes. If a token dies mid-request (HTTP 401), it is automatically replaced in the pool.

**How convergence works:** After each probe, if the best version code seen so far hasn't increased for `--stable` consecutive probes, the command stops early.

**Output:** A table with each probe's GSF ID prefix, version string, and version code. The highest version code is highlighted and printed as the final result with its version code.

---

### `download` — Download APKs

By default, `download` fetches the base APK, all split APKs, and any additional files (OBB expansion files, Play Asset Delivery packs).

```bash
gpdl download com.whatsapp                          # Everything (base + splits + extras)
gpdl download com.whatsapp -o ./apks                # Custom output directory
gpdl download com.whatsapp -a armv7                 # ARMv7 build
gpdl download com.whatsapp -v 231205015             # Specific version code
gpdl download com.instagram.android -v 434.0.0.44.74  # Specific version string
gpdl download com.whatsapp --no-splits              # Skip split APKs
gpdl download com.whatsapp --no-extras              # Skip OBB / asset packs
gpdl download com.whatsapp -d https://...           # Use custom dispenser
gpdl download com.whatsapp --country IN             # Country header for regional variant
gpdl download com.whatsapp --proxy socks5://host:1080  # Route through proxy
gpdl download com.whatsapp --profile "Galaxy S25 Ultra"  # Specific device profile
```

| Flag | Short | Default | Description |
| ------ | ------- | --------- | ------------- |
| `--output` | `-o` | `.` (current dir) | Output directory |
| `--arch` | `-a` | `arm64` | Architecture: `arm64` or `armv7` |
| `--version` | `-v` | latest | Version code (e.g. `384009971`) **or** version string (e.g. `434.0.0.44.74`). When a version string is given, the tool probes fresh GSF IDs until it finds a cohort that sees that version |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |
| `--no-splits` | | `false` | Skip downloading split APKs |
| `--no-extras` | | `false` | Skip downloading OBB files and asset packs |
| `--country` | `-c` | — | 2-letter country code (e.g. `IN`, `US`). Combine with `--proxy` for true regional APK variants |
| `--proxy` | `-p` | — | Proxy URL for FDFE calls (e.g. `socks5://host:port` or `http://host:port`) |
| `--profile` | | — | Device profile key or name substring (e.g. `D2` or `Samsung`). Run `gpdl profiles` to list all |

**Output files:**

| Type | Naming | Example |
| ------ | -------- | --------- |
| Base APK | `{package}-{vc}.apk` | `com.whatsapp-231205015.apk` |
| Split APK | `{package}-{vc}-{split}.apk` | `com.whatsapp-231205015-config.arm64_v8a.apk` |
| OBB (main/patch) | `{type}.{vc}.{package}.obb` | `main.20925.com.tencent.ig.obb` |
| Asset pack | `{package}-{vc}-asset.apk` | `com.tencent.ig-20925-asset.apk` |

Split APKs can be installed to a device with:

```bash
adb install-multiple *.apk
```

---

### `info` — Show app details

```bash
gpdl info com.whatsapp
gpdl info com.whatsapp --country IN
gpdl info com.whatsapp --proxy socks5://host:1080
gpdl info com.whatsapp --profile "Galaxy S25 Ultra"
```

| Flag | Short | Default | Description |
| ------ | ------- | --------- | ------------- |
| `--arch` | | `arm64` | Architecture for token |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |
| `--country` | `-c` | — | 2-letter country code; sets `gl=` and locale headers |
| `--proxy` | `-p` | — | Proxy URL for FDFE calls |
| `--profile` | | — | Device profile key or name substring |

Displays app name, version, developer, rating, download count, and Play Store URL.

---

### `search` — Search for apps

```bash
gpdl search "whatsapp"
gpdl search "file manager" --limit 5
gpdl search "whatsapp" --country IN
```

| Flag | Short | Default | Description |
| ------ | ------- | --------- | ------------- |
| `--limit` | `-l` | `10` | Max results |
| `--arch` | | `arm64` | Architecture for token |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |
| `--country` | `-c` | — | 2-letter country code for regional results |
| `--proxy` | `-p` | — | Proxy URL for FDFE calls |
| `--profile` | | — | Device profile key or name substring |

---

### `list-splits` — List available split APKs

```bash
gpdl list-splits com.whatsapp
```

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--arch` | | `arm64` | Architecture for token |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |

Shows all split APK names (config splits, language splits, etc.) without downloading.

---

### `profiles` — List device profiles

```bash
gpdl profiles           # All profiles
gpdl profiles --arch arm64   # ARM64 only
gpdl profiles --arch armv7   # ARMv7 only
```

Use the **Key** column value with `--profile` in any command.

## Running without installing globally

After `uv sync`, you can also invoke via the module directly:

```bash
uv run python -m gpdl auth
uv run python -m gpdl download com.whatsapp
```

## How It Works

1. **Authentication** — Gets an anonymous token from Aurora Store's dispenser, rotating through device profiles for reliability
2. **Details** — Fetches app metadata (version, size, splits) via Google Play's protobuf API
3. **Purchase** — "Purchases" the free app to get download authorization
4. **Delivery** — Gets download URLs for the base APK, split APKs, OBB files, and asset packs
5. **Download** — Streams all files in parallel from Google Play CDN with progress tracking

## Finding the Latest Version

Google stages app rollouts by device cohort, which is determined by the GSF ID (Google Services Framework ID) — a device registration number assigned during token acquisition. Different GSF IDs may see different active versions (e.g. 434 vs 435 for the same app).

The `latest` command exploits this by sampling multiple fresh GSF IDs and reporting the highest version code seen:

```bash
gpdl latest com.instagram.android --probes 10 --stable 3
```

- Each probe fetches a fresh token → fresh GSF ID → queries Play Store for that cohort's version
- Probing stops early once the max version has been stable for `--stable` consecutive probes
- The device profile (`--profile`) affects which SDK level is presented; higher SDK profiles tend to receive new versions first

## Token Dispenser

The tool uses [Aurora Store's](https://auroraoss.com/) public token dispenser by default (`https://auroraoss.com/api/auth`). This service provides anonymous Google Play authentication tokens — no personal Google account required.

You can point to a custom/self-hosted dispenser with the `--dispenser` / `-d` flag on any command:

```bash
gpdl auth -d https://my-dispenser.example.com/api/auth
gpdl download com.whatsapp -d https://my-dispenser.example.com/api/auth
```

## Device Profiles

The tool includes multiple device profiles, used to authenticate with Google Play's token dispenser. Profiles are rotated automatically during token acquisition to maximise compatibility.

Run `gpdl profiles` to see all available profiles and their keys. Use `--profile` with any command to pin a specific device.

Profiles are stored as `.properties` files in the `gpdl/profiles/` directory.

## Architecture Support

| Flag | ABI | Devices |
|------|-----|---------|
| `arm64` (default) | arm64-v8a | Modern phones (2017+) |
| `armv7` | armeabi-v7a | Older 32-bit phones |

### Verified integrity manifest

`download --integrity-manifest PATH` fails unless every requested base/split APK has
Google delivery size and digest metadata. SHA-256 is preferred; SHA-1 is used only
when SHA-256 is absent. Files are verified before atomic publication, then manifest
is written atomically with relative filename, size, algorithm, and Base64url digest.
Manifest never contains download URLs, cookies, headers, or auth tokens.
