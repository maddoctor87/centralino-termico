#!/usr/bin/env bash
set -euo pipefail

for candidate in /dev/serial/by-id/* /dev/ttyUSB0 /dev/ttyUSB1 /dev/ttyACM0 /dev/ttyACM1; do
    if [ -e "$candidate" ]; then
        printf '%s\n' "$candidate"
        exit 0
    fi
done

echo "PLC serial port not found" >&2
exit 1
