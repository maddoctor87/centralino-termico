# README Logiche - Firmware MicroPython Centrale Termica ESP32 PLC 21

Questo documento descrive tutte le logiche implementate nel firmware MicroPython per il controllo della centrale termica solare, dalla A alla Z.

## 1. Panoramica generale
- **Scopo**: Controllare pompe, valvole e riscaldamenti in un impianto solare termico, con logiche automatiche per pannelli (Block 1), piscina/riscaldamento (Block 2), e sicurezza. Integrazione MQTT per monitoraggio e comandi remoti.
- **Hardware**: ESP32 con I2C per sensori (DS2482 + DS18B20), attuatori (PCA9685), ingressi (MCP23008 + GPIO diretti), Ethernet W5500, MQTT.
- **Architettura**: Task asincroni in `main.py` (sensori, controlli, MQTT). Stato globale in `state.py`. Config in `config.py`.

## 2. Logiche di controllo (Blocks)
### Block 1: Controllo pannelli solari (C1)
- **Modulo**: `control_panels.py`
- **Scopo**: Regolare la pompa C1 (PWM 0-10V) per ottimizzare raccolta calore dai pannelli, bilanciando delta temperature.
- **Logica** (`control_panels_task`):
  - Calcola `delta_solare = S1 - S2` (pannelli vs boiler solare).
  - Se delta > soglia minima, attiva C1 con duty cycle basato su friction factor e hysteresis.
  - Override manuale, hard stop su allarmi sensori.
  - Antilegionella: Ciclo periodico per prevenire batteri (configurabile).
- **Uscita**: C1 PWM su Q0.5 del PLC 21 (switch B1 = ON).
- **Ingressi**: Temperature S1-S3.

### Block 2: Logiche piscina e riscaldamento
- **Modulo**: `control_block2_pool_heat_pdc.py`
- **Scopo**: Gestire richieste calore piscina/riscaldamento, coordinando GAS, valvola, PDC e pompe ausiliarie.
- **Stato attuale**: previsto a progetto, ma non ancora integrato nel `main.py` della repo corrente.
- **Logica target** (`Block2Controller.run_once`):
  - **GAS_ENABLE**: ON se PDC_HELP_REQUEST, o PDC lavora su C1 + richiesta piscina/riscaldamento, o boost dopo lavoro continuo C2 su piscina, o piscina appena riempita (placeholder).
  - **VALVE_RELAY**: ON su richiesta piscina o riscaldamento (devia flusso).
  - **PDC_CMD_START_C2**: ON se PDC libero da C1 + richiesta piscina/riscaldamento (comanda PDC a lavorare su C2).
  - **HEAT_PUMP**: ON su richiesta aiuto riscaldamento.
  - **PISCINA_PUMP**: ON su richiesta calore piscina.
  - **Delay/Hold**: Ritardi spegnimento per stabilità (GAS_OFF_DELAY_S, VALVE_OFF_DELAY_S, PDC_C2_CMD_HOLD_S).
  - **Sicurezza**: Spegnimento su ingressi invalidi.
- **Uscite**: GAS_ENABLE (Q0.6), VALVE_RELAY (Q0.4), PDC_CMD_START_C2 (Q0.7), HEAT_PUMP (ch0), PISCINA_PUMP (Q0.2).
- **Ingressi target**: PDC_WORK_ACS/C2, PDC_HELP_REQUEST, POOL_THERMOSTAT_CALL, HEAT_HELP_REQUEST.
- **Nota**: nella repo corrente sono già allineati `POOL_THERMOSTAT_CALL` e `HEAT_HELP_REQUEST`; i segnali PDC restano da mappare esplicitamente in `config.py` / `inputs.py`.

### Altri controlli
- **C2 (trasferimento solare → PDC)**: `control_c2.py`
  - ON se delta_solare > delta_PDC + hysteresis, con hard stop.
  - Uscita: C2 (Q0.0).
- **CR (ricircolo collettore)**: `control_recirc.py`
  - Hysteresis normale/emergenza, antilegionella timer.
  - Uscita: CR (Q0.1).
- **Antilegionella**: In `control_panels.py`, ciclo C1 per pulizia.

## 3. Gestione I/O
- **Sensori temperatura** (`sensors.py`): DS18B20 via DS2482, lettura asincrona ogni 1s, validazione, allarmi su sensori invalidi.
- **Attuatori** (`actuators.py`): PCA9685 per relè/PWM, safe state OFF.
- **Ingressi digitali** (`inputs.py`): MCP23008 + GPIO diretti, debounce configurabile (`INPUT_DEBOUNCE_MS`, attualmente 50ms).

## 4. Comunicazioni
- **MQTT** (`comms_mqtt.py`): Connessione a broker, publish snapshot ogni 10s su `centralina/state`, subscribe comandi su `centralina/cmd` (manual override, antileg, setpoints).
- **Ethernet**: W5500 per rete, DHCP/static IP.

## 5. Stato e sicurezza
- **Stato globale** (`state.py`): Tracking temps, relays, setpoints, allarmi (sensori invalidi), block2 outputs.
- **Allarmi**: Su sensori mancanti (pannelli, C2, CR, S4).
- **Fallback**: Spegnimento sicuro su errori, manual mode per override.
- **Setpoints**: Configurabili via MQTT (delta min, hysteresis, etc.).

## 6. Integrazione e task
- **main.py**: Boot sequenziale (Ethernet, I2C, init managers), task asincroni:
  - `sensor_task`: Lettura sensori.
  - `input_task`: Lettura ingressi.
  - Controlli attuali: panels, C2, CR, aux.
  - Controlli previsti: Block2 dopo implementazione e integrazione nel scheduler.
  - `mqtt_task`: Comunicazioni.
- **Frequenza**: Tutto ogni 1s (CONTROL_INTERVAL_MS).
- **Snapshot MQTT**: Include temps, relays, setpoints, allarmi, block2.

## 7. Configurazione
- **config.py**: Pin mapping, setpoints (es. DELTA_MIN_SOLARE=5°C), MQTT, I2C, delays.
- **README_pin_map.md**: Mappatura fisica pin/morsetti.

Il sistema è modulare, sicuro e scalabile. Tutte le logiche sono implementate con hysteresis, delay e validazione per robustezza.