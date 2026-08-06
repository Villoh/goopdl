# goopdl

Download APKs from Google Play Store using anonymous authentication. Downloads base APKs, split APKs (App Bundles), OBB expansion files, and Play Asset Delivery packs — all by default.

> **Hard fork.** Independent project, hard-forked from [appknox/gplaydl](https://github.com/appknox/gplaydl), itself a fork of [rehmatworks/gplaydl](https://github.com/rehmatworks/gplaydl). MIT licensed, upstream copyright preserved.

## Install

As a global CLI tool (recommended — isolated environment, `goopdl` on your PATH):

```bash
uv tool install goopdl
# or
pipx install goopdl
```

Run it once without installing:

```bash
uvx goopdl --help
```

As a library or into the current environment:

```bash
pip install goopdl
```

## Features

- Anonymous authentication via Aurora Store's token dispenser (no Google account needed)
- Optional Google account AAS token retrieval without credential or token files
- Multiple device profiles with automatic rotation for reliable token acquisition
- Downloads base APK, split APKs, OBB files, and asset packs in one go
- Streaming gzip decompression for Play Asset Delivery packs
- Beautiful terminal UI with real-time download progress bars
- Device support: ARM64, ARMv7, x86, x86_64, and Android TV
- Custom token dispenser URL support
- Search and browse app details from the command line
- Find the latest available version by sampling multiple fresh GSF IDs

## Deployment / Integration

In a **Dockerfile**, pin an exact version (recommended for production):

```dockerfile
RUN pip3 install goopdl==1.0.0
```

In a **`requirements.txt`**:

```
goopdl==1.0.0
```

To track unreleased changes, install straight from git:

```bash
pip install git+https://github.com/Villoh/goopdl.git
```

---

## From source

**Requirements:** Python 3.9+, [uv](https://github.com/astral-sh/uv)

```bash
git clone https://github.com/Villoh/goopdl.git
cd goopdl
uv sync
uv pip install .
```

## Quick Start

```bash
# 1. Get an auth token (automatic, anonymous)
uv run goopdl auth

# 2. Download an app (base APK only)
uv run goopdl download com.whatsapp
```

All commands below use `uv run goopdl ...`. If you activate the virtual environment (`source .venv/bin/activate`) you can drop the `uv run` prefix.

## Commands

### `auth` — Acquire an authentication token

```bash
goopdl auth                                   # Default (arm64)
goopdl auth --arch armv7                      # Token for older ARM devices
goopdl auth -d https://my-server/api          # Use a custom dispenser
goopdl auth --clear                           # Remove all cached tokens
goopdl auth --country IN                      # Register device with India MCC/MNC
goopdl auth --proxy socks5://host:1080        # Route dispenser call through proxy
goopdl auth --profile "Galaxy S25 Ultra"      # Use a specific device profile
```

| Flag | Short | Default | Description |
| ------ | ------- | --------- | ------------- |
| `--arch` | | `arm64` | Device type: `arm64`, `armv7`, `x86`, `x86_64`, or `tv` |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |
| `--clear` | | `false` | Remove all cached tokens |
| `--country` | `-c` | — | 2-letter country code; registers device with that region's MCC/MNC |
| `--proxy` | `-p` | — | Proxy URL for dispenser calls (e.g. `socks5://host:port`) |
| `--profile` | | — | Device profile key or name substring (e.g. `Pv` or `Samsung`). Run `goopdl profiles` to list all |

Tokens are cached at `~/.config/goopdl/auth-{arch}.json` and reused automatically by other commands.

#### Direct Google account authentication

Set both environment variables to bypass Aurora's dispenser and authenticate directly with Google:

```bash
export GOOPDL_ACCOUNT_EMAIL="account@example.com"
export GOOPDL_AAS_TOKEN="your-aas-token"
goopdl download com.whatsapp
```

No CLI flags, config files, or secret files are used. The AAS token is a persistent sensitive credential; changing the Google account password invalidates it. Temporary Google Play bearer tokens are kept in memory and never cached. If only one variable is configured, goopdl fails without falling back to anonymous authentication.

---

### `aastoken` — Print a Google account AAS token

Use a throwaway Google account, not your main one. The AAS token grants full
Play Store device access under that account.

#### Password or app-password mode

```bash
# Interactive: asks for email and hides password input
goopdl aastoken

# Arguments: convenient, but password remains in shell history
goopdl aastoken user@example.com password
```

Google commonly rejects both account passwords and 16-character app passwords with `BadAuthentication`. Keep 2-Step Verification enabled and use OAuth mode instead.

#### Browser OAuth mode (recommended)

```bash
goopdl aastoken --browser
```

Sign in and select **I agree** in the isolated Chrome, Edge, Chromium, or Brave
window that opens. goopdl captures the signed-in email and one-time
`oauth_token`, closes the temporary browser profile, and exchanges them for the
AAS token. Set `GOOPDL_BROWSER` to the browser executable if it is not detected
automatically.

The browser profile, cookies, credentials, and tokens are never reused or saved
by goopdl. The resulting AAS token is printed to the console.

#### Manual OAuth mode

```bash
goopdl aastoken --oauth
```

If browser automation is unavailable:

1. Open [Google EmbeddedSetup](https://accounts.google.com/EmbeddedSetup).
2. Sign in and select **I agree**.
3. Open browser developer tools (`F12`).
4. In Chrome or Edge, open **Application → Cookies →
   `https://accounts.google.com`**.
5. Copy the `oauth_token` cookie value, which starts with `oauth2_4/`.
6. Run `goopdl aastoken --oauth`, enter the same email, then paste the token.

The `oauth_token` is short-lived and single-use. Credentials and tokens are
used in memory only; no `credentials.txt` or `token.txt` files are created.

---

### `latest` — Find the latest available version

Probes multiple fresh GSF IDs (Google Services Framework IDs) from the token dispenser. Because Google stages rollouts by device cohort (tied to the GSF ID), sampling many IDs and taking the maximum version gives the most accurate latest version available.

```bash
goopdl latest com.instagram.android
goopdl latest com.instagram.android --stable 5
goopdl latest com.instagram.android --profile "Galaxy S25 Ultra"
goopdl latest com.instagram.android --country IN
goopdl latest com.instagram.android --proxy socks5://host:1080
goopdl latest com.instagram.android -s 4 -c US -p socks5://host:1080
```

| Flag | Short | Default | Description |
| ------ | ------- | --------- | ------------- |
| `--stable` | `-s` | `3` | Stop early when the highest version code is unchanged for this many consecutive probes |
| `--profile` | | top-ranked | Device profile for all probes (e.g. `Galaxy S25 Ultra`). Defaults to the highest SDK + Vending version profile |
| `--country` | `-c` | — | 2-letter country code sent with FDFE requests |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |
| `--proxy` | `-p` | — | Proxy URL for dispenser + FDFE calls (e.g. `socks5://host:port`) |
| `--arch` | | `arm64` | Architecture for token acquisition |

**How the pool works:** The tool maintains a regional pool of 5 GSF ID / token pairs on disk (`~/.config/goopdl/token-pool-{arch}-{country}.json`). Before every `latest` or `download` call, it checks how many valid (unexpired) tokens exist in that region's pool and fetches only the deficit from Aurora's dispenser — so if the pool is already full, zero dispenser calls are made. Tokens are valid for 50 minutes. If a token dies mid-request (HTTP 401), it is automatically replaced in the pool.

**How convergence works:** After each probe, if the best version code seen so far hasn't increased for `--stable` consecutive probes, the command stops early.

**Output:** A table with each probe's GSF ID prefix, version string, and version code. The highest version code is highlighted and printed as the final result with its version code.

---

### `inspect-delivery` — Inspect version delivery without downloading

Fetches Google Play delivery metadata for an exact version code without
requesting APK files. Use this to confirm that a device profile receives a
compatible ABI before starting a large download.

```bash
goopdl inspect-delivery com.whatsapp --arch armv7 --version 231205015 --json
goopdl inspect-delivery com.whatsapp --arch arm64 --version 231205015 \
  --output delivery.json
```

Example JSON:

```json
{
  "architecture": "armv7",
  "package": "com.whatsapp",
  "splits": ["base", "config.armeabi_v7a", "config.en"],
  "version": {"code": 231205015, "name": "2.23.26.15"}
}
```

| Flag | Short | Default | Description |
| ------ | ------- | --------- | ----------- |
| `--version` | `-v` | required | Exact Google Play version code |
| `--arch` | | `arm64` | Device architecture |
| `--json` | | `false` | Print machine-readable JSON |
| `--output` | `-o` | — | Write JSON to a file instead of stdout |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |
| `--country` | `-c` | — | 2-letter country code |
| `--proxy` | `-p` | — | Proxy URL for FDFE calls |
| `--profile` | | — | Device profile key or name substring |

The command performs authentication, version purchase, and delivery metadata
lookup. It does not download or publish APK files.

---

### `download` — Download APKs

By default, `download` fetches the base APK, all split APKs, and any additional files (OBB expansion files, Play Asset Delivery packs).

```bash
goopdl download com.whatsapp                          # Everything (base + splits + extras)
goopdl download com.whatsapp -o ./apks                # Custom output directory
goopdl download com.whatsapp -a armv7                 # ARMv7 build
goopdl download com.whatsapp -a arm64,armv7           # Several device builds
goopdl download com.google.android.katniss -a tv      # Android TV build
goopdl download com.whatsapp -v 231205015             # Specific version code
goopdl download com.instagram.android -v 434.0.0.44.74  # Specific version string
goopdl download com.whatsapp -l de,fr,zh-CN           # Extra language splits
goopdl download com.whatsapp --dm                     # DEX metadata for faster first launch
goopdl download com.whatsapp --no-splits              # Skip split APKs
goopdl download com.whatsapp --no-extras              # Skip OBB / asset packs
goopdl download com.whatsapp -d https://...           # Use custom dispenser
goopdl download com.whatsapp --country IN             # Country header for regional variant
goopdl download com.whatsapp --proxy socks5://host:1080  # Route through proxy
goopdl download com.whatsapp --profile "Galaxy S25 Ultra"  # Specific device profile
```

| Flag | Short | Default | Description |
| ------ | ------- | --------- | ------------- |
| `--output` | `-o` | `.` (current dir) | Output directory |
| `--arch` | `-a` | `arm64` | Device type(s): `arm64`, `armv7`, `x86`, `x86_64`, or `tv`; comma-separated |
| `--version` | `-v` | latest | Version code (e.g. `384009971`) **or** version string (e.g. `434.0.0.44.74`). When a version string is given, the tool probes fresh GSF IDs until it finds a cohort that sees that version |
| `--locale` | `-l` | English | Extra language splits, comma-separated (e.g. `de,fr,zh-CN`) |
| `--dm` | | `false` | Download verified DEX metadata (`.dm`) used to speed up first launch |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |
| `--no-splits` | | `false` | Skip downloading split APKs |
| `--no-extras` | | `false` | Skip downloading OBB files and asset packs |
| `--country` | `-c` | — | 2-letter country code (e.g. `IN`, `US`). Combine with `--proxy` for true regional APK variants |
| `--proxy` | `-p` | — | Proxy URL for FDFE calls (e.g. `socks5://host:port` or `http://host:port`) |
| `--profile` | | — | Device profile key or name substring (e.g. `D2` or `Samsung`). Run `goopdl profiles` to list all |

**Output files:**

| Type | Naming | Example |
| ------ | -------- | --------- |
| Base APK | `{package}-{vc}.apk` | `com.whatsapp-231205015.apk` |
| Split APK | `{package}-{vc}-{split}.apk` | `com.whatsapp-231205015-config.arm64_v8a.apk` |
| OBB (main/patch) | `{type}.{vc}.{package}.obb` | `main.20925.com.tencent.ig.obb` |
| Asset pack | `{package}-{vc}-asset.apk` | `com.tencent.ig-20925-asset.apk` |

Split APKs can be installed to a device with:

```bash
adb install-multiple com.whatsapp-231205015*.apk
```

Use package/version prefix shown by CLI. Bare `*.apk` can mix unrelated apps or versions.

#### Google Play rate limits

Google Play may temporarily return HTTP 429 while acquiring delivery URLs. goopdl retries delivery acquisition up to two times with bounded backoff; APK file downloads are not retried. Persistent throttling exits with code `5`, while an unavailable version (HTTP 404 or confirmed unavailable response) exits with code `4`. Retry later or use a different provider, account, or network if throttling persists.

---

### `info` — Show app details

```bash
goopdl info com.whatsapp
goopdl info com.whatsapp --country IN
goopdl info com.whatsapp --proxy socks5://host:1080
goopdl info com.whatsapp --profile "Galaxy S25 Ultra"
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
goopdl search "whatsapp"
goopdl search "file manager" --limit 5
goopdl search "whatsapp" --country IN
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
goopdl list-splits com.whatsapp
```

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--arch` | | `arm64` | Architecture for token |
| `--dispenser` | `-d` | Aurora Store | Custom token dispenser URL |

Shows all split APK names (config splits, language splits, etc.) without downloading.

---

### `profiles` — List device profiles

```bash
goopdl profiles           # All profiles
goopdl profiles --arch arm64   # ARM64 only
goopdl profiles --arch armv7   # ARMv7 only
```

Use the **Key** column value with `--profile` in any command.

## Running without installing globally

After `uv sync`, you can also invoke via the module directly:

```bash
uv run python -m goopdl auth
uv run python -m goopdl download com.whatsapp
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
goopdl latest com.instagram.android --probes 10 --stable 3
```

- Each probe fetches a fresh token → fresh GSF ID → queries Play Store for that cohort's version
- Probing stops early once the max version has been stable for `--stable` consecutive probes
- The device profile (`--profile`) affects which SDK level is presented; higher SDK profiles tend to receive new versions first

## Token Dispenser

The tool uses [Aurora Store's](https://auroraoss.com/) public token dispenser by default (`https://auroraoss.com/api/auth`). This service provides anonymous Google Play authentication tokens — no personal Google account required.

You can point to a custom/self-hosted dispenser with the `--dispenser` / `-d` flag on any command:

```bash
goopdl auth -d https://my-dispenser.example.com/api/auth
goopdl download com.whatsapp -d https://my-dispenser.example.com/api/auth
```

## Device Profiles

The tool includes multiple device profiles, used to authenticate with Google Play's token dispenser. Profiles are rotated automatically during token acquisition to maximise compatibility.

Run `goopdl profiles` to see all available profiles and their keys. Use `--profile` with any command to pin a specific device.

Profiles are stored as `.properties` files in the `goopdl/profiles/` directory.

## Architecture Support

| Flag | ABI | Devices |
| ------ | ----- | --------- |
| `arm64` (default) | arm64-v8a | Modern phones (2017+) |
| `armv7` | armeabi-v7a | Older 32-bit phones |
| `x86` | x86 | Intel/AMD 32-bit emulators, devices |
| `x86_64` | x86_64 | Intel/AMD 64-bit emulators, devices |
| `tv` | arm64-v8a / armeabi-v7a | Android TV boxes, streaming devices |

Multiple values accepted comma-separated (e.g. `-a arm64,armv7`) — `download` retries
each until one succeeds.

### Verified integrity manifest

`download --integrity-manifest PATH` fails unless every requested base/split APK has
Google delivery size and digest metadata. SHA-256 is preferred; SHA-1 is used only
when SHA-256 is absent. Files are verified before atomic publication, then manifest
is written atomically with relative filename, size, algorithm, and Base64url digest.
Manifest never contains download URLs, cookies, headers, or auth tokens.
