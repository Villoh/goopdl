#!/usr/bin/env bash
# Build the onefile binary dist/goopdl (dist/goopdl.exe on Windows) with the
# `python` on PATH. CI and the release both use this script. Extra arguments
# go to PyInstaller, e.g. --distpath /tmp/dist.
set -euo pipefail
cd "$(dirname "$0")/.."

python -m pip install pyinstaller .
# keyring loads its backends by name and lists them as entry points in its
# metadata. PyInstaller's keyring hook collects both today; the two flags
# keep the binary working if that hook changes.
python -m PyInstaller --onefile --name goopdl --collect-data goopdl \
  --collect-submodules keyring.backends --copy-metadata keyring \
  "$@" goopdl/__main__.py
