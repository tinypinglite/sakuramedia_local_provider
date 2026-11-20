#!/usr/bin/env bash
set -euo pipefail

PLUGIN_ID="${1:?plugin_id}"
ARCH="${2:?arch}"
OUT_DIR="${3:?output_dir}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends gcc libc6-dev binutils file
python -m pip install --no-cache-dir -q -r .github/requirements-build.txt

python .github/scripts/nuitka_build.py \
  --plugin-id "$PLUGIN_ID" \
  --arch "$ARCH" \
  --output-dir "$OUT_DIR"
