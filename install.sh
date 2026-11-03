#!/bin/sh
# Install goopdl from a GitHub release. Never edits your shell config or PATH.
#   curl -fsSL https://villoh.github.io/goopdl/install.sh | sh
# Env: GOOPDL_VERSION (e.g. 1.3.1, default latest), GOOPDL_INSTALL_DIR (default ~/.local/bin)
set -eu

repo=Villoh/goopdl
dir=${GOOPDL_INSTALL_DIR:-$HOME/.local/bin}

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=macos ;;
  *) echo "Unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=x86_64 ;;
  aarch64 | arm64) arch=arm64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
asset=goopdl-$os-$arch

if [ -n "${GOOPDL_VERSION:-}" ]; then
  base=${GOOPDL_BASE_URL:-https://github.com/$repo/releases/download/v${GOOPDL_VERSION#v}}
else
  base=${GOOPDL_BASE_URL:-https://github.com/$repo/releases/latest/download}
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "Downloading $asset..."
curl -fsSL "$base/$asset" -o "$tmp/$asset" || {
  echo "Could not download $asset (not in this release?). Try: uv tool install goopdl" >&2
  exit 1
}
curl -fsSL "$base/SHA256SUMS" -o "$tmp/SHA256SUMS"

want=$(awk -v f="$asset" '$2 == f { print $1 }' "$tmp/SHA256SUMS")
[ -n "$want" ] || { echo "$asset not listed in SHA256SUMS" >&2; exit 1; }
if command -v sha256sum >/dev/null 2>&1; then
  got=$(sha256sum "$tmp/$asset" | cut -d' ' -f1)
else
  got=$(shasum -a 256 "$tmp/$asset" | cut -d' ' -f1)
fi
[ "$got" = "$want" ] || { echo "Checksum mismatch for $asset" >&2; exit 1; }

mkdir -p "$dir"
chmod +x "$tmp/$asset"
mv "$tmp/$asset" "$dir/goopdl"
echo "Installed $("$dir/goopdl" --version) to $dir/goopdl"

case ":$PATH:" in
  *":$dir:"*) ;;
  *) echo "Note: $dir is not in your PATH. Add it, e.g.: export PATH=\"$dir:\$PATH\"" ;;
esac
