# Firmware MicroPython per Centralina Termica (ESP32 PLC 21)

Questo repository contiene il firmware MicroPython per la **centralina termica** basata su **Industrial Shields ESP32 PLC 21**.

## Stato attuale
- Logica di controllo **C1/C2/CR** (pompe) in sviluppo.
- Sensori DS18B20 su bus 1-Wire gestito via DS2482 (I2C).
- Uscite relè su PCA9685 (Q0.x) + PWM C1 su Q0.5 / A0.5 tramite PCA9685 ch13.
- MQTT: esiste un modulo base (`comms_mqtt.py`) con topic placeholder (`centralina/state`, `centralina/cmd`).

## Integrazione MQTT / Server "Itineris"
Il firmware **comunica con un server locale (Itineris) tramite MQTT**. Nel codice attuale i topic usati sono placeholder (es. `centralina/state`, `centralina/cmd`), quindi è necessario allineare i topic/payload con quanto usato dal server.

### Cosa devi fornire dal server (progetto `server-docker`)
Devi individuare e documentare **i topic MQTT già usati dal server** per:

1. **Comandi / parametri** (il PLC si sottoscrive)
   - Esempio: `centralina/cmd`, `itineris/centrale/cmd`, o simili.
   - Il payload dovrebbe essere JSON, ad esempio con chiavi come:
     - `antilegionella_request` (bool)
     - `delta_pwm_min`, `delta_pwm_max`, `pwm_min`, `pwm_max` (numerici)
     - (TODO Blocchi futuri) `pool_just_filled`, `gas_off_delay_s`, `valve_off_delay_s`, `pdc_c2_cmd_hold_s`, `pool_c2_gas_boost_after_s`

2. **Telemetria / stato** (il PLC pubblica)
   - Esempio: `centralina/state`, `itineris/centrale/state`, ecc.
   - Payload JSON con almeno:
     - temperature S1..S7 (e flag validità)
     - stato uscite: `C1`, `C2`, `CR`, `P4`, `P5`, `VALVE`
     - stato antilegionella (`antileg_ok`, `antileg_request`, ecc.)
     - eventuali allarmi sensori

### Dove modificare nel firmware
- I topic MQTT sono configurati in `config.py`:
  - `MQTT_TOPIC_STATE`
  - `MQTT_TOPIC_CMD`
- Se serve, modifica anche la logica di parsing per i comandi in `comms_mqtt.py`.

### Azioni necessarie
1. Nel progetto `server-docker`, trova i file che pubblicano/sottoscrivono i topic MQTT (es. script Node/Python):
   - cerca parole chiave come `mqtt`, `topic`, `subscribe`, `publish`, `centralina`, `itineris`, ecc.
2. Copia qui i topic effettivi e (se presenti) gli schemi JSON utilizzati.
3. A quel punto aggiorno il firmware per usare esattamente quegli topic/payload.

---

> **Nota**: finché non ho i topic/payload corretti, il firmware usa i placeholder `centralina/state` e `centralina/cmd`.

## Come cercare nel progetto server
Esempi di comandi da eseguire in `~/docker-server`:

```sh
grep -R -n "mqtt" .
grep -R -n "topic" .
```

Per ridurre il rumore, cerca parole chiave specifiche già concordate (ad esempio `centrale`, `antilegionella`, `pool_just_filled`, ecc.).

## Note per il firmware
- Il firmware ha già mappati i pin e i dispositivi I2C (DS2482, PCA9685, MCP23008).
- Le logiche di controllo C1/C2/CR sono definite e vanno implementate nelle funzioni `control_*`.
- La parte MQTT deve essere aggiornata con i topic/payload corretti non appena si hanno i dettagli server.

---

> **Obiettivo successivo**: ottenere i topic MQTT e lo schema dei messaggi dal server Itineris per completare l’integrazione.
