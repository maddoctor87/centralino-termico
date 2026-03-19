# README_INGRESSI

## Scopo

Questo documento raccoglie le scoperte già fatte durante il reverse engineering / validazione del layer I/O del PLC Industrial Shields basato su ESP32, con focus **esclusivo sugli INGRESSI**.

L’obiettivo è evitare di ripetere i test già eseguiti e consolidare una base tecnica chiara per il driver MicroPython.

---

## Contesto

* **Repo/framework analizzato:** `framework-arduino-esp32-industrialshields`
* **Variant osservata:** `hardware/esp32/2.1.2/variants/esp32plc/pins_is.h`
* **Core I/O espanso:** `hardware/esp32/2.1.2/cores/industrialshields/expanded-gpio.c`
* **Driver periferiche analizzati:**

  * `peripheral-mcp23008.c/.h`
  * `peripheral-ads1015.c/.h`
  * `peripheral-pca9685.c/.h` *(analizzato per contesto generale, ma non rilevante per gli ingressi)*
* **Ambiente test hardware:** MicroPython REPL via `mpremote` su `/dev/ttyUSB0`
* **Bus I2C usato nei test:**

```python
from machine import I2C, Pin
i2c = I2C(0, scl=Pin(22), sda=Pin(21), freq=100000)
```

---

## File già esplorati lato ingressi

### Variant / mapping pin

* `hardware/esp32/2.1.2/variants/esp32plc/pins_is.h`

### Layer GPIO espanso

* `hardware/esp32/2.1.2/cores/industrialshields/expanded-gpio.h`
* `hardware/esp32/2.1.2/cores/industrialshields/expanded-gpio.c`

### Driver ingressi

* `hardware/esp32/2.1.2/cores/industrialshields/peripheral-mcp23008.h`
* `hardware/esp32/2.1.2/cores/industrialshields/peripheral-mcp23008.c`
* `hardware/esp32/2.1.2/cores/industrialshields/peripheral-ads1015.h`
* `hardware/esp32/2.1.2/cores/industrialshields/peripheral-ads1015.c`

---

## Risultati già certi

## 1. Gli ingressi non sono tutti dello stesso tipo

Dal variant `pins_is.h` e dal comportamento di `expanded-gpio.c` emerge che il PLC usa una logica **mista** per gli ingressi:

* alcuni ingressi sono mappati su **MCP23008**
* altri ingressi sono mappati su **ADS1015**
* altri ancora, in alcune varianti/modelli, sono **GPIO ESP32 diretti**

Quindi non esiste un unico driver “digital input puro” per tutti i pin `I...`.

---

## 2. Regola di decoding dei pin encoded

Per i pin encoded `> 0xff`:

* **byte alto** = indirizzo/famiglia device
* **byte basso** = indice/canale/pin sul device

Esempi:

* `0x2106` → device `0x21`, pin `6`
* `0x4902` → device `0x49`, canale `2`
* `0x4803` → device `0x48`, canale `3`

---

## 3. Mapping device ↔ funzione

Dalle analisi del core e dagli scan I2C reali:

* `0x20`, `0x21`, `0x23` → **MCP23008**
* `0x48`, `0x49`, `0x4A`, `0x4B` → **ADS1015**
* valori piccoli tipo `27`, `26`, `35`, `25`, `34`, `5` → **GPIO ESP32 diretti**

---

## 4. Logica usata da `expanded-gpio.c` per gli ingressi

### `digitalRead()`

* se l’indirizzo è MCP23008 → legge l’ingresso tramite `mcp23008_get_input()`
* se l’indirizzo è ADS1015 → legge il canale analogico e lo converte in HIGH/LOW usando soglia:

  * `HIGH` se `raw > 1023`
  * `LOW` altrimenti

### `analogRead()`

* sui pin expanded mappati su ADS1015 usa `ads1015_get_input()`

Quindi alcuni ingressi “digitali” del framework sono in realtà **analogici sogliati**.

---

## Mapping ingressi osservato nel variant

Nel blocco variant già esaminato sono emersi questi esempi significativi:

### Ingressi via MCP23008

* `PIN_I0_0 = 0x2106`
* `PIN_I0_1 = 0x2104`
* `PIN_I0_2 = 0x2105`
* `PIN_I0_3 = 0x2103`
* `PIN_I0_4 = 0x2102`
* `PIN_I1_0 = 0x2101`
* `PIN_I1_1 = 0x2100`
* `PIN_I1_2 = 0x2007`
* `PIN_I1_3 = 0x2006`
* `PIN_I1_4 = 0x2005`
* `PIN_I2_0 = 0x2004`
* `PIN_I2_1 = 0x2003`
* `PIN_I2_2 = 0x2002`
* `PIN_I2_3 = 0x2001`
* `PIN_I2_4 = 0x2000`

### Ingressi via ADS1015

* `PIN_I0_7 = 0x4902`
* `PIN_I0_8 = 0x4903`
* `PIN_I0_9 = 0x4803`
* `PIN_I0_10 = 0x4802`
* `PIN_I0_11 = 0x4801`
* `PIN_I0_12 = 0x4800`
* `PIN_I1_7 = 0x4900`
* `PIN_I1_8 = 0x4901`
* `PIN_I1_9 = 0x4A03`
* `PIN_I1_10 = 0x4A02`
* `PIN_I1_11 = 0x4A00`
* `PIN_I1_12 = 0x4A01`
* `PIN_I2_7 = 0x4B03`
* `PIN_I2_8 = 0x4B02`
* `PIN_I2_9 = 0x4B00`
* `PIN_I2_10 = 0x4B01`

### Ingressi ESP32 diretti (dipendono dalla variante)

* `PIN_I0_5 = 27`
* `PIN_I0_6 = 26`
* `PIN_I1_5 = 35`
* `PIN_I1_6 = 25`
* `PIN_I2_5 = 34`
* `PIN_I2_6 = 5`

---

## Cosa fa il driver MCP23008

Da `peripheral-mcp23008.c`:

* init:

  * `IODIR = 0xff` → tutti input
  * `IOCON = IOCON_SEQOP | IOCON_ODR`
  * `GPPU = 0x00` → niente pull-up interni
* lettura input:

  * `GPIO` register = `0x09`
* configurazione verso pin:

  * agisce su `IODIR`

### Implicazioni pratiche

* gli ingressi MCP sono veri ingressi digitali letti dal registro `GPIO`
* non ci sono pull-up interni attivi di default
* il comportamento elettrico reale dipende dallo stadio hardware esterno del PLC

---

## Cosa fa il driver ADS1015

Da `peripheral-ads1015.c`:

* usa conversione **single-shot**
* seleziona il canale `0..3`
* attende circa `1 ms`
* legge il `CONVERSION register`
* restituisce un valore a 12 bit circa (`0..2047` nel flusso osservato)

Nel layer `expanded-gpio.c`, il risultato viene digitalizzato così:

* `HIGH` se `raw > 1023`
* `LOW` altrimenti

### Implicazioni pratiche

* gli ingressi ADS non vanno trattati come digitali puri nel driver MicroPython
* il driver corretto deve poter:

  * leggere il valore raw del canale
  * opzionalmente applicare la soglia 1023 per compatibilità col framework Arduino

---

## Test hardware già eseguiti

## 1. Scan I2C

```python
print(i2c.scan())
```

Risultato osservato:

```python
[32, 33, 35, 64, 65, 72, 73, 104, 112]
```

Interpretazione:

* `32 = 0x20`
* `33 = 0x21`
* `35 = 0x23`
* `64 = 0x40`
* `65 = 0x41`
* `72 = 0x48`
* `73 = 0x49`
* `104 = 0x68`
* `112 = 0x70`

### Conclusione

La presenza dei device I2C attesi per ingressi è confermata sul PLC reale.

---

## 2. Lettura diretta del registro GPIO dei MCP23008

Test eseguito sui device:

* `0x20`
* `0x21`
* `0x23`

Lettura del registro `0x09` (`GPIO`):

* `0x20` → `0b00000000` = `0`
* `0x21` → `0b10000011` = `131`
* `0x23` → `0b00000000` = `0`

### Conclusione

Al momento del test:

* solo `0x21` mostrava bit alti
* `0x20` e `0x23` risultavano tutti bassi

Questo conferma che la lettura diretta del `GPIO` MCP è coerente e utile per monitoraggio passivo.

---

## 3. Lettura ADS “rozze” senza configurazione

Lettura iniziale del conversion register senza corretta selezione canale/config:

* `0x48` → `0`
* `0x49` → `0`

### Conclusione

Test non significativo da solo, perché il driver ADS1015 richiede configurazione del canale prima della conversione.

---

## 4. Classe ADS1015 minimale in MicroPython

È stata costruita una classe minima per replicare la logica del framework:

```python
class ADS1015:
    REG_CONV = 0x00
    REG_CFG = 0x01

    def __init__(self, i2c, addr):
        self.i2c = i2c
        self.addr = addr

    def read_raw(self, ch):
        mux_map = {
            0: 0x40,
            1: 0x50,
            2: 0x60,
            3: 0x70,
        }
        if ch not in mux_map:
            raise ValueError("channel must be 0..3")

        cfg_hi = 0x80 | 0x02 | mux_map[ch]
        cfg_lo = 0x80 | 0x03
        self.i2c.writeto_mem(self.addr, self.REG_CFG, bytes([cfg_hi, cfg_lo]))

        import time
        time.sleep_ms(2)

        data = self.i2c.readfrom_mem(self.addr, self.REG_CONV, 2)
        val = ((data[0] << 8) | data[1]) >> 4
        if val > 0x07ff:
            val = 0
        return val
```

---

## 5. Misure ADS reali sui canali

Valori misurati:

* `0x48 ch0 = 1`
* `0x48 ch1 = 1`
* `0x48 ch2 = 1`
* `0x48 ch3 = 1`
* `0x49 ch0 = 2`
* `0x49 ch1 = 2`
* `0x49 ch2 = 2`
* `0x49 ch3 = 1`

### Conclusione

Tutti i canali letti risultavano praticamente a zero.

Con la soglia usata dal framework:

* `raw > 1023` → HIGH
* altrimenti LOW

questi ingressi risultano quindi **LOW**.

---

## Interpretazione consolidata sugli ingressi

### Conclusione principale

Gli ingressi del PLC usano **tre famiglie** diverse:

1. **MCP23008** per digitali puri via I2C
2. **ADS1015** per analogici letti e sogliati come digitali
3. **GPIO ESP32 diretti** per alcuni ingressi nativi della scheda

### Conseguenza pratica per il driver MicroPython

Il driver non deve trattare tutti i `PIN_I...` allo stesso modo.

Serve una logica di dispatch per tipo di pin:

* se address `0x20/0x21/0x23` → leggere MCP23008
* se address `0x48/0x49/0x4A/0x4B` → leggere ADS1015 raw e sogliare se richiesto
* se pin numerico diretto → usare `machine.Pin`

---

## Limiti dei test già identificati

* non sempre è possibile attivare manualmente gli ingressi dal campo, perché dipendono dalla logica reale del PLC/impianto
* i test statici danno solo una fotografia del momento
* per correlare pin logici ↔ evento reale serve monitoraggio continuo dei registri MCP/ADS durante il funzionamento dell’impianto

---

## Problemi pratici emersi nel REPL

### Formattazione binaria

In MicroPython si è evitato `format()` in stile CPython dove non disponibile, usando:

```python
"{:08b}".format(v)
```

o helper manuali.

### Prompt multilinea

C’è stato un `SyntaxError` quando una chiamata funzione è stata inserita senza uscire correttamente dal blocco `def` nel prompt REPL multilinea.

---

## Cose già provate vs ancora aperte

## Già provato

* presenza reale dei device I2C sul bus
* corrispondenza MCP23008 ↔ ingressi digitali puri
* corrispondenza ADS1015 ↔ ingressi analogici sogliati
* lettura reale dei registri MCP23008
* lettura reale dei canali ADS1015 con classe MicroPython minimale
* soglia digitale del framework Arduino = `1023`
* correlazione reale validata per gli ingressi digitali usati dal progetto:
  `I0.0 -> 0x21 bit 6`, `I0.1 -> 0x21 bit 4`, `I0.2 -> 0x21 bit 5`, `I0.3 -> 0x21 bit 3`, `I0.4 -> 0x21 bit 2`
* conferma sul campo che i relay feedback utili per il progetto stanno su `I0.0..I0.4`
* conferma che `I0.5 = GPIO27` e `I0.6 = GPIO26`
* conferma che `I0.10`, `I0.11`, `I0.12` non sono la strada corretta per contatti secchi di feedback relè

## Ancora aperto

* comportamento dinamico di eventuali altri ingressi non ancora usati nel progetto
* eventuali particolarità elettriche residue lato cablaggio/campo fuori dai segnali già validati

---

## Linea guida per il driver MicroPython lato ingressi

Il driver corretto dovrebbe essere strutturato così:

### 1. Decoder pin

Funzione che distingue:

* pin ESP32 diretto
* pin MCP23008
* pin ADS1015

### 2. Driver MCP23008

Funzioni minime:

* `read_gpio(addr)`
* `read_bit(addr, bit)`

### 3. Driver ADS1015

Funzioni minime:

* `read_raw(addr, ch)`
* `read_digital(addr, ch, threshold=1023)`

### 4. API unificata

Esempio concettuale:

```python
read_input(pin_encoded) -> 0/1
read_input_raw(pin_encoded) -> raw se ADS, bit se MCP, valore GPIO se diretto
```

---

## Stato del lavoro

La parte **ingressi** è già a un buon livello di comprensione.

La prossima fase utile è:

* consolidare questa logica in un modulo MicroPython pulito
* aggiungere monitoraggio passivo continuo per correlare eventi reali sui registri MCP/ADS
* mantenere separato il lavoro sulle **uscite**, che usa invece PCA9685 e richiede una validazione distinta

---

## Nota finale

Questo README documenta solo quanto già emerso e validato finora sugli ingressi.

Quando si passerà a implementare il driver definitivo, è importante mantenere la distinzione tra:

* **fatti già verificati**
* **deduzioni forti ma non ancora chiuse al 100%**
* **parti ancora da validare sul campo**
