# Remote Ops

- `ops/find-plc-port.sh`: trova automaticamente la seriale del PLC
- `ops/plc-console.sh`: apre la console seriale a `115200`
- `ops/plc-repl.sh`: apre il REPL MicroPython via `mpremote`
- `ops/plc-sync.sh`: copia i file `.py` sul PLC e fa reset
- `ops/plc-reset.sh`: reset software del PLC

Prerequisiti sul portatile remoto:
- `python3`
- `tio` o `picocom`
- virtualenv `.venv-tools` con `mpremote`
