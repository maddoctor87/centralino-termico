#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
PORT=${1:-$("$SCRIPT_DIR/find-plc-port.sh")}

if command -v tio >/dev/null 2>&1; then
    exec tio -b 115200 "$PORT"
fi

exec picocom -b 115200 "$PORT"
