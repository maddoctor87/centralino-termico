# esp32_sonde

Firmware temporaneo per ESP32 che legge sonde DS18B20 dirette e pubblica su MQTT, allineato al progetto centrale termica.

## MQTT

- Broker: `192.168.10.20:1883`
- Credenziali: `relay_sensor / RelaySensor!2024`
- Topic stato: `centralina/sonde/<DEVICE_ID>/state`
- Topic status: `centralina/sonde/<DEVICE_ID>/status`

## Payload pubblicato

Il topic `state` pubblica un JSON con:

- `temps`: mappa logica `S1..S7` compatibile con il progetto
- `sensors`: dettaglio raw per ROM 1-Wire
- `device_id`, `ip`, `sensor_count`, `ts`

## Mappatura ROM -> sonda progetto

1. Avvia il firmware con `ROM_LABELS = {}`.
2. Leggi il payload MQTT e annota le ROM in `sensors`.
3. Compila `ROM_LABELS` in [`config.py`](/home/mad/centralino centrale termica/esp32_sonde/config.py) associando ogni ROM a `S1..S7`.

Esempio:

```python
ROM_LABELS = {
    "28ff641d7216035c": "S1",
    "28ff4c1d7216031a": "S2",
}
```

## Comandi REPL

Per ricavare le ROM delle sonde dal REPL MicroPython:

```python
import onewire, ds18x20, ubinascii
from machine import Pin
ds = ds18x20.DS18X20(onewire.OneWire(Pin(4, Pin.OPEN_DRAIN, Pin.PULL_UP)))
roms = ds.scan()
[ubinascii.hexlify(r).decode() for r in roms]
```

Per ricavare ROM e temperatura letta:

```python
import onewire, ds18x20, ubinascii, time
from machine import Pin
ds = ds18x20.DS18X20(onewire.OneWire(Pin(4, Pin.OPEN_DRAIN, Pin.PULL_UP)))
roms = ds.scan()
ds.convert_temp()
time.sleep_ms(750)
[(ubinascii.hexlify(r).decode(), ds.read_temp(r)) for r in roms]
```

Se il pin 1-Wire cambia, sostituire `Pin(4, ...)` con il valore reale di `ONEWIRE_GPIO` in [`config.py`](/home/mad/centralino centrale termica/esp32_sonde/config.py).
