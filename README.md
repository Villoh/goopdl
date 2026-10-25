<p align="center">
  <img src="https://raw.githubusercontent.com/Villoh/goopdl/main/docs/assets/logo.svg" alt="goopdl" width="720">
</p>

<p align="center">
  Download APKs from Google Play from the command line.<br>
  No Google account needed. Split APKs, OBB files, and asset packs included.
</p>

<p align="center">
  <a href="https://pypi.org/project/goopdl/"><img src="https://img.shields.io/pypi/v/goopdl?style=flat-square&logo=pypi&logoColor=white&color=00b0ff" alt="PyPI version"></a>
  <a href="https://pypi.org/project/goopdl/"><img src="https://img.shields.io/badge/python-3.10%2B-7c4dff?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://github.com/Villoh/goopdl/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Villoh/goopdl/ci.yml?branch=main&style=flat-square&logo=github&label=CI" alt="CI status"></a>
  <a href="https://pypistats.org/packages/goopdl"><img src="https://img.shields.io/pypi/dm/goopdl?style=flat-square&color=00e5a0" alt="Monthly downloads"></a>
  <a href="https://github.com/Villoh/goopdl/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-00e5a0?style=flat-square" alt="MIT license"></a>
</p>

<p align="center">
  <b>English</b> · <a href="https://github.com/Villoh/goopdl/blob/main/README.es.md">Español</a>
</p>

---

## Features

- **No account required.** Anonymous tokens come from Aurora Store's public
  dispenser. You can [sign in with a Google account](https://github.com/Villoh/goopdl/blob/main/docs/authentication.md#google-account)
  if you prefer.
- **The whole app.** Base APK, split APKs, OBB expansion files, and Play Asset
  Delivery packs.
- **Any version.** Download by version code or version string, or find the
  newest one Google is rolling out with `goopdl latest`.
- **Any device.** ARM64, ARMv7, x86, x86_64, and Android TV, with bundled
  device profiles that rotate automatically.
- **Regional builds.** Combine `--country` and `--proxy` to get a specific
  region's variant.
- **Verified downloads.** Optionally check every file against Google's
  digests and write an integrity manifest.

## Install

```bash
uv tool install goopdl    # or: pipx install goopdl
```

Or run it once without installing: `uvx goopdl --help`.
[More install options](https://github.com/Villoh/goopdl/blob/main/docs/installation.md),
including pinned versions and building from source.

## Quick start

```bash
# Download the latest base APK
goopdl download com.whatsapp

# Download the full app: base, split APKs, OBB files, and asset packs
goopdl download com.whatsapp --splits --extras -o ./apks
```

After a split download, goopdl prints the exact `adb install-multiple`
command to install it on a connected device.

goopdl fetches an anonymous token on first use. You don't need to run
`goopdl auth`.

More examples:

```bash
goopdl search "file manager"                     # find a package name
goopdl info com.whatsapp                         # app details
goopdl latest com.instagram.android              # newest version in rollout
goopdl download com.whatsapp -v 231205015        # a specific version
goopdl download com.whatsapp -a armv7            # a 32-bit ARM build
```

## Documentation

| Guide | Covers |
| ----- | ------ |
| [Installation](https://github.com/Villoh/goopdl/blob/main/docs/installation.md) | uv, pipx, pip, Docker, building from source |
| [Command reference](https://github.com/Villoh/goopdl/blob/main/docs/commands.md) | Every command and flag, output files, exit codes |
| [Authentication](https://github.com/Villoh/goopdl/blob/main/docs/authentication.md) | Anonymous tokens, custom dispensers, Google account mode, AAS tokens |
| [How it works](https://github.com/Villoh/goopdl/blob/main/docs/how-it-works.md) | Download pipeline, token pool, staged rollouts, device profiles |

## Credits

goopdl is an independent hard fork of
[appknox/gplaydl](https://github.com/appknox/gplaydl), itself a fork of
[rehmatworks/gplaydl](https://github.com/rehmatworks/gplaydl). Anonymous
authentication relies on [Aurora Store's](https://auroraoss.com/) token
dispenser.

## License

[MIT](https://github.com/Villoh/goopdl/blob/main/LICENSE). Upstream copyright
is preserved.
