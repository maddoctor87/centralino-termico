# centralino-termico

Firmware MicroPython per centrale termica su Industrial Shields ESP32 PLC 21.

- Sensori DS18B20 / DS18B22 tramite DS2482 su I2C.
- Uscite su morsetti PLC Q0.x; C1 Wilo PWM2 su Q0.5 del PLC 21 (switch B1 = ON).
- MQTT integrato con server esistente.
- Logiche modulari per pannelli, ricircolo, trasferimento calore e funzioni ausiliarie.

## Stato progetto
Il firmware attuale include la struttura base di boot, sensori, attuatori, ingressi digitali, controllo pannelli, controllo C2, ricircolo e funzioni ausiliarie.

Il Block 2 (logiche piscina / riscaldamento / GAS / valvola / comando PDC C2) è previsto a progetto ma non ancora integrato nel `main.py` della repo corrente.

Il flag MQTT `pool_just_filled` / "piscina appena riempita" puo essere ricevuto e pubblicato nello snapshot stato, ma non produce effetti finche il Block 2 non viene schedulato nel firmware attivo.

## Componenti principali
- Industrial Shields ESP32 PLC 21
- DS2482 su I2C
- DS18B20 / DS18B22
- PCA9685 su I2C
- MCP23008 su I2C
- Ethernet W5500
- Integrazione MQTT con server esistente

## Note
- Il comando C1 usa un duty Wilo PWM2 invertito su Q0.5 del PLC 21 con switch B1 = ON.
- Gli ingressi applicativi `PDC_WORK_ACS`, `PDC_WORK_ACR`, `PDC_HELP_REQUEST`, `HEAT_HELP_REQUEST` e `POOL_THERMOSTAT_CALL` sono mappati in `config.py` su contatti relè NC, quindi sono invertiti logicamente rispetto al livello fisico letto dal PLC.
- Il comando MQTT per la piscina appena riempita è `{"pool_just_filled": true|false}` su `centralina/cmd`.
