# README_TEST_MANUALI

Guida operativa per test manuali da PC via `mpremote`.

Uso previsto:
- da terminale, vicino al quadro
- senza creare file `.py` sul PLC
- senza caricare questo README sul PLC

Contesto consolidato:
- modello reale: Industrial Shields ESP32 PLC 21+
- C1 nel firmware = pompa pannelli Wilo PWM2 su `A0.5`
- non confondere C1 con segnali tipo `work_c1`
- bridge DS2482 non ancora arrivato
- test 1-Wire diretti su TX1/RX1 gia non riusciti

# 1. Accesso al PLC

Comandi pratici dal PC:

```bash
cd /home/mad/centralino\ centrale\ termica
PORT=$(./ops/find-plc-port.sh)
echo "$PORT"
```

```bash
./ops/plc-repl.sh
```

Alternativa diretta:

```bash
.venv-tools/bin/mpremote connect /dev/ttyUSB0 repl
```

Se vuoi partire pulito:

```bash
.venv-tools/bin/mpremote connect /dev/ttyUSB0 soft-reset repl
```

Note pratiche:
- se il firmware sta stampando log o sta eseguendo task, manda `Ctrl-C` una o due volte prima di incollare i blocchi
- per uscire dal REPL usa `Ctrl-X` oppure `Ctrl-]` secondo il terminale
- per interrompere monitor continui usa `Ctrl-C`

# 2. Blocco base da incollare nel REPL

Incolla tutto il blocco una volta sola all'inizio della sessione.

```python
from machine import I2C, Pin
import time

i2c = I2C(0, scl=Pin(22), sda=Pin(21), freq=100000)

def b8(v):
    return "{:08b}".format(v & 0xFF)

def bit(v, n):
    return (v >> n) & 1

def hx(v):
    return "0x%02X" % v

def rd1(addr, reg):
    return i2c.readfrom_mem(addr, reg, 1)[0]

def wr1(addr, reg, val):
    i2c.writeto_mem(addr, reg, bytes([val & 0xFF]))

MCP21_ADDR = 0x21
ADS48_ADDR = 0x48
ADS49_ADDR = 0x49
PCA_ADDR = 0x40
ADS_THRESHOLD = 1023

I_MCP = {
    "I0.0": 6,
    "I0.1": 4,
    "I0.2": 5,
    "I0.3": 3,
    "I0.4": 2,
}

I_GPIO = {
    "I0.5": 27,
    "I0.6": 26,
}

I_ADS = {
    "I0.7":  (ADS49_ADDR, 2),
    "I0.8":  (ADS49_ADDR, 3),
    "I0.9":  (ADS48_ADDR, 3),
    "I0.10": (ADS48_ADDR, 2),
    "I0.11": (ADS48_ADDR, 1),
    "I0.12": (ADS48_ADDR, 0),
}

Q_DIGITAL = {
    "Q0.0": 11,
    "Q0.1": 10,
    "Q0.2": 9,
    "Q0.3": 8,
    "Q0.4": 12,
}

A_PWM = {
    "A0.5": 13,
    "A0.6": 6,
    "A0.7": 7,
}

g27 = Pin(27, Pin.IN)
g26 = Pin(26, Pin.IN)

def scan_hex():
    return [hx(x) for x in i2c.scan()]

# --- MCP23008 ---
MCP_IODIR = 0x00
MCP_IOCON = 0x05
MCP_GPPU = 0x06
MCP_GPIO = 0x09
MCP_IOCON_SEQOP = 0x20
MCP_IOCON_ODR = 0x04

def mcp_init(addr=MCP21_ADDR):
    wr1(addr, MCP_IODIR, 0xFF)
    wr1(addr, MCP_IOCON, MCP_IOCON_SEQOP | MCP_IOCON_ODR)
    wr1(addr, MCP_GPPU, 0x00)

def mcp_gpio(addr=MCP21_ADDR):
    return rd1(addr, MCP_GPIO)

def mcp_bit(index, addr=MCP21_ADDR):
    return bit(mcp_gpio(addr), index)

# --- ADS1015 ---
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
            raise ValueError("ADS1015 channel must be 0..3")

        cfg_hi = 0x80 | 0x02 | mux_map[ch]
        cfg_lo = 0x80 | 0x03
        self.i2c.writeto_mem(self.addr, self.REG_CFG, bytes([cfg_hi, cfg_lo]))
        time.sleep_ms(2)

        data = self.i2c.readfrom_mem(self.addr, self.REG_CONV, 2)
        val = ((data[0] << 8) | data[1]) >> 4
        if val > 0x07FF:
            val = 0
        return val

    def read_digital(self, ch, threshold=ADS_THRESHOLD):
        return 1 if self.read_raw(ch) > threshold else 0

ads48 = ADS1015(i2c, ADS48_ADDR)
ads49 = ADS1015(i2c, ADS49_ADDR)

def ads_dev(addr):
    return ads48 if addr == ADS48_ADDR else ads49

# --- PCA9685 ---
PCA_MODE1 = 0x00
PCA_PRESCALE = 0xFE
PCA_MODE1_SLEEP = 0x10
PCA_MODE1_AI = 0x20

def pca_init(addr=PCA_ADDR):
    wr1(addr, PCA_MODE1, PCA_MODE1_SLEEP | PCA_MODE1_AI)
    wr1(addr, PCA_PRESCALE, 11)
    wr1(addr, PCA_MODE1, PCA_MODE1_AI)

def pca_led(ch, on_l, on_h, off_l, off_h, addr=PCA_ADDR):
    base = 0x06 + (int(ch) * 4)
    wr1(addr, base + 0, on_l)
    wr1(addr, base + 1, on_h)
    wr1(addr, base + 2, off_l)
    wr1(addr, base + 3, off_h)

def pca_on(ch, addr=PCA_ADDR):
    pca_led(ch, 0x00, 0x10, 0x00, 0x00, addr=addr)

def pca_off(ch, addr=PCA_ADDR):
    pca_led(ch, 0x00, 0x00, 0x00, 0x10, addr=addr)

def pca_pwm(ch, value, addr=PCA_ADDR):
    value = max(0, min(4095, int(value)))
    pca_led(ch, 0x00, 0x00, value & 0xFF, (value >> 8) & 0x0F, addr=addr)

def pca_dump(ch, addr=PCA_ADDR):
    base = 0x06 + (int(ch) * 4)
    return [rd1(addr, base + i) for i in range(4)]

def q_on(name):
    pca_on(Q_DIGITAL[name])

def q_off(name):
    pca_off(Q_DIGITAL[name])

def q_pulse(name, ms=500):
    q_on(name)
    time.sleep_ms(ms)
    q_off(name)

def pwm_raw(name, value):
    pca_pwm(A_PWM[name], value)

def pwm_pct(name, pct):
    pct = max(0, min(100, int(pct)))
    pwm_raw(name, pct * 4095 // 100)

def c1_raw(value):
    pwm_raw("A0.5", value)

def c1_pct(pct):
    pwm_pct("A0.5", pct)

mcp_init()
pca_init()

print("READY:", scan_hex())
```

Cosa aspettarti:
- nessun file viene scritto sul PLC
- il blocco deve chiudersi con una riga `READY: [...]`
- se fallisce subito su `mcp_init()` o `pca_init()`, prima verifica lo scan I2C
- il PCA viene inizializzato con la sequenza validata Industrial Shields: `MODE1 = SLEEP | AI`, `PRESCALE = 11`, `MODE1 = AI`

# 3. Test I2C scan

Blocco da incollare:

```python
print(i2c.scan())
print(scan_hex())
```

Risultato atteso indicativo:
- `0x20`, `0x21`, `0x23` per MCP
- `0x40` e spesso `0x41` lato PCA/espansioni
- `0x48`, `0x49` per ADS
- possono comparire anche `0x68`, `0x70`
- `0x18` non e atteso finche il DS2482 non e arrivato

Se `0x40`, `0x48`, `0x49` o `0x21` mancano:
- fermati
- controlla cablaggio I2C
- non ha senso proseguire con i test mirati

# 4. Test ingressi digitali diretti

Questo test legge:
- `I0.0 .. I0.4` dal MCP `0x21`
- `I0.5 = GPIO27`
- `I0.6 = GPIO26`
- e stampa anche il byte MCP in binario

```python
def read_i0_0_to_i0_6():
    raw = mcp_gpio()
    out = {
        "MCP21_GPIO_raw": raw,
        "MCP21_GPIO_bin": b8(raw),
        "I0.0": bit(raw, 6),
        "I0.1": bit(raw, 4),
        "I0.2": bit(raw, 5),
        "I0.3": bit(raw, 3),
        "I0.4": bit(raw, 2),
        "I0.5": g27.value(),
        "I0.6": g26.value(),
        "GPIO27": g27.value(),
        "GPIO26": g26.value(),
    }
    for k in (
        "MCP21_GPIO_raw", "MCP21_GPIO_bin",
        "I0.0", "I0.1", "I0.2", "I0.3", "I0.4", "I0.5", "I0.6",
        "GPIO27", "GPIO26",
    ):
        print("%-14s %s" % (k + ":", out[k]))
    return out

read_i0_0_to_i0_6()
```

Cosa aspettarti:
- il byte `MCP21_GPIO_bin` cambia quando cambiano gli ingressi MCP reali
- `I0.5` e `I0.6` leggono direttamente `GPIO27` e `GPIO26`
- se tutto resta fermo, probabilmente il segnale non sta arrivando davvero al PLC

# 5. Test ingressi ADS

Questo test legge:
- `I0.7_raw .. I0.12_raw`
- e la corrispondente digitalizzazione con soglia `1023`

```python
def ads_inputs(th=ADS_THRESHOLD):
    order = ("I0.7", "I0.8", "I0.9", "I0.10", "I0.11", "I0.12")
    out = {}
    for name in order:
        addr, ch = I_ADS[name]
        raw = ads_dev(addr).read_raw(ch)
        out[name + "_raw"] = raw
        out[name] = 1 if raw > th else 0
    for name in order:
        print("%-10s raw=%-4s dig=%s" % (name, out[name + "_raw"], out[name]))
    return out

ads_inputs()
```

Cosa aspettarti:
- `raw > 1023` = `HIGH`
- `raw <= 1023` = `LOW`
- valori `1`, `2`, `3` o comunque molto bassi significano praticamente zero utile lato ingresso

# 6. Test specifico relay feedback

I relay feedback attualmente sono cablati su:
- `I0.10 = ADS48 ch2`
- `I0.11 = ADS48 ch1`
- `I0.12 = ADS48 ch0`

Funzione dedicata:

```python
def relay_feedbacks(th=ADS_THRESHOLD, verbose=True):
    mapping = {
        "I0.10": (ADS48_ADDR, 2),
        "I0.11": (ADS48_ADDR, 1),
        "I0.12": (ADS48_ADDR, 0),
    }
    out = {}
    for name in ("I0.10", "I0.11", "I0.12"):
        addr, ch = mapping[name]
        raw = ads_dev(addr).read_raw(ch)
        out[name + "_raw"] = raw
        out[name] = 1 if raw > th else 0
        if verbose:
            print("%-10s raw=%-4s dig=%s" % (name, raw, out[name]))
    return out

relay_feedbacks()
```

Interpretazione operativa:
- se `raw` resta circa `0..5`, manca una tensione utile sul contatto
- per ottenere `HIGH` logico serve `raw > 1023`
- quei canali non vanno giudicati come digitali puri: sono ingressi ADS sogliati
- un contatto secco senza tensione reale produce tipicamente raw quasi nullo

# 7. Test uscite digitali Q0.0..Q0.4

Mapping reale:

```python
print(Q_DIGITAL)
```

Atteso:

```python
{'Q0.0': 11, 'Q0.1': 10, 'Q0.2': 9, 'Q0.3': 8, 'Q0.4': 12}
```

Esempi pratici:

```python
q_on("Q0.0")
q_off("Q0.0")
```

```python
q_on("Q0.1")
time.sleep_ms(1000)
q_off("Q0.1")
```

```python
q_pulse("Q0.0", 300)
q_pulse("Q0.1", 300)
q_pulse("Q0.2", 300)
q_pulse("Q0.3", 300)
q_pulse("Q0.4", 300)
```

Cosa aspettarti:
- il relè relativo deve commutare
- se il carico e scollegato, puoi comunque verificare il cambio con multimetro o LED di quadro
- se `Q0.x` commuta ma il feedback non cambia, guarda prima il cablaggio feedback e la presenza di tensione reale

# 8. Test PWM su A0.5

`A0.5` = PCA `0x40` ch `13`.

Nel firmware:
- `A0.5` pilota C1
- C1 = pompa pannelli Wilo PWM2

Comandi base:

```python
c1_raw(0)
print(pca_dump(A_PWM["A0.5"]))
```

```python
c1_raw(2048)
print(pca_dump(A_PWM["A0.5"]))
```

```python
c1_raw(4095)
print(pca_dump(A_PWM["A0.5"]))
```

```python
c1_pct(0)
c1_pct(50)
c1_pct(100)
```

Dump registri del canale:

```python
print(pca_dump(13))
```

Cosa aspettarti:
- `c1_raw(0)` -> duty nullo
- `c1_raw(2048)` -> duty circa 50%
- `c1_raw(4095)` -> massimo valore scrivibile
- il dump canale deve cambiare su `OFF_L/OFF_H`

Nota pratica:
- qui stai testando il path fisico `A0.5`
- non stai testando direttamente la logica del firmware C1
- il nome C1 nel firmware indica la pompa pannelli Wilo PWM2

# 9. Monitor continui

Monitor input completo, stampa solo se cambia qualcosa:

```python
def snapshot_inputs(th=ADS_THRESHOLD):
    raw = mcp_gpio()
    out = {
        "MCP21_GPIO_bin": b8(raw),
        "I0.0": bit(raw, 6),
        "I0.1": bit(raw, 4),
        "I0.2": bit(raw, 5),
        "I0.3": bit(raw, 3),
        "I0.4": bit(raw, 2),
        "I0.5": g27.value(),
        "I0.6": g26.value(),
    }
    for name in ("I0.7", "I0.8", "I0.9", "I0.10", "I0.11", "I0.12"):
        addr, ch = I_ADS[name]
        raw_ads = ads_dev(addr).read_raw(ch)
        out[name + "_raw"] = raw_ads
        out[name] = 1 if raw_ads > th else 0
    return out

def monitor_inputs(period_ms=250):
    prev = None
    while True:
        cur = snapshot_inputs()
        if cur != prev:
            print("----")
            for k in sorted(cur):
                print("%-14s %s" % (k + ":", cur[k]))
            prev = cur
        time.sleep_ms(period_ms)
```

```python
monitor_inputs()
```

Monitor dedicato ai relay feedback:

```python
def monitor_relay_feedbacks(period_ms=250):
    prev = None
    while True:
        cur = relay_feedbacks(verbose=False)
        if cur != prev:
            print("----")
            for k in ("I0.10_raw", "I0.10", "I0.11_raw", "I0.11", "I0.12_raw", "I0.12"):
                print("%-14s %s" % (k + ":", cur[k]))
            prev = cur
        time.sleep_ms(period_ms)
```

```python
monitor_relay_feedbacks()
```

Interruzione:
- usa `Ctrl-C`
- se il REPL resta incastrato, premi ancora `Ctrl-C`

# 10. Interpretazione risultati

Se un relay NC da `HIGH` a riposo:
- e coerente solo se il contatto NC porta davvero una tensione valida verso l'ingresso
- in quel caso il relay a riposo puo risultare chiuso e quindi "alto"

Se un ADS resta a `1`, `2`, `3`:
- per il debug operativo equivale a zero
- il PLC non sta vedendo una tensione significativa
- quel canale verra digitalizzato `LOW`

Se `Q0.x` cambia ma il feedback non cambia:
- l'uscita puo essere corretta ma il feedback puo essere cablato altrove
- oppure il feedback e su ADS ma manca una tensione di riferimento reale
- oppure il relè commuta ma il contatto osservato non e quello giusto

Se `A0.5` scrive `4095` ma il livello fisico resta basso:
- controlla che stai misurando il nodo giusto
- controlla il path reale `A0.5` / selezione hardware della scheda
- controlla massa e riferimento dello strumento
- controlla che il carico Wilo PWM2 non richieda una condizione elettrica diversa dal solo write PCA

# 11. Note sul cablaggio relay feedback

Punto chiave:
- sui relay feedback cablati su `I0.10`, `I0.11`, `I0.12` non basta un contatto secco

Serve:
- una tensione di riferimento reale
- coerente con il tipo di ingresso e col riferimento elettrico del PLC

Conseguenze pratiche:
- senza tensione reale gli ADS leggono quasi zero
- un contatto secco aperto/chiuso ma flottante non produce un `HIGH` affidabile
- per vedere `HIGH` logico serve un raw ben sopra soglia, non basta una commutazione meccanica

# 12. Note sulle sonde

Stato pratico attuale:
- i test 1-Wire diretti su GPIO/TX1/RX1 non hanno restituito ROM utili
- il bridge DS2482 non e ancora arrivato
- per ora conviene sospendere i test sonde

Indicazione operativa:
- non perdere tempo con procedure lunghe su 1-Wire diretto in questo stato
- riprendere i test quando il DS2482 sara fisicamente disponibile sul bus I2C
- se nello scan manca `0x18`, al momento e coerente con lo stato hardware noto
