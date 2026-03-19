# centralino-termico

Firmware MicroPython per centrale termica su Industrial Shields ESP32 PLC 21.

- Sensori DS18B20 / DS18B22 tramite DS2482 su I2C.
- Uscite su morsetti PLC Q0.x; C1 Wilo PWM2 su Q0.5 del PLC 21 (switch B1 = ON).
- MQTT integrato con server esistente.
- Logiche modulari per pannelli, ricircolo, trasferimento calore e funzioni ausiliarie.

## Stato progetto
Il firmware attuale include boot, sensori, attuatori, ingressi digitali, controllo pannelli, controllo C2, ricircolo e Block 2 piscina / riscaldamento.

Il Block 2 (logiche piscina / riscaldamento / GAS / valvola / comando PDC ACR) è ora integrato nel `main.py` della repo corrente.

Il flag MQTT `pool_just_filled` / "piscina appena riempita" viene ricevuto, pubblicato nello snapshot stato e usato dalla logica Block 2 attiva.

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
- Gli ingressi applicativi `PDC_WORK_ACS`, `PDC_HELP_REQUEST`, `PDC_WORK_ACR`, `HEAT_HELP_REQUEST` e `POOL_THERMOSTAT_CALL` sono mappati in `config.py` sui morsetti reali `I0.0..I0.4` con contatti relè NC; quindi a riposo il PLC legge `HIGH`, mentre in richiesta/lavoro legge `LOW`, e il firmware inverte logicamente il valore fisico.
- Il comando MQTT per la piscina appena riempita è `{"pool_just_filled": true|false}` su `centralina/cmd`.
