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

FILES=(
    config.py
    main.py
    actuators.py
    sensors.py
    control_panels.py
    control_c2.py
    control_recirc.py
    control_aux.py
    comms_mqtt.py
    diagnostics.py
    state.py
)

for file in "${FILES[@]}"; do
    if [ -f "$ROOT_DIR/$file" ]; then
        echo "sync $file"
        "$MPREMOTE" connect "$PORT" fs cp "$ROOT_DIR/$file" ":$file"
    fi
done

exec "$MPREMOTE" connect "$PORT" reset
