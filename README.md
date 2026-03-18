# centralino-termico

Firmware MicroPython per centrale termica su Industrial Shields ESP32 PLC 21.

- Sensori DS18B20 / DS18B22 tramite DS2482 su I2C.
- Uscite su morsetti PLC Q0.x; C1 PWM su Q0.5 del PLC 21 (switch B1 = ON).
- MQTT integrato con server esistente.
- Logiche modulari per pannelli, ricircolo, trasferimento calore e funzioni ausiliarie.

## Stato progetto
Il firmware attuale include la struttura base di boot, sensori, attuatori, ingressi digitali, controllo pannelli, controllo C2, ricircolo e funzioni ausiliarie.

Il Block 2 (logiche piscina / riscaldamento / GAS / valvola / comando PDC C2) è previsto a progetto ma non ancora integrato nel `main.py` della repo corrente.

## Componenti principali
- Industrial Shields ESP32 PLC 21
- DS2482 su I2C
- DS18B20 / DS18B22
- PCA9685 su I2C
- MCP23008 su I2C
- Ethernet W5500
- Integrazione MQTT con server esistente

## Note
- Il comando PWM della pompa C1 usa Q0.5 del PLC 21 con switch B1 = ON.
- I segnali `POOL_THERMOSTAT_CALL` e `HEAT_HELP_REQUEST` sono già allineati nella repo corrente.
- I segnali `PDC_WORK_ACS`, `PDC_WORK_ACR` e `PDC_HELP_REQUEST` sono target del futuro Block 2 e vanno mappati esplicitamente in `config.py` / `inputs.py` quando sarà implementato.