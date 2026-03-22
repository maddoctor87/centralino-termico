# README Logiche - Firmware MicroPython Centrale Termica ESP32 PLC 21

Questo documento descrive tutte le logiche implementate nel firmware MicroPython per il controllo della centrale termica solare, dalla A alla Z.

## 1. Panoramica generale
- **Scopo**: Controllare pompe, valvole e riscaldamenti in un impianto solare termico, con logiche automatiche per pannelli (Block 1), piscina/riscaldamento (Block 2), e sicurezza. Integrazione MQTT per monitoraggio e comandi remoti.
- **Hardware**: ESP32 con I2C per sensori (DS2482 + DS18B20), attuatori (PCA9685), ingressi (MCP23008 + GPIO diretti), Ethernet W5500, MQTT.
- **Architettura**: Task asincroni in `main.py` (sensori, controlli, MQTT). Stato globale in `state.py`. Config in `config.py`.

## 2. Logiche di controllo (Blocks)
### Block 1: Controllo pannelli solari (C1)
- **Modulo**: `control_panels.py`
- **Scopo**: Regolare la pompa C1 con duty Wilo PWM2 diretto per ottimizzare raccolta calore dai pannelli, bilanciando delta temperature.
- **Logica** (`control_panels_task`):
  - Calcola `delta_solare = S1 - S2` (pannelli vs boiler solare).
  - Se delta > soglia minima, attiva C1 con wilo duty basato su friction factor e hysteresis.
  - Il setpoint portale `solar_target_c` limita il caricamento normale del boiler solare: a target raggiunto C1 si ferma e riparte solo sotto isteresi.
  - Il setpoint `solar_target_c` viene ignorato solo in emergenza termica sul boiler solare (`S3`/`S2` alti a 85 C), dove restano attive solo le protezioni di sicurezza.
  - Override manuale, hard stop su allarmi sensori.
  - Antilegionella: Ciclo periodico per prevenire batteri (configurabile).
- **Uscita**: C1 Wilo PWM2 su Q0.5 del PLC 21 (switch B1 = ON).
- **Ingressi**: Temperature S1-S3.

### Block 2: Logiche piscina e riscaldamento
- **Modulo**: `control_block2_pool_heat_pdc.py`
- **Scopo**: Gestire richieste calore piscina/riscaldamento, coordinando GAS, valvola, PDC e pompe ausiliarie.
- **Stato attuale**: attivo e schedulato nel `main.py` della repo corrente.
- **Logica target** (`Block2Controller.run_once`):
- **GAS_ENABLE**: ON se PDC_HELP_REQUEST ma solo quando il boiler PDC ne ha davvero bisogno (sensore alto `S4` molto sotto target oppure stratificazione `S4-S5` eccessiva), o PDC lavora su C1 + richiesta piscina/riscaldamento, o boost dopo lavoro continuo C2 su piscina, o piscina appena riempita (placeholder).
  - **Priorita aiuto PDC da solare**: se `PDC_HELP_REQUEST` e il lato solare e' a temperatura critica, il firmware preferisce scaricare il boiler solare su `C2` invece di accendere il gas; fuori da quel caso il gas resta l'unico aiuto automatico.
  - **Filtro aiuto PDC**: se `PDC_HELP_REQUEST` ma `S5` ha gia' raggiunto il target del boiler PDC, la richiesta viene ignorata: `GAS_ENABLE=OFF` e `C2=OFF`.
  - **VALVE**: ON su richiesta piscina o riscaldamento (valvola EVIE, devia flusso).
  - **PDC_CMD_START_ACR**: ON se PDC libero da C1 + richiesta piscina/riscaldamento (comanda il lavoro ACR).
  - **HEAT_PUMP**: ON su richiesta aiuto riscaldamento.
  - **PISCINA_PUMP**: ON su richiesta calore piscina.
  - **Delay/Hold**: Ritardi spegnimento per stabilità (GAS_OFF_DELAY_S, VALVE_OFF_DELAY_S, PDC_C2_CMD_HOLD_S).
  - **Sicurezza**: Spegnimento su ingressi invalidi.
- **Uscite**: C2 (Q0.0), PISCINA_PUMP (Q0.1), HEAT_PUMP (Q0.2), CR (Q0.3), VALVE (Q0.4), GAS_ENABLE (Q0.6), PDC_CMD_START_ACR (Q0.7).
- **Ingressi target**: PDC_WORK_ACS, PDC_HELP_REQUEST, PDC_WORK_ACR, HEAT_HELP_REQUEST, POOL_THERMOSTAT_CALL.
- **Nota**: nella repo corrente gli ingressi `PDC_WORK_ACS`, `PDC_HELP_REQUEST`, `PDC_WORK_ACR`, `HEAT_HELP_REQUEST` e `POOL_THERMOSTAT_CALL` sono mappati in `config.py` rispettivamente su `I0.0`, `I0.1`, `I0.2`, `I0.3`, `I0.4`; essendo contatti relè NC, a riposo il PLC legge `HIGH` e in richiesta/lavoro legge `LOW`, poi `inputs.py` inverte il livello fisico nel segnale applicativo.
- **Flag MQTT attivo**: `pool_just_filled` puo essere comandato via MQTT/API, viene pubblicato nello snapshot stato e partecipa alla logica Block 2 runtime.

### Altri controlli
- **C2 (trasferimento solare → PDC)**: `control_c2.py`
  - ON se la media del boiler solare (`S2/S3`) supera la media del boiler PDC (`S4/S5`) oltre hysteresis.
  - Override aiuto PDC: se `PDC_HELP_REQUEST` ma `S5` e' gia' al target boiler PDC, la richiesta viene ignorata.
  - In `PDC_HELP_REQUEST`, `C2` non viene piu usata come aiuto normale: parte solo come scarico del boiler solare se la temperatura lato solare e' critica.
  - Override antilegionella: se `antileg_request` e la media boiler solare (`S2/S3`) ha gia' raggiunto il target antilegionella, `C2` viene forzata ON; se il solare non e' pronto, `C2` resta OFF e la priorita passa a gas + PDC ACR.
  - Stop aggiuntivo se ACS e' attiva e `S1` scende sotto la media del boiler solare.
  - Hard stop invariato se `S4` supera la soglia di sicurezza.
  - Uscita: C2 (Q0.0).
- **CR (ricircolo collettore)**: `control_recirc.py`
  - In normale parte solo se il boiler PDC (media `S4/S5`) e' almeno a 40 C.
  - L'abilitazione da boiler PDC ha isteresi dedicata: se CR e' gia' attivo resta abilitato fino a 38 C.
  - Poi mantiene il collettore al setpoint portale `recirc_target_c` con isteresi: si accende quando `min(S6,S7)` scende sotto il target meno isteresi e si spegne al target.
  - In antilegionella `S6/S7` non partecipano al criterio di completamento: il ciclo porta prima il boiler PDC alla soglia di avvio (`S5 >= target`, `S4 >= target + 5 C`), poi attiva `CR`.
  - Durante la fase con `CR` attivo, il boiler PDC deve restare almeno a target (`S4` e `S5 >= target`) per 1800 secondi totali; se la temperatura scende, il timer viene messo in pausa e non azzerato.
  - Il setpoint normale `recirc_target_c` viene ignorato solo in emergenza o antilegionella; l'emergenza CR scatta solo se il boiler solare alto (`max(S2,S3)`) arriva a 85 C.
  - Uscita: CR (Q0.1).
- **Antilegionella**: richiesta manuale o schedulata dal portale; esecuzione nel firmware tramite `control_recirc.py`, con supporto di `control_c2.py` e `control_block2_pool_heat_pdc.py` per scegliere tra solare e gas + PDC ACR.

## 3. Gestione I/O
- **Sensori temperatura** (`sensors.py`): DS18B20 via DS2482, lettura asincrona ogni 1s, validazione, allarmi su sensori invalidi.
- **Attuatori** (`actuators.py`): PCA9685 per relè e duty Wilo PWM2, safe state OFF.
- **Ingressi digitali** (`inputs.py`): MCP23008 + GPIO diretti, debounce configurabile (`INPUT_DEBOUNCE_MS`, attualmente 50ms).

## 4. Comunicazioni
- **MQTT** (`comms_mqtt.py`): Connessione a broker, publish snapshot ogni 10s su `centralina/state`, subscribe comandi su `centralina/cmd` (manual override, antileg, setpoints, `pool_just_filled`).
- **Scheduler antilegionella** (`portal_sync/backend/acs.py` + `portal_sync/backend/main.py`): il backend mantiene una programmazione settimanale configurabile dal portale ACS e pubblica `antileg_request=true` all'orario stabilito usando la timezone applicativa del server.
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
  - Controlli attuali: panels, C2, CR, Block2.
  - `mqtt_task`: Comunicazioni.
- **Frequenza**: Tutto ogni 1s (CONTROL_INTERVAL_MS).
- **Snapshot MQTT**: Include temps, relays, setpoints, allarmi, block2.

## 7. Configurazione
- **config.py**: Pin mapping, setpoints (es. DELTA_MIN_SOLARE=5°C), MQTT, I2C, delays.
- **README_pin_map.md**: Mappatura fisica pin/morsetti.

Il sistema è modulare, sicuro e scalabile. Tutte le logiche sono implementate con hysteresis, delay e validazione per robustezza.
