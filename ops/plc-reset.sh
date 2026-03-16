#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
PORT=${1:-$("$SCRIPT_DIR/find-plc-port.sh")}
MPREMOTE="$ROOT_DIR/.venv-tools/bin/mpremote"

if [ ! -x "$MPREMOTE" ]; then
    echo "mpremote not found at $MPREMOTE" >&2
    exit 1
fi

exec "$MPREMOTE" connect "$PORT" reset
