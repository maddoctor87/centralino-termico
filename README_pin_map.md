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
| Q0.1 | ch 10 | - | PISCINA_PUMP (pompa piscina) | Relè Finder 24Vdc | ON/OFF |
| Q0.2 | ch 9 | - | HEAT_PUMP (pompa aiuto riscaldamento) | Relè Finder 24Vdc | ON/OFF |
| Q0.3 | ch 8 | - | CR (pompa ricircolo collettore) | Relè Finder 24Vdc | ON/OFF |
| Q0.4 | ch 12 | - | VALVE / valvola EVIE | Relè Finder 24Vdc | ON/OFF |
| Q0.6 | ch 6 | - | GAS_ENABLE (abilitazione gas) | Relè Finder 24Vdc | ON/OFF |
| Q0.7 | ch 7 | - | PDC_CMD_START_ACR (comando avvio lavoro ACR verso PDC) | Relè Finder 24Vdc | ON/OFF |

---
## 3) Ingressi digitali (I0.x / MCP23008 @ 0x21)

| Morsetto | Pin MCP23008 | GPIO ESP32 | Segnale firmware | Funzione |
|----------|-------------|------------|------------------|----------|
| I0.0 | pin 6 | - | PDC_WORK_ACS | Feedback relè NC |
| I0.1 | pin 4 | - | PDC_HELP_REQUEST | Feedback relè NC |
| I0.2 | pin 5 | - | HEAT_HELP_REQUEST | Feedback relè NC |
| I0.3 | pin 3 | - | POOL_THERMOSTAT_CALL | Feedback relè NC |
| I0.4 | pin 2 | - | PDC_WORK_ACR | Feedback relè NC di Q0.7 |
| I0.5 | - | GPIO 27 | Spare / prototipazione | Non usato negli alias applicativi correnti |
| I0.6 | - | GPIO 26 | Spare / prototipazione | Non usato negli alias applicativi correnti |

> Nota: tutti i segnali applicativi sopra sono cablati con contatti relè NC. Nel firmware vengono quindi invertiti logicamente: contatto chiuso = ingresso fisico attivo = segnale applicativo `False`; contatto aperto = ingresso fisico disattivo = segnale applicativo `True`.

---
## 4) Pompa PWM Wilo (C1)

- **Uscita usata dal firmware**: **A0.5**
- **Configurazione hardware**: **switch zona B, switch 1 = OFF**
- **Uso nel progetto**: comando duty Wilo PWM2 della pompa C1

> Il firmware usa A0.5 come uscita PWM per la pompa C1 Wilo PWM2.
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
