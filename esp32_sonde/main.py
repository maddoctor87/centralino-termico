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


def _expected_sensor_count():
    labels = _rom_labels()
    if labels:
        return len(labels)
    return len(getattr(config, "SENSOR_LABELS", ()))


def _configure_static_ip(wlan):
    ip = getattr(config, "WIFI_STATIC_IP", None)
    if not ip:
        return

    netmask = getattr(config, "WIFI_NETMASK", "255.255.255.0")
    gateway = getattr(config, "WIFI_GATEWAY", None)
    dns = getattr(config, "WIFI_DNS", gateway)
    if not gateway:
        return

    desired = (ip, netmask, gateway, dns)
    try:
        current = wlan.ifconfig()
    except Exception:
        current = None

    if current != desired:
        wlan.ifconfig(desired)


def wifi_connect(force_reset=False):
    wlan = network.WLAN(network.STA_IF)
    if force_reset and getattr(config, "WIFI_RESET_ON_RECONNECT", True):
        try:
            wlan.disconnect()
        except Exception:
            pass
        try:
            wlan.active(False)
            time.sleep_ms(200)
        except Exception:
            pass

    wlan.active(True)
    _configure_static_ip(wlan)

    if not wlan.isconnected():
        try:
            wlan.disconnect()
        except Exception:
            pass
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        timeout = getattr(config, "WIFI_CONNECT_TIMEOUT_S", 20)
        start = time.time()
        while not wlan.isconnected():
            if time.time() - start > timeout:
                try:
                    status = wlan.status()
                except Exception:
                    status = "unknown"
                print("[wifi] timeout, continuo offline status={}".format(status))
                return wlan
            time.sleep(1)
        print("[wifi] connesso:", wlan.ifconfig()[0])
    return wlan


def mqtt_disconnect(client):
    if client is None:
        return None
    try:
        client.disconnect()
    except Exception:
        pass
    return None


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


def scan_stable_roms(ds):
    retries = max(1, int(getattr(config, "SCAN_RETRIES", 3)))
    delay_ms = max(0, int(getattr(config, "SCAN_RETRY_DELAY_MS", 200)))
    union = {}

    for attempt in range(retries):
        try:
            current_roms = ds.scan()
        except Exception as exc:
            print("[sonde] scan error attempt={} err={}".format(attempt + 1, exc))
            current_roms = []

        for rom in current_roms:
            union[rom_hex(rom)] = rom

        if attempt + 1 < retries and delay_ms:
            time.sleep_ms(delay_ms)

    rom_keys = sorted(union.keys())
    roms = [union[key] for key in rom_keys]
    return roms, rom_keys


def read_temperatures(ds, roms, known_sensors, miss_counts):
    logical_temps = {label: None for label in getattr(config, "SENSOR_LABELS", ())}
    sensors = {}
    labels = _rom_labels()
    descriptions = getattr(config, "SENSOR_DESCRIPTIONS", {})
    tolerance = max(1, int(getattr(config, "SENSOR_MISS_TOLERANCE", 3)))
    seen = set()

    ds.convert_temp()
    time.sleep_ms(getattr(config, "CONVERSION_WAIT_MS", 750))

    for rom in roms:
        rom_key = rom_hex(rom)
        seen.add(rom_key)
        label = labels.get(rom_key)

        try:
            temp_c = ds.read_temp(rom)
        except Exception as exc:
            print("[sonde] read error rom={} err={}".format(rom_key, exc))
            temp_c = None

        row = known_sensors.get(rom_key, {})
        row["temp_c"] = temp_c
        row["present"] = True
        if label:
            row["label"] = label
            logical_temps[label] = temp_c
            if label in descriptions:
                row["description"] = descriptions[label]
        sensors[rom_key] = row
        miss_counts[rom_key] = 0

    for rom_key, row in known_sensors.items():
        if rom_key in seen:
            continue

        misses = miss_counts.get(rom_key, 0) + 1
        miss_counts[rom_key] = misses
        if misses < tolerance:
            cached = dict(row)
            cached["present"] = False
            sensors[rom_key] = cached
            label = cached.get("label")
            if label in logical_temps:
                logical_temps[label] = cached.get("temp_c")

    return logical_temps, sensors


def build_state_payload(wlan, rom_keys, logical_temps, sensors):
    ip = None
    if wlan is not None and wlan.isconnected():
        try:
            ip = wlan.ifconfig()[0]
        except Exception:
            ip = None

    expected_count = _expected_sensor_count()
    present_count = 0
    missing_labels = []
    for label in getattr(config, "SENSOR_LABELS", ()): 
        if logical_temps.get(label) is None:
            missing_labels.append(label)
        else:
            present_count += 1

    return {
        "device_id": config.DEVICE_ID,
        "project": "centralina",
        "source": "esp32_sonde",
        "ip": ip,
        "onewire_gpio": config.ONEWIRE_GPIO,
        "sensor_count": len(rom_keys),
        "expected_sensor_count": expected_count,
        "present_sensor_count": present_count,
        "missing_labels": missing_labels,
        "temps": logical_temps,
        "sensors": sensors,
        "ts": time.time(),
    }


def _print_local_status(payload, rom_keys):
    print("[sonde] ts={} count={}/{} missing={} roms={} temps={}".format(
        payload.get("ts"),
        payload.get("present_sensor_count"),
        payload.get("expected_sensor_count"),
        payload.get("missing_labels"),
        rom_keys,
        payload.get("temps"),
    ))


def main():
    wlan = wifi_connect(force_reset=True)
    client = None
    ds = ds_init()
    last_wifi_retry = 0
    known_sensors = {}
    miss_counts = {}

    while True:
        try:
            roms, rom_keys = scan_stable_roms(ds)
            logical_temps, sensors = read_temperatures(ds, roms, known_sensors, miss_counts)
            known_sensors.update(sensors)
            payload = build_state_payload(wlan, rom_keys, logical_temps, sensors)
            _print_local_status(payload, rom_keys)

            wifi_online = wlan is not None and wlan.isconnected()
            now = time.time()

            if not wifi_online and (now - last_wifi_retry) >= getattr(config, "WIFI_RETRY_INTERVAL_S", 30):
                last_wifi_retry = now
                client = mqtt_disconnect(client)
                wlan = wifi_connect(force_reset=True)
                wifi_online = wlan is not None and wlan.isconnected()

            if wifi_online:
                ip = wlan.ifconfig()[0]
                try:
                    if client is None:
                        raise OSError("mqtt client assente")
                    client.ping()
                except Exception:
                    client = mqtt_disconnect(client)
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
                client = mqtt_disconnect(client)
            time.sleep(5)

        time.sleep(config.READ_INTERVAL_SEC)


try:
    main()
except Exception:
    time.sleep(3)
    reset()
