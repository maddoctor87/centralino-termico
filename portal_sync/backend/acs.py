# acs.py – ACS (Centrale Termica) state store + MQTT bridge
# Modello dati: replica del state.py del firmware MicroPython.
import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from threading import Lock
from typing import Any, Dict, Optional

from paho.mqtt import client as mqtt

logger = logging.getLogger(__name__)

ACS_ROLES = ["superad", "admin", "maintenance"]

# ── Configurazione ────────────────────────────────────────────────────────────
ACS_TOPIC = os.environ.get("ACS_MQTT_TOPIC", "centralina/state").strip()
ACS_CMD_TOPIC = os.environ.get("ACS_MQTT_CMD_TOPIC", "centralina/cmd").strip()
ACS_OFFLINE_AFTER_S = max(30, int(os.environ.get("ACS_OFFLINE_AFTER_S", "60")))
ACS_ANTILEG_SCHEDULE_FILE = Path(
    os.environ.get("ACS_ANTILEG_SCHEDULE_FILE", "data/acs_antileg_schedule.json")
).resolve()
ACS_ANTILEG_SCHEDULE_POLL_S = max(
    10, int(os.environ.get("ACS_ANTILEG_SCHEDULE_POLL_S", "15"))
)
ACS_NIGHT_ECO_FILE = Path(
    os.environ.get("ACS_NIGHT_ECO_FILE", "data/acs_night_eco.json")
).resolve()
ACS_NIGHT_ECO_POLL_S = max(
    10, int(os.environ.get("ACS_NIGHT_ECO_POLL_S", "30"))
)


# ── Store thread-safe ─────────────────────────────────────────────────────────
class ACSStore:
    """In-memory snapshot dello stato ESP32 ACS."""

    _EMPTY: Dict[str, Any] = {
        "ts": None,
        "temps": {k: None for k in ("S1", "S2", "S3", "S4", "S5", "S6", "S7")},
        "inputs": {},
        "c1_wilo_duty_pct": 20,
        "c1_active": False,
        "c2_on": False,
        "cr_on": False,
        "piscina_pump_on": False,
        "heat_pump_on": False,
        "gas_enable_on": False,
        "pdc_cmd_start_acr_on": False,
        "p4_on": False,
        "p5_on": False,
        "valve_on": False,
        "relays": {k: False for k in ("C2", "PISCINA_PUMP", "HEAT_PUMP", "CR", "VALVE", "GAS_ENABLE", "PDC_CMD_START_ACR")},
        "relay_available": {k: False for k in ("C2", "PISCINA_PUMP", "HEAT_PUMP", "CR", "VALVE", "GAS_ENABLE", "PDC_CMD_START_ACR")},
        "manual_mode": False,
        "manual_relays": {k: False for k in ("C2", "PISCINA_PUMP", "HEAT_PUMP", "CR", "VALVE", "GAS_ENABLE", "PDC_CMD_START_ACR")},
        "manual_c1_wilo_duty_pct": 20,
        "pool_just_filled": False,
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
            "ALARM_C2_FB_MISMATCH": False,
        },
        "c1_latch": False,
        "c2_fb_expected": None,
        "c2_fb_last_change_ts": 0,
        "c2_fb_alarm": False,
        "cr_emerg": False,
        "block2_outputs": {
            "gas_enable": False,
            "valve": False,
            "pdc_cmd_start_acr": False,
            "heat_pump": False,
            "piscina_pump": False,
        },
        "firmware_version": "unknown",
        "ota": {
            "enabled": False,
            "current_version": "unknown",
            "current_build": None,
            "current_partition": None,
            "state": "idle",
            "message": None,
            "target_version": None,
            "manifest_url": None,
            "firmware_url": None,
            "target_partition": None,
            "bytes_written": 0,
            "total_bytes": 0,
            "started_at": None,
            "finished_at": None,
            "last_error": None,
            "last_result": None,
            "last_success_version": None,
            "last_success_partition": None,
        },
        "antileg_ok": False,
        "antileg_ok_ts": None,
        "antileg_request": False,
        "antileg_hold_elapsed_s": 0,
        "antileg_phase": "idle",
    }

    def __init__(self) -> None:
        self._lock = Lock()
        self._data: Dict[str, Any] = json.loads(json.dumps(self._EMPTY))
        self._received_at: Optional[float] = None

    def _normalize(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(payload)
        legacy_map = {
            "c1_duty": "c1_wilo_duty_pct",
            "manual_pwm_duty": "manual_c1_wilo_duty_pct",
        }
        for legacy_key, current_key in legacy_map.items():
            if current_key not in payload and legacy_key in payload:
                payload[current_key] = payload[legacy_key]

        data = json.loads(json.dumps(self._EMPTY))
        for key, value in payload.items():
            if key in {"temps", "alarms", "relays", "relay_available", "manual_relays", "setpoints", "setpoint_meta", "block2_outputs", "ota"}:
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

class ACSAntilegScheduler:
    _DEFAULT: Dict[str, Any] = {
        "enabled": False,
        "weekday": 6,
        "time_hhmm": "03:00",
        "last_trigger_at": None,
        "last_trigger_key": None,
        "last_result": None,
    }
    _WEEKDAY_LABELS = (
        "Lunedi",
        "Martedi",
        "Mercoledi",
        "Giovedi",
        "Venerdi",
        "Sabato",
        "Domenica",
    )

    def __init__(self, publish_cmd, snapshot_fn, tzinfo) -> None:
        self._publish_cmd = publish_cmd
        self._snapshot_fn = snapshot_fn
        self._tzinfo = tzinfo
        self._lock = Lock()
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self._data: Dict[str, Any] = dict(self._DEFAULT)
        self._load()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(
            target=self._loop,
            name="acs-antileg-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("ACSAntilegScheduler avviato")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def update_config(self, *, enabled: bool, weekday: int, time_hhmm: str) -> Dict[str, Any]:
        normalized = {
            "enabled": bool(enabled),
            "weekday": self._normalize_weekday(weekday),
            "time_hhmm": self._normalize_time_hhmm(time_hhmm),
        }
        with self._lock:
            self._data.update(normalized)
            self._save_unlocked()
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._data)
        next_run = self._next_run_dt(data, now_dt=datetime.now(self._tzinfo))
        weekday = int(data["weekday"])
        return {
            "enabled": bool(data["enabled"]),
            "weekday": weekday,
            "weekday_label": self._WEEKDAY_LABELS[weekday],
            "time_hhmm": data["time_hhmm"],
            "next_run_at": int(next_run.timestamp()) if next_run else None,
            "last_trigger_at": data.get("last_trigger_at"),
            "last_result": data.get("last_result"),
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("ACSAntilegScheduler tick error")
            if self._stop.wait(ACS_ANTILEG_SCHEDULE_POLL_S):
                break

    def _tick(self) -> None:
        with self._lock:
            data = dict(self._data)

        now_dt = datetime.now(self._tzinfo)
        trigger_key = self._current_trigger_key(data, now_dt)
        if trigger_key is None:
            return
        if data.get("last_trigger_key") == trigger_key:
            return

        snapshot = self._snapshot_fn() if callable(self._snapshot_fn) else {}
        if isinstance(snapshot, dict) and snapshot.get("antileg_request"):
            self._mark_trigger(trigger_key, int(time.time()), "already_active")
            return

        ok = bool(self._publish_cmd({"antileg_request": True}))
        self._mark_trigger(
            trigger_key,
            int(time.time()),
            "published" if ok else "publish_failed",
        )
        if not ok:
            logger.warning("ACSAntilegScheduler publish failed for key=%s", trigger_key)

    def _mark_trigger(self, trigger_key: str, ts: int, result: str) -> None:
        with self._lock:
            self._data["last_trigger_key"] = trigger_key
            self._data["last_trigger_at"] = int(ts)
            self._data["last_result"] = str(result)
            self._save_unlocked()

    def _load(self) -> None:
        try:
            payload = json.loads(ACS_ANTILEG_SCHEDULE_FILE.read_text())
        except Exception:
            return

        if not isinstance(payload, dict):
            return

        try:
            normalized = {
                "enabled": bool(payload.get("enabled", self._DEFAULT["enabled"])),
                "weekday": self._normalize_weekday(payload.get("weekday", self._DEFAULT["weekday"])),
                "time_hhmm": self._normalize_time_hhmm(payload.get("time_hhmm", self._DEFAULT["time_hhmm"])),
                "last_trigger_at": self._normalize_optional_int(payload.get("last_trigger_at")),
                "last_trigger_key": payload.get("last_trigger_key"),
                "last_result": payload.get("last_result"),
            }
        except Exception as e:
            logger.warning("ACSAntilegScheduler load error: %s", e)
            return

        with self._lock:
            self._data.update(normalized)

    def _save_unlocked(self) -> None:
        ACS_ANTILEG_SCHEDULE_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACS_ANTILEG_SCHEDULE_FILE.write_text(json.dumps(self._data))

    def _current_trigger_key(self, data: Dict[str, Any], now_dt: datetime) -> Optional[str]:
        if not data.get("enabled"):
            return None
        hour, minute = self._split_hhmm(data["time_hhmm"])
        if now_dt.weekday() != int(data["weekday"]):
            return None
        if now_dt.hour != hour or now_dt.minute != minute:
            return None
        return now_dt.strftime("%Y%m%d%H%M")

    def _next_run_dt(self, data: Dict[str, Any], now_dt: datetime) -> Optional[datetime]:
        if not data.get("enabled"):
            return None
        hour, minute = self._split_hhmm(data["time_hhmm"])
        weekday = int(data["weekday"])
        day_delta = (weekday - now_dt.weekday()) % 7
        candidate = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
        candidate += timedelta(days=day_delta)
        if candidate <= now_dt:
            candidate += timedelta(days=7)
        return candidate

    def _normalize_weekday(self, value: Any) -> int:
        weekday = int(value)
        if weekday < 0 or weekday > 6:
            raise ValueError("weekday fuori range 0..6")
        return weekday

    def _normalize_time_hhmm(self, value: Any) -> str:
        hour, minute = self._split_hhmm(value)
        return "{:02d}:{:02d}".format(hour, minute)

    def _split_hhmm(self, value: Any) -> tuple[int, int]:
        parts = str(value or "").strip().split(":")
        if len(parts) != 2:
            raise ValueError("time_hhmm deve essere HH:MM")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("time_hhmm non valido")
        return hour, minute

    def _normalize_optional_int(self, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        return int(value)


class ACSNightEcoScheduler:
    _DEFAULT: Dict[str, Any] = {
        "enabled": False,
        "start_hhmm": "23:00",
        "end_hhmm": "06:00",
        "day_pdc_target_c": 50.0,
        "night_pdc_target_c": 45.0,
        "day_recirc_target_c": 45.0,
        "night_recirc_target_c": 40.0,
        "last_applied_mode": None,
        "last_applied_key": None,
        "last_applied_at": None,
        "last_result": None,
    }

    def __init__(self, publish_cmd, snapshot_fn, tzinfo) -> None:
        self._publish_cmd = publish_cmd
        self._snapshot_fn = snapshot_fn
        self._tzinfo = tzinfo
        self._lock = Lock()
        self._stop = Event()
        self._thread: Optional[Thread] = None
        self._data: Dict[str, Any] = dict(self._DEFAULT)
        self._load()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(
            target=self._loop,
            name="acs-night-eco-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("ACSNightEcoScheduler avviato")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None

    def update_config(
        self,
        *,
        enabled: bool,
        start_hhmm: str,
        end_hhmm: str,
        day_pdc_target_c: float,
        night_pdc_target_c: float,
        day_recirc_target_c: float,
        night_recirc_target_c: float,
    ) -> Dict[str, Any]:
        normalized = {
            "enabled": bool(enabled),
            "start_hhmm": self._normalize_time_hhmm(start_hhmm),
            "end_hhmm": self._normalize_time_hhmm(end_hhmm),
            "day_pdc_target_c": self._normalize_target(day_pdc_target_c, low=20.0, high=95.0),
            "night_pdc_target_c": self._normalize_target(night_pdc_target_c, low=20.0, high=95.0),
            "day_recirc_target_c": self._normalize_target(day_recirc_target_c, low=20.0, high=80.0),
            "night_recirc_target_c": self._normalize_target(night_recirc_target_c, low=20.0, high=80.0),
        }
        self._validate_targets(normalized)
        with self._lock:
            self._data.update(normalized)
            self._data["last_applied_key"] = None
            self._save_unlocked()

        now_dt = datetime.now(self._tzinfo)
        if normalized["enabled"]:
            mode, trigger_key = self._current_mode_and_key(normalized, now_dt)
        else:
            mode, trigger_key = "day", None
        self._apply_mode(mode, trigger_key=trigger_key, reason="config_update")
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            data = dict(self._data)
        now_dt = datetime.now(self._tzinfo)
        active_mode, _ = self._current_mode_and_key(data, now_dt)
        return {
            "enabled": bool(data["enabled"]),
            "start_hhmm": data["start_hhmm"],
            "end_hhmm": data["end_hhmm"],
            "day_pdc_target_c": float(data["day_pdc_target_c"]),
            "night_pdc_target_c": float(data["night_pdc_target_c"]),
            "day_recirc_target_c": float(data["day_recirc_target_c"]),
            "night_recirc_target_c": float(data["night_recirc_target_c"]),
            "active_mode": active_mode if data["enabled"] else "day",
            "night_active": bool(data["enabled"] and active_mode == "night"),
            "last_applied_mode": data.get("last_applied_mode"),
            "last_applied_at": data.get("last_applied_at"),
            "last_result": data.get("last_result"),
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                logger.exception("ACSNightEcoScheduler tick error")
            if self._stop.wait(ACS_NIGHT_ECO_POLL_S):
                break

    def _tick(self) -> None:
        with self._lock:
            data = dict(self._data)

        if not data.get("enabled"):
            return

        mode, trigger_key = self._current_mode_and_key(
            data,
            datetime.now(self._tzinfo),
        )
        if trigger_key == data.get("last_applied_key"):
            return
        self._apply_mode(mode, trigger_key=trigger_key, reason="schedule")

    def _apply_mode(self, mode: str, *, trigger_key: Optional[str], reason: str) -> None:
        snapshot = self._snapshot_fn() if callable(self._snapshot_fn) else {}
        current_setpoints = {}
        if isinstance(snapshot, dict):
            current_setpoints = snapshot.get("setpoints") or {}

        with self._lock:
            data = dict(self._data)

        target_setpoints = self._target_setpoints(data, mode)
        changes = []
        for key, target in target_setpoints.items():
            current_value = self._try_float(current_setpoints.get(key))
            if current_value is not None and abs(current_value - target) < 0.05:
                continue
            ok = bool(self._publish_cmd({"setpoint": {"key": key, "value": target}}))
            changes.append("{}={}({})".format(key, target, "ok" if ok else "fail"))
            if not ok:
                self._mark_applied(
                    mode,
                    trigger_key,
                    "publish_failed:{}:{}".format(reason, key),
                )
                logger.warning(
                    "ACSNightEcoScheduler publish failed mode=%s key=%s", mode, key
                )
                return

        result = "no_change:{}".format(reason) if not changes else "applied:{}:{}".format(
            reason,
            ",".join(changes),
        )
        self._mark_applied(mode, trigger_key, result)

    def _mark_applied(
        self,
        mode: str,
        trigger_key: Optional[str],
        result: str,
    ) -> None:
        with self._lock:
            self._data["last_applied_mode"] = str(mode)
            self._data["last_applied_key"] = trigger_key
            self._data["last_applied_at"] = int(time.time())
            self._data["last_result"] = str(result)
            self._save_unlocked()

    def _target_setpoints(self, data: Dict[str, Any], mode: str) -> Dict[str, float]:
        if mode == "night":
            return {
                "pdc_target_c": float(data["night_pdc_target_c"]),
                "recirc_target_c": float(data["night_recirc_target_c"]),
            }
        return {
            "pdc_target_c": float(data["day_pdc_target_c"]),
            "recirc_target_c": float(data["day_recirc_target_c"]),
        }

    def _current_mode_and_key(
        self, data: Dict[str, Any], now_dt: datetime
    ) -> tuple[str, Optional[str]]:
        if not data.get("enabled"):
            return "day", None

        night_active = self._is_night_active(
            data["start_hhmm"],
            data["end_hhmm"],
            now_dt,
        )
        if night_active:
            start_hour, start_minute = self._split_hhmm(data["start_hhmm"])
            start_minutes = start_hour * 60 + start_minute
            now_minutes = now_dt.hour * 60 + now_dt.minute
            anchor_dt = now_dt
            if self._window_crosses_midnight(data["start_hhmm"], data["end_hhmm"]) and now_minutes < start_minutes:
                anchor_dt = now_dt - timedelta(days=1)
            return "night", "night:{}".format(anchor_dt.strftime("%Y%m%d"))
        return "day", "day:{}".format(now_dt.strftime("%Y%m%d"))

    def _is_night_active(self, start_hhmm: str, end_hhmm: str, now_dt: datetime) -> bool:
        start_hour, start_minute = self._split_hhmm(start_hhmm)
        end_hour, end_minute = self._split_hhmm(end_hhmm)
        start_total = start_hour * 60 + start_minute
        end_total = end_hour * 60 + end_minute
        now_total = now_dt.hour * 60 + now_dt.minute

        if start_total < end_total:
            return start_total <= now_total < end_total
        return now_total >= start_total or now_total < end_total

    def _window_crosses_midnight(self, start_hhmm: str, end_hhmm: str) -> bool:
        start_hour, start_minute = self._split_hhmm(start_hhmm)
        end_hour, end_minute = self._split_hhmm(end_hhmm)
        return (start_hour * 60 + start_minute) > (end_hour * 60 + end_minute)

    def _load(self) -> None:
        try:
            payload = json.loads(ACS_NIGHT_ECO_FILE.read_text())
        except Exception:
            return

        if not isinstance(payload, dict):
            return

        try:
            normalized = {
                "enabled": bool(payload.get("enabled", self._DEFAULT["enabled"])),
                "start_hhmm": self._normalize_time_hhmm(payload.get("start_hhmm", self._DEFAULT["start_hhmm"])),
                "end_hhmm": self._normalize_time_hhmm(payload.get("end_hhmm", self._DEFAULT["end_hhmm"])),
                "day_pdc_target_c": self._normalize_target(payload.get("day_pdc_target_c", self._DEFAULT["day_pdc_target_c"]), low=20.0, high=95.0),
                "night_pdc_target_c": self._normalize_target(payload.get("night_pdc_target_c", self._DEFAULT["night_pdc_target_c"]), low=20.0, high=95.0),
                "day_recirc_target_c": self._normalize_target(payload.get("day_recirc_target_c", self._DEFAULT["day_recirc_target_c"]), low=20.0, high=80.0),
                "night_recirc_target_c": self._normalize_target(payload.get("night_recirc_target_c", self._DEFAULT["night_recirc_target_c"]), low=20.0, high=80.0),
                "last_applied_mode": payload.get("last_applied_mode"),
                "last_applied_key": payload.get("last_applied_key"),
                "last_applied_at": self._normalize_optional_int(payload.get("last_applied_at")),
                "last_result": payload.get("last_result"),
            }
            self._validate_targets(normalized)
        except Exception as e:
            logger.warning("ACSNightEcoScheduler load error: %s", e)
            return

        with self._lock:
            self._data.update(normalized)

    def _save_unlocked(self) -> None:
        ACS_NIGHT_ECO_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACS_NIGHT_ECO_FILE.write_text(json.dumps(self._data))

    def _normalize_time_hhmm(self, value: Any) -> str:
        hour, minute = self._split_hhmm(value)
        return "{:02d}:{:02d}".format(hour, minute)

    def _split_hhmm(self, value: Any) -> tuple[int, int]:
        parts = str(value or "").strip().split(":")
        if len(parts) != 2:
            raise ValueError("time_hhmm deve essere HH:MM")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("time_hhmm non valido")
        return hour, minute

    def _normalize_target(self, value: Any, *, low: float, high: float) -> float:
        target = float(value)
        if target < low or target > high:
            raise ValueError("target fuori range")
        return round(target, 1)

    def _validate_targets(self, data: Dict[str, Any]) -> None:
        start_hhmm = data["start_hhmm"]
        end_hhmm = data["end_hhmm"]
        if start_hhmm == end_hhmm:
            raise ValueError("La fascia notte non puo avere stesso orario di inizio e fine")
        if float(data["night_pdc_target_c"]) > float(data["day_pdc_target_c"]):
            raise ValueError("Il target PDC notte deve essere <= del target giorno")
        if float(data["night_recirc_target_c"]) > float(data["day_recirc_target_c"]):
            raise ValueError("Il target ricircolo notte deve essere <= del target giorno")

    def _normalize_optional_int(self, value: Any) -> Optional[int]:
        if value in (None, ""):
            return None
        return int(value)

    def _try_float(self, value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:
            return None


__all__ = [
    "ACSStore",
    "ACSMQTTBridge",
    "ACSAntilegScheduler",
    "ACSNightEcoScheduler",
    "ACS_ROLES",
]
