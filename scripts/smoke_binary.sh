#!/usr/bin/env bash
# Smoke-test a goopdl binary: it starts, and keyring is bundled with its
# backends. Never touches a real secret store or the user's config: it uses a
# temporary config directory and the null keyring backend, which stores
# nothing.
# Usage: scripts/smoke_binary.sh path/to/goopdl[.exe]
set -euo pipefail

binary=$1
config=$(mktemp -d)
trap 'rm -rf "$config"' EXIT
if command -v cygpath > /dev/null; then
  config=$(cygpath -m "$config") # Git Bash: the binary needs C:/... paths
fi
export GOOPDL_CONFIG_DIR=$config PYTHON_KEYRING_BACKEND=keyring.backends.null.Keyring
unset GOOPDL_ACCOUNT_EMAIL GOOPDL_AAS_TOKEN

# check CODE TEXT COMMAND...: exit code CODE, TEXT in the output, no traceback.
check() {
  local code=$1 expected=$2 status=0 output flat
  shift 2
  echo "\$ $*"
  output=$("$@" 2>&1) || status=$?
  printf '%s\n' "$output"
  # Rich wraps long lines; compare with all whitespace squeezed to one space.
  flat=$(printf '%s' "$output" | tr -s ' \r\n\t' ' ')
  if [[ $status != "$code" || $flat != *"$expected"* || $flat == *Traceback* ]]; then
    echo "smoke test failed: expected exit code $code and \"$expected\"" >&2
    exit 1
  fi
}

check 0 "goopdl" "$binary" --version
check 0 "anonymous" "$binary" auth status

# The token of this saved account is in the keyring, which the null backend
# never has: goopdl must load keyring and that backend, then say so cleanly.
printf '{"v": 1, "email": "smoke@example.test", "storage": "keyring"}' \
  > "$config/account.json"
chmod 600 "$config/account.json"
check 0 "saved account (unusable)" "$binary" auth status
check 0 "Storage : system keyring (null Keyring)" "$binary" auth status
check 2 "token is not in the system keyring" "$binary" auth token
