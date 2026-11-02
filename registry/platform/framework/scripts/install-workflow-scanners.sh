#!/usr/bin/env bash
# Install pinned, hash-verified workflow security scanners into a bin dir.
#
#   bash scripts/install-workflow-scanners.sh "$RUNNER_TEMP/bin"
#
# zizmor (workflow security audits) and actionlint (workflow correctness).
# Versions and hashes are pinned here; bump both together and regenerate
# config/zizmor-baseline.json, because new zizmor audits change the findings.
# Linux x86_64 only (GitHub-hosted ubuntu-* runners).
set -euo pipefail

DEST="${1:?usage: install-workflow-scanners.sh <bin-dir>}"
ZIZMOR_VERSION="1.31.1"
ZIZMOR_WHEEL_SHA256="58e878eb2dbd2c6183c105e1ac28c14f7dd1eca44a783f1fe2a69647d012c410"
ACTIONLINT_VERSION="1.7.7"
ACTIONLINT_TGZ_SHA256="023070a287cd8cccd71515fedc843f1985bf96c436b7effaecce67290e7e0757"

if [[ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]]; then
  echo "install-workflow-scanners: Linux x86_64 only (got $(uname -s)-$(uname -m))" >&2
  exit 2
fi

mkdir -p "$DEST"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

printf 'zizmor==%s --hash=sha256:%s\n' "$ZIZMOR_VERSION" "$ZIZMOR_WHEEL_SHA256" > "$tmp/req.txt"
python3 -m pip install --quiet --disable-pip-version-check --no-deps \
  --require-hashes --only-binary=:all: --target "$tmp/py" -r "$tmp/req.txt"
install -m 0755 "$tmp/py/bin/zizmor" "$DEST/zizmor"

curl -sSLf --max-time 60 -o "$tmp/actionlint.tgz" \
  "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz"
echo "${ACTIONLINT_TGZ_SHA256}  $tmp/actionlint.tgz" | sha256sum -c - >/dev/null
tar -xzf "$tmp/actionlint.tgz" -C "$tmp" actionlint
install -m 0755 "$tmp/actionlint" "$DEST/actionlint"

"$DEST/zizmor" --version
"$DEST/actionlint" -version | head -1
