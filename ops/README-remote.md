# Remote Ops

- `ops/find-plc-port.sh`: trova automaticamente la seriale del PLC
- `ops/plc-console.sh`: apre la console seriale a `115200`
- `ops/plc-repl.sh`: apre il REPL MicroPython via `mpremote`
- `ops/plc-sync.sh`: copia i file `.py` sul PLC e fa reset
- `ops/plc-reset.sh`: reset software del PLC

Nota OTA:
- l'OTA firmware nativo usa Ethernet + backend ACS, ma la prima installazione del supporto OTA sul PLC va fatta comunque con `ops/plc-sync.sh`
- dopo il primo deploy, il portale ACS puo pubblicare il comando OTA e il PLC scarica il firmware `.app-bin` via HTTP nella partizione OTA successiva

Prerequisiti sul portatile remoto:
- `python3`
- `tio` o `picocom`
- virtualenv `.venv-tools` con `mpremote`
