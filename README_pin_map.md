# Pin Map - ESP32 PLC 21 (Centralina Termica)

Questo documento mostra la mappatura dei pin / morsetti usati dal firmware per la centrale termica, con l'indicazione di dove collegare i componenti.

---
## 1) Bus I2C (comune a PCA9685, MCP23008, DS2482)
- **SDA** = GPIO 21
- **SCL** = GPIO 22

> Nota: tutte le periferiche I2C condividono lo stesso bus.

---
## 2) Uscite (Q0.x / PCA9685 @ 0x40)

| Morsetto | Canale PCA9685 | GPIO ESP32 | Funzione firmware | Carico | Note |
|---------|----------------|------------|-------------------|--------|------|
| Q0.0 | ch 11 | - | C2 (pompa trasferimento solare -> PDC) | Relè Finder 24Vdc | ON/OFF |
| Q0.1 | ch 10 | - | CR (pompa ricircolo collettore) | Relè Finder 24Vdc | ON/OFF |
| Q0.2 | ch 9 | - | PISCINA_PUMP (pompa piscina, attivata da POOL_THERMOSTAT_CALL) | Relè Finder 24Vdc | ON/OFF |
| Q0.4 | ch 12 | - | VALVE_RELAY (valvola motorizzata fail-safe) | Relè Finder 24Vdc | ON/OFF |
| Q0.6 | ch 6 | - | GAS_ENABLE (abilitazione gas) | Relè Finder 24Vdc | ON/OFF |
| Q0.7 | ch 7 | - | PDC_CMD_START_C2 (comando avvio lavoro C2 verso PDC) | Relè Finder 24Vdc | ON/OFF |
| - | ch 0 | - | HEAT_PUMP (pompa per aiuto riscaldamento) | Relè Finder 24Vdc | ON/OFF (richiede espansione I2C esterna) |

---
## 3) Ingressi digitali (I0.x / MCP23008 @ 0x21)

| Morsetto | Pin MCP23008 | GPIO ESP32 | Segnale firmware | Funzione |
|----------|-------------|------------|------------------|----------|
| I0.5 | - | GPIO 27 | POOL_THERMOSTAT_CALL | Piscina thermostat call |
| I0.6 | - | GPIO 26 | HEAT_HELP_REQUEST | Riscaldamento help request |
| I0.0 | pin 0 | - | PDC_WORK_C1 | PDC working on C1 (placeholder) |
| I0.1 | pin 1 | - | PDC_WORK_C2 | PDC working on C2 (placeholder) |
| I0.7 | pin 7 | - | PDC_HELP_REQUEST | PDC help request (placeholder) |

---
## 4) Pompa PWM Wilo (C1)

- **Segnale 0-10V**: A0.5 = ch 13 PCA9685 (C1_PWM_CH)
- **Driver optoisolatore**: PC817 (4 canali) con pull-up +5V verso ingresso PWM+ della pompa.
- **Schema tipico**: +5V → R 1k–2.2k → PWM+ (pompa). PWM- a massa.

> In `config.py` è configurato per usare PCA9685 (0-10V su A0.5).

---
## 5) Sensori temperatura (1-Wire)

- **Bridge I2C→1-Wire**: DS2482S-800 @ 0x18 (collegato al bus I2C SDA/SCL)
- **Sonde**: 7× DS18B20/DS18B22 in parallelo sul bus 1-Wire.
- **Pull-up**: una resistenza 4.7kΩ tra VCC 5V e data 1-Wire.

Mappatura logica nel firmware (`config.py`):
- S1: pannelli solari
- S2: centro boiler solare
- S3: alto boiler solare
- S4: alto boiler PDC
- S5: basso boiler PDC
- S6: collettore ricircolo ingresso
- S7: collettore ricircolo fine

---
## 6) Ethernet W5500

- **SPI ID**: 2
- **SCK**: GPIO 18
- **MOSI**: GPIO 23
- **MISO**: GPIO 19
- **CS**: GPIO 15
- **INT**: GPIO 4

> Configurazione IP statica: 192.168.10.210 (modificabile in config.py)

---
## 7) Note generali cablaggio
- Tutte le uscite relè sono pilotate a 24Vdc (Q0.x) e comandano i relè Finder 24Vdc.
- Gli ingressi sono contatti puliti (24Vdc) dai relè Finder 230Vac coil, passati attraverso un circuito di alimentazione 24Vdc del PLC.
- Lo snubber RC / MOV deve essere applicato a ogni carico 230Vac (uscita relè).

---
## 8) Passi successivi (check finale)
1. Verifica che i relè abbiano la giusta alimentazione 24Vdc.
2. Assicurati che il bus I2C sia libero e connesso a DS2482, PCA9685 e MCP23008.
3. Carica il firmware e testa la ricezione MQTT sul topic configurato (`centralina/cmd`) e l’invio su (`centralina/state`).

---
## 9) Idee future / espansioni
- **Sistema feedback relè con resistenze e I2C**: Implementare un ADC I2C (es. ADS1115 @0x48) collegato a una rete di resistenze sui contatti NC dei relè. Ogni relè ha un resistore diverso (1k, 2k, 4k, etc.) in serie. L'ADC misura la tensione per identificare quale relè è attivo (o combinazioni). Vantaggi: monitora tutti i 7 relè con 1 ingresso analogico, espandibile, robusto contro rumore. Schema: +5V → R1 → NC_relè1 → R2 → NC_relè2 → ... → GND, con ADC sul punto comune.

---

Se vuoi, posso aggiungere un disegno semplificato del cablaggio (text-based) o un elenco pin-to-pin ancora più dettagliato (es. segnali verso il relè con n° morsetto).