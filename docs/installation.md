# Installation

**English** · [Español](es/installation.md)

goopdl needs Python 3.10 or newer.

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
