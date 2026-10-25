# Command reference

**English** · [Español](es/commands.md)

Run `goopdl COMMAND --help` for the same information in your terminal.

| Command | What it does |
| ------- | ------------ |
| [`download`](#download) | Download an app's APKs |
| [`fast-download`](#fast-download) | Download the latest version with a single token |
| [`latest`](#latest) | Find the newest version Google is rolling out |
| [`inspect-delivery`](#inspect-delivery) | Show what a version would deliver, without downloading |
| [`info`](#info) | Show app details |
| [`search`](#search) | Search Google Play |
| [`list-splits`](#list-splits) | List an app's split APKs |
| [`profiles`](#profiles) | List bundled device profiles |
| [`auth`](#auth) | Get and cache an anonymous token |
| [`aastoken`](#aastoken) | Print a Google account AAS token |

## Common flags

Most commands that talk to Google Play accept these:

| Flag | Short | Description |
| ---- | ----- | ----------- |
| `--arch` | | Device type: `arm64` (default), `armv7`, `x86`, `x86_64`, or `tv`. See [architectures](how-it-works.md#architectures) |
| `--country` | `-c` | 2-letter country code, such as `US` or `IN` |
| `--proxy` | `-p` | Proxy URL, such as `socks5://host:1080` or `http://host:8080` |
| `--profile` | | Device profile key or name substring, such as `D2` or `Samsung`. See [`profiles`](#profiles) |
| `--dispenser` | `-d` | Custom token dispenser URL. See [custom dispenser](authentication.md#custom-dispenser) |

`--country` only sets region headers. To get a region's actual APK variant,
also route traffic through a proxy in that region with `--proxy`.

---

## `download`

Downloads the base APK. Add `--splits` for split APKs and `--extras` for OBB
files and Play Asset Delivery packs.

```bash
goopdl download com.whatsapp                            # base APK, latest version
goopdl download com.whatsapp --splits                   # + split APKs
goopdl download com.whatsapp --splits --extras          # + OBB files and asset packs
goopdl download com.whatsapp -o ./apks                  # custom output directory
goopdl download com.whatsapp -a arm64,armv7             # one download per architecture
goopdl download com.google.android.katniss -a tv        # Android TV build
goopdl download com.whatsapp -v 231205015               # exact version code
goopdl download com.instagram.android -v 434.0.0.44.74  # exact version string
goopdl download com.whatsapp --splits -l de,fr,zh-CN    # extra language splits
goopdl download com.whatsapp --dm                       # DEX metadata for faster first launch
goopdl download com.whatsapp -c IN -p socks5://host:1080  # regional variant
```

| Flag | Short | Default | Description |
| ---- | ----- | ------- | ----------- |
| `--output` | `-o` | `.` | Output directory |
| `--arch` | `-a` | `arm64` | Device type. Comma-separate several to download each in turn; goopdl stops at the first one that fails |
| `--version` | `-v` | latest | Version code (`384009971`) or version string (`434.0.0.44.74`). For a version string, goopdl probes fresh GSF IDs until one sees that version |
| `--splits` / `--no-splits` | | `--no-splits` | Include split APKs |
| `--extras` / `--no-extras` | | `--no-extras` | Include OBB files and asset packs |
| `--locale` | `-l` | English | Extra language splits, comma-separated. Needs `--splits` |
| `--dm` | | off | Download verified DEX metadata (`.dm`) |
| `--integrity-manifest` | | — | Write a verified JSON manifest. See [integrity manifest](#integrity-manifest) |

Also accepts the [common flags](#common-flags).

### Output files

| Type | Name | Example |
| ---- | ---- | ------- |
| Base APK | `{package}-{vc}.apk` | `com.whatsapp-231205015.apk` |
| Split APK | `{package}-{vc}-{split}.apk` | `com.whatsapp-231205015-config.arm64_v8a.apk` |
| OBB (main/patch) | `{type}.{vc}.{package}.obb` | `main.20925.com.tencent.ig.obb` |
| Asset pack | `{package}-{vc}-asset.apk` | `com.tencent.ig-20925-asset.apk` |

`{vc}` is the version code. After a split download, goopdl prints an
`adb install-multiple` command listing exactly the APK and `.dm` files it
downloaded, quoted for your shell (PowerShell on Windows):

```bash
adb install-multiple com.whatsapp-231205015.apk com.whatsapp-231205015-config.arm64_v8a.apk
```

### Integrity manifest

`--integrity-manifest PATH` makes the download fail unless Google sends size
and digest metadata for every requested base and split APK. goopdl:

1. Verifies each file against that metadata before moving it into place.
   It prefers SHA-256 and uses SHA-1 only when SHA-256 is missing.
2. Writes the manifest atomically: relative file name, size, algorithm, and
   Base64url digest for each file.

With several architectures, one manifest lists the files of all of them.
Google sends no digests for OBB files and asset packs, so `--extras` files are
downloaded unverified and left out of the manifest. The manifest never
contains download URLs, cookies, headers, or tokens.

If a check fails, goopdl prints the reason and exits with code `1`.

### Rate limits

Google Play sometimes answers delivery requests with HTTP 429. goopdl retries
the delivery request up to twice with backoff. It does not retry file
downloads. If throttling continues, it exits with code `5`. Wait, or switch
account, network, or dispenser.

---

## `fast-download`

Downloads the latest version using one token: a valid pooled token for the
`--country` if one exists, otherwise a single fresh one. Unlike `download`, it
doesn't fill the 5-token pool or try several device profiles. Use it for
crawlers and other high-volume callers, where hitting the dispenser on every
call gets your IP blocked.

```bash
goopdl fast-download com.whatsapp -o ./apks --splits
```

| Flag | Short | Default | Description |
| ---- | ----- | ------- | ----------- |
| `--output` | `-o` | `.` | Output directory |
| `--arch` | `-a` | `arm64` | Device type (one only) |
| `--splits` / `--no-splits` | | `--no-splits` | Include split APKs |
| `--extras` / `--no-extras` | | `--no-extras` | Include OBB files and asset packs |
| `--profile` | | random | Device profile. Defaults to a random pick from the two newest profiles |

Also accepts `--country`, `--proxy`, and `--dispenser`. `--proxy` applies only
to Google Play calls; dispenser requests rotate proxy regions on their own.

---

## `latest`

Finds the newest version available by asking Google Play through several GSF
IDs and keeping the highest version code. [Finding the latest
version](how-it-works.md#finding-the-latest-version) explains why one device
isn't enough.

```bash
goopdl latest com.instagram.android
goopdl latest com.instagram.android --stable 5
goopdl latest com.instagram.android -s 4 -c US -p socks5://host:1080
```

| Flag | Short | Default | Description |
| ---- | ----- | ------- | ----------- |
| `--stable` | `-s` | `3` | Stop once the highest version code hasn't changed for this many probes in a row |
| `--profile` | | top-ranked | Device profile for every probe. Defaults to the one with the highest SDK and Play Store version |

Also accepts the [common flags](#common-flags). `--proxy` applies to both
dispenser and Google Play calls.

The output is a table with each probe's GSF ID prefix, version name, and
version code. The highest version is highlighted and printed last.

---

## `inspect-delivery`

Asks Google Play what it would deliver for an exact version code, without
downloading any APK. Use it to check that a device profile gets the ABI you
want before you start a large download.

```bash
goopdl inspect-delivery com.whatsapp --arch armv7 --version 231205015 --json
goopdl inspect-delivery com.whatsapp --version 231205015 -o delivery.json
```

```json
{
  "architecture": "armv7",
  "package": "com.whatsapp",
  "splits": ["base", "config.armeabi_v7a", "config.en"],
  "version": {"code": 231205015, "name": "2.23.26.15"}
}
```

| Flag | Short | Default | Description |
| ---- | ----- | ------- | ----------- |
| `--version` | `-v` | required | Exact version code |
| `--json` | | off | Print JSON |
| `--output` | `-o` | — | Write the JSON to a file instead of stdout |

Also accepts the [common flags](#common-flags). The command authenticates and
"purchases" the version (see [how it works](how-it-works.md#the-download-pipeline)),
but downloads nothing.

---

## `info`

Shows an app's name, version, developer, rating, download count, and Play
Store URL.

```bash
goopdl info com.whatsapp
goopdl info com.whatsapp -c IN -p socks5://host:1080
goopdl info com.whatsapp --raw    # full decoded protobuf response as JSON
```

| Flag | Short | Default | Description |
| ---- | ----- | ------- | ----------- |
| `--raw` | `-r` | off | Print the full decoded response as JSON |

Also accepts the [common flags](#common-flags).

---

## `search`

```bash
goopdl search "whatsapp"
goopdl search "file manager" --limit 5
goopdl search "whatsapp" --country IN
```

| Flag | Short | Default | Description |
| ---- | ----- | ------- | ----------- |
| `--limit` | `-l` | `10` | Maximum number of results |

Also accepts the [common flags](#common-flags).

---

## `list-splits`

Lists an app's split APKs (ABI, density, and language configs) without
downloading anything.

```bash
goopdl list-splits com.whatsapp
```

Accepts `--arch` and `--dispenser`.

---

## `profiles`

Lists the bundled device profiles. Pass a value from the **Key** column to
`--profile` in any command.

```bash
goopdl profiles              # every profile
goopdl profiles --arch arm64 # one architecture
```

`--arch` accepts `arm64`, `armv7`, `x86`, `x86_64`, `tv`, or `all` (default).

---

## `auth`

Gets an anonymous token from the dispenser and caches it. See
[authentication](authentication.md) for both auth modes.

```bash
goopdl auth
goopdl auth --arch armv7
goopdl auth --country IN
goopdl auth --profile "Galaxy S25 Ultra"
goopdl auth --clear
```

| Flag | Short | Default | Description |
| ---- | ----- | ------- | ----------- |
| `--clear` | | off | Delete every cached token |
| `--country` | `-c` | — | Register the device with that country's MCC/MNC, so later calls with the same `--country` see its catalog without a proxy |

Also accepts the [common flags](#common-flags).

---

## `aastoken`

Prints a Google account AAS token for
[Google account mode](authentication.md#google-account). It saves no
credentials or tokens.

```bash
goopdl aastoken --browser                 # recommended
goopdl aastoken --oauth                   # paste an oauth_token yourself
goopdl aastoken [EMAIL] [PASSWORD]        # password mode, usually rejected
```

| Flag | Description |
| ---- | ----------- |
| `--browser` | Open an isolated browser window and capture the login automatically |
| `--oauth` | Prompt for a one-time `oauth_token` instead of a password |

[Getting an AAS token](authentication.md#getting-an-aas-token) walks through
each mode.

---

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0` | Success |
| `1` | Other error |
| `2` | Invalid input or configuration, such as only one Google account variable set |
| `3` | Authentication failed |
| `4` | Version unavailable (HTTP 404 or confirmed unavailable) |
| `5` | Google Play rate limit persisted after retries |
