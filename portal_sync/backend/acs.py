# acs.py – ACS (Centrale Termica) state store + MQTT bridge
# Modello dati: replica del state.py del firmware MicroPython.
import json
import logging
import os
import time
from threading import Lock
from typing import Any, Dict, Optional

from paho.mqtt import client as mqtt

logger = logging.getLogger(__name__)

ACS_ROLES = ["superad", "admin", "maintenance"]

# ── Configurazione ────────────────────────────────────────────────────────────
ACS_TOPIC = os.environ.get("ACS_MQTT_TOPIC", "centralina/state").strip()
ACS_CMD_TOPIC = os.environ.get("ACS_MQTT_CMD_TOPIC", "centralina/cmd").strip()
ACS_OFFLINE_AFTER_S = max(30, int(os.environ.get("ACS_OFFLINE_AFTER_S", "60")))


# ── Store thread-safe ─────────────────────────────────────────────────────────
class ACSStore:
    """In-memory snapshot dello stato ESP32 ACS."""

    _EMPTY: Dict[str, Any] = {
        "ts": None,
        "temps": {k: None for k in ("S1", "S2", "S3", "S4", "S5", "S6", "S7")},
        "c1_wilo_duty_pct": 0,
        "c2_on": False,
        "cr_on": False,
        "p4_on": False,
        "p5_on": False,
        "valve_on": False,
        "relays": {k: False for k in ("C2", "CR", "P4", "P5", "VALVE")},
        "relay_available": {k: False for k in ("C2", "CR", "P4", "P5", "VALVE")},
        "manual_mode": False,
        "manual_relays": {k: False for k in ("C2", "CR", "P4", "P5", "VALVE")},
        "manual_c1_wilo_duty_pct": 0,
        "setpoints": {
            "solar_target_c": 55.0,
            "pdc_target_c": 50.0,
            "recirc_target_c": 45.0,
            "antileg_target_c": 70.0,
        },
        "setpoint_meta": {
            "solar_target_c": {"label": "Target solare", "min": 20.0, "max": 95.0, "step": 0.5},
            "pdc_target_c": {"label": "Target boiler PDC", "min": 20.0, "max": 95.0, "step": 0.5},
            "recirc_target_c": {"label": "Target ricircolo", "min": 20.0, "max": 80.0, "step": 0.5},
            "antileg_target_c": {"label": "Target antilegionella", "min": 55.0, "max": 80.0, "step": 0.5},
        },
        "alarms": {
            "ALARM_SENSORS_PANELS": False,
            "ALARM_SENSORS_C2": False,
            "ALARM_SENSORS_CR": False,
            "ALARM_S4_INVALID": False,
        },
        "c1_latch": False,
        "cr_emerg": False,
        "antileg_ok": False,
        "antileg_ok_ts": None,
        "antileg_request": False,
    }

    def __init__(self) -> None:
        self._lock = Lock()
        self._data: Dict[str, Any] = json.loads(json.dumps(self._EMPTY))
        self._received_at: Optional[float] = None

    def _normalize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = json.loads(json.dumps(self._EMPTY))
        for key, value in payload.items():
            if key in {"temps", "alarms", "relays", "relay_available", "manual_relays", "setpoints", "setpoint_meta"}:
                if isinstance(value, dict):
                    data[key].update(value)
                continue
            data[key] = value
        return data

    def update(self, payload: str) -> None:
        try:
            d = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("ACSStore: payload JSON non valido: %.80s", payload)
            return
        if not isinstance(d, dict):
            return
        now = time.time()
        with self._lock:
            self._data = self._normalize(d)
            self._received_at = now

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            data = dict(self._data)
            received_at = self._received_at
        online = received_at is not None and (now - received_at) <= ACS_OFFLINE_AFTER_S
        data["online"] = online
        data["received_at"] = received_at
        return data


# ── MQTT bridge ───────────────────────────────────────────────────────────────
class ACSMQTTBridge:
    """Subscriber MQTT dedicato al topic ACS dell'ESP32."""

    def __init__(self, store: ACSStore, mqtt_settings: Dict[str, Any]) -> None:
        self._store = store
        self._settings = mqtt_settings
        self._client: Optional[mqtt.Client] = None
        self._lock = Lock()

    def start(self) -> None:
        if not ACS_TOPIC:
            logger.info("ACSMQTTBridge disabilitato: ACS_MQTT_TOPIC non configurato")
            return
        host = self._settings.get("host") or "mosquitto"
        port = int(self._settings.get("port", 1883))
        client_id = f"piscina-acs-{os.getpid()}"
        logger.info("ACSMQTTBridge → MQTT %s:%s topic=%s", host, port, ACS_TOPIC)

        kw: Dict[str, Any] = {"client_id": client_id}
        cbv = getattr(mqtt, "CallbackAPIVersion", None)
        if cbv is not None:
            kw["callback_api_version"] = cbv.VERSION2
        client = mqtt.Client(**kw)

        username = self._settings.get("username")
        password = self._settings.get("password")
        if username:
            client.username_pw_set(username, password or "")

        client.on_connect = self._on_connect
        client.on_message = self._on_message
        client.on_disconnect = self._on_disconnect
        client.connect_async(host, port, keepalive=60)
        client.loop_start()
        with self._lock:
            self._client = client

    def stop(self) -> None:
        with self._lock:
            client = self._client
            if not client:
                return
            self._client = None
        try:
            client.loop_stop()
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
        logger.info("ACSMQTTBridge fermato")

    def publish_cmd(self, payload: Dict[str, Any]) -> bool:
        """Pubblica un comando verso l'ESP32 (future use)."""
        with self._lock:
            client = self._client
        if not client or not ACS_CMD_TOPIC:
            return False
        try:
            client.publish(ACS_CMD_TOPIC, json.dumps(payload), qos=1)
            return True
        except Exception as e:
            logger.warning("ACSMQTTBridge publish_cmd error: %s", e)
            return False

    def _on_connect(self, client, _ud, _flags, rc, _props=None) -> None:
        if rc == 0:
            logger.info("ACSMQTTBridge connesso, subscribe a '%s'", ACS_TOPIC)
            client.subscribe(ACS_TOPIC, qos=1)
        else:
            logger.warning("ACSMQTTBridge connessione fallita rc=%s", rc)

    def _on_disconnect(self, _client, _ud, *args) -> None:
        rc = args[-1] if args else 0
        if hasattr(rc, "value"):
            rc = rc.value
        if rc != 0:
            logger.warning("ACSMQTTBridge disconnesso inaspettatamente rc=%s", rc)

    def _on_message(self, _client, _ud, message: mqtt.MQTTMessage) -> None:
        try:
            raw = message.payload.decode("utf-8", errors="replace").strip()
        except Exception:
            raw = ""
        self._store.update(raw)


__all__ = ["ACSStore", "ACSMQTTBridge", "ACS_ROLES"]
