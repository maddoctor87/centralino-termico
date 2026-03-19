from machine import Pin, reset
import network
import time
import ubinascii
import ujson
import onewire
import ds18x20
from umqtt.simple import MQTTClient

import config


def _mqtt_password():
    return getattr(config, "MQTT_PASS", getattr(config, "MQTT_PASSWORD", "")) or None


def _status_payload(status, ip=None, error=None):
    payload = {
        "device_id": config.DEVICE_ID,
        "status": status,
        "ts": time.time(),
    }
    if ip:
        payload["ip"] = ip
    if error:
        payload["error"] = str(error)
    return ujson.dumps(payload)


def _rom_labels():
    labels = {}
    for rom, label in getattr(config, "ROM_LABELS", {}).items():
        if rom:
            labels[str(rom).lower()] = label
    return labels


def wifi_connect():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if not wlan.isconnected():
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        timeout = getattr(config, "WIFI_CONNECT_TIMEOUT_S", 20)
        start = time.time()
        while not wlan.isconnected():
            if time.time() - start > timeout:
                print("[wifi] timeout, continuo offline")
                return wlan
            time.sleep(1)
        print("[wifi] connesso:", wlan.ifconfig()[0])
    return wlan


def mqtt_connect(ip=None):
    client = MQTTClient(
        client_id=getattr(config, "MQTT_CLIENT_ID", config.DEVICE_ID),
        server=config.MQTT_BROKER,
        port=config.MQTT_PORT,
        user=getattr(config, "MQTT_USER", None) or None,
        password=_mqtt_password(),
        keepalive=getattr(config, "MQTT_KEEPALIVE", 60),
    )
    client.set_last_will(
        config.MQTT_TOPIC_STATUS,
        _status_payload("offline", ip=ip),
        retain=True,
    )
    client.connect()
    client.publish(
        config.MQTT_TOPIC_STATUS,
        _status_payload("online", ip=ip),
        retain=True,
    )
    return client


def ds_init():
    pin = Pin(config.ONEWIRE_GPIO, Pin.OPEN_DRAIN, Pin.PULL_UP)
    ow = onewire.OneWire(pin)
    return ds18x20.DS18X20(ow)


def rom_hex(rom):
    return ubinascii.hexlify(rom).decode().lower()


def read_temperatures(ds, roms):
    logical_temps = {label: None for label in getattr(config, "SENSOR_LABELS", ())}
    sensors = {}
    labels = _rom_labels()
    descriptions = getattr(config, "SENSOR_DESCRIPTIONS", {})

    ds.convert_temp()
    time.sleep_ms(getattr(config, "CONVERSION_WAIT_MS", 750))

    for rom in roms:
        rom_key = rom_hex(rom)
        label = labels.get(rom_key)

        try:
            temp_c = ds.read_temp(rom)
        except Exception:
            temp_c = None

        row = {"temp_c": temp_c}
        if label:
            logical_temps[label] = temp_c
            row["label"] = label
            if label in descriptions:
                row["description"] = descriptions[label]
        sensors[rom_key] = row

    return logical_temps, sensors


def build_state_payload(wlan, roms, logical_temps, sensors):
    ip = None
    if wlan is not None and wlan.isconnected():
        try:
            ip = wlan.ifconfig()[0]
        except Exception:
            ip = None

    return {
        "device_id": config.DEVICE_ID,
        "project": "centralina",
        "source": "esp32_sonde",
        "ip": ip,
        "onewire_gpio": config.ONEWIRE_GPIO,
        "sensor_count": len(roms),
        "temps": logical_temps,
        "sensors": sensors,
        "ts": time.time(),
    }


def _print_local_status(payload):
    print("[sonde] ts={} count={} temps={}".format(
        payload.get("ts"),
        payload.get("sensor_count"),
        payload.get("temps"),
    ))


def main():
    wlan = wifi_connect()
    client = None
    ds = ds_init()
    last_wifi_retry = 0

    while True:
        try:
            roms = ds.scan()
            logical_temps, sensors = read_temperatures(ds, roms)
            payload = build_state_payload(wlan, roms, logical_temps, sensors)
            _print_local_status(payload)

            wifi_online = wlan is not None and wlan.isconnected()
            now = time.time()

            if not wifi_online and (now - last_wifi_retry) >= getattr(config, "WIFI_RETRY_INTERVAL_S", 30):
                last_wifi_retry = now
                wlan = wifi_connect()
                wifi_online = wlan is not None and wlan.isconnected()
                if not wifi_online:
                    client = None

            if wifi_online:
                ip = wlan.ifconfig()[0]
                try:
                    client.ping()
                except Exception:
                    client = mqtt_connect(ip=ip)

                client.publish(
                    config.MQTT_TOPIC_STATE,
                    ujson.dumps(payload),
                    retain=False,
                )
                client.publish(
                    config.MQTT_TOPIC_STATUS,
                    _status_payload("online", ip=ip),
                    retain=True,
                )

        except Exception as e:
            print("[sonde] loop error:", e)
            try:
                if client is not None:
                    client.publish(
                        config.MQTT_TOPIC_STATUS,
                        _status_payload("error", error=e),
                        retain=True,
                    )
            except Exception:
                pass
            time.sleep(5)

        time.sleep(config.READ_INTERVAL_SEC)


try:
    main()
except Exception:
    time.sleep(3)
    reset()
