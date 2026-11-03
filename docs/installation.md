# Installation

**English** · [Español](es/installation.md)

## Prebuilt binary (no Python needed)

Linux and macOS (x86_64 and arm64):

```bash
curl -fsSL https://villoh.github.io/goopdl/install.sh | sh
```

Windows (x86_64 and arm64), in PowerShell:

```powershell
irm https://villoh.github.io/goopdl/install.ps1 | iex
```

The script downloads the binary from the GitHub release, checks its SHA-256,
and copies it to `~/.local/bin` (Linux/macOS) or `%LOCALAPPDATA%\goopdl\bin`
(Windows). It never edits your `PATH`; it prints the command to add the
directory if needed. Set `GOOPDL_VERSION=1.3.1` to pin a version and
`GOOPDL_INSTALL_DIR` to change the destination. To update, run it again.

The rest of this page assumes Python 3.10 or newer.

## As a CLI tool

Install it into an isolated environment so `goopdl` lands on your `PATH`:

```bash
uv tool install goopdl
# or
pipx install goopdl
```

Try it once without installing anything:

```bash
uvx goopdl --help
```

## With Nix

The repository is a flake:

```bash
nix run github:Villoh/goopdl -- --help
```

To install it declaratively, add it as an input and use `inputs.goopdl.packages.${system}.default`.
`overlays.default` adds `goopdl` to every Python package set, for flakes that use it as a library.

## As a library

Install it into the current environment:

```bash
pip install goopdl
```

## Pinning a version

Pin an exact version in production. In a Dockerfile:

```dockerfile
RUN pip3 install goopdl==1.2.2
```

In `requirements.txt`:

```text
goopdl==1.2.2
```

To try unreleased changes, install from git:

```bash
pip install git+https://github.com/Villoh/goopdl.git
```

## From source

You need [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/Villoh/goopdl.git
cd goopdl
uv sync
uv run goopdl --help
```

`uv sync` installs goopdl into `.venv`. Prefix commands with `uv run`, or
activate the environment (`source .venv/bin/activate`) and call `goopdl`
directly. The module entry point works too:

```bash
uv run python -m goopdl download com.whatsapp
```

Run the test suite:

```bash
uv run python -m unittest discover -s tests
```
