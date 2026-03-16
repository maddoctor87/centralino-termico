from datetime import date, datetime, time as dt_time, timedelta
from functools import lru_cache
from pathlib import Path
import io
import imaplib
import os
import re
import smtplib
import time
import threading
import logging
import hmac
import json
import secrets
import jwt  # PyJWT
import math
import urllib.parse
from typing import Optional, Any, Literal, Dict, List

import qrcode
import requests
from PIL import Image, ImageDraw
from email import encoders
from email.header import decode_header
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from email.policy import default as email_default
from email.utils import formatdate, getaddresses, make_msgid, parseaddr
from requests.auth import HTTPBasicAuth, HTTPDigestAuth
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException
from fastapi import FastAPI, Depends, HTTPException, status, Header, Response, Query, Request, UploadFile, File, Form, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, PlainTextResponse, FileResponse
from pydantic import BaseModel, Field, ConfigDict, model_validator, AliasChoices
import paho.mqtt.publish as mqtt_publish
import yaml
from pywebpush import webpush, WebPushException

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 fallback if package installed
    from backports.zoneinfo import ZoneInfo
from . import events as ev
from . import auth_db as adb
from . import tasks as tk
from . import worklogs as wl
from . import bookings as bk
from . import external_cal as xcal
from . import inventory as inv
from . import meetings as mt
from .yolo_state import yolo_store, alert_store
from .yolo_rules import rules_engine
from .device_registry import DeviceStore, DeviceMQTTBridge, build_device_configs
from . import acs as acs_module
from . import notifications as notif
from .hotspot import (
    HotspotManager,
    HotspotNotConfigured,
    HotspotUnavailable,
    GuestDeviceLimitExceeded,
    normalise_mac,
)

app = FastAPI()

logger = logging.getLogger("backend.devices")
logger.setLevel(logging.INFO)
hotspot_logger = logging.getLogger("backend.hotspot")
hotspot_logger.setLevel(logging.INFO)

APP_ENV = (os.environ.get("APP_ENV") or "prod").strip().lower()

SESSION_COOKIE_NAME = os.environ.get("SESSION_COOKIE_NAME", "pm_session")
CSRF_COOKIE_NAME = os.environ.get("CSRF_COOKIE_NAME", "pm_csrf")
CSRF_HEADER_NAME = os.environ.get("CSRF_HEADER_NAME", "X-CSRF-Token")
COOKIE_SECURE = APP_ENV in {"prod", "production"}
AUTH_MAX_FAILS = int(os.environ.get("AUTH_MAX_FAILS", "8"))
AUTH_FAIL_WINDOW_SECONDS = int(os.environ.get("AUTH_FAIL_WINDOW_SECONDS", "900"))
AUTH_BAN_SECONDS = int(os.environ.get("AUTH_BAN_SECONDS", "3600"))


def _parse_cors_origins(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = [p.strip() for p in str(raw).split(",")]
    return [p for p in parts if p]


cors_origins = _parse_cors_origins(os.environ.get("CORS_ALLOW_ORIGINS"))
if APP_ENV in {"prod", "production"} and not cors_origins:
    raise RuntimeError("CORS_ALLOW_ORIGINS is required in production (comma-separated list)")

# CORS per il frontend React
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- security helpers (client IP, autoban, CSRF) ---
def _client_ip(request: Request) -> str:
    xff = request.headers.get('x-forwarded-for') or request.headers.get('X-Forwarded-For')
    if xff:
        # first hop is the original client
        return xff.split(',')[0].strip()
    client = getattr(request, 'client', None)
    if client and client.host:
        return str(client.host)
    return 'unknown'

@app.middleware('http')
async def _autoban_middleware(request: Request, call_next):
    # Apply bans only to auth-sensitive endpoints and protected UIs
    path = request.url.path or ''
    if path.startswith('/api/auth') or path in {'/_auth'}:
        ip = _client_ip(request)
        banned, until_ts, reason = adb.is_ip_banned(ip)
        if banned:
            return PlainTextResponse(
                'Banned',
                status_code=429,
                headers={'Retry-After': str(max(1, until_ts - int(time.time())))},
            )

    # CSRF protection for cookie-based sessions (unsafe methods)
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and path.startswith('/api/'):
        # Public endpoints are excluded
        if path.startswith('/api/public/') or path in {'/api/status', '/api/auth/login'}:
            return await call_next(request)

        # If the request uses an explicit Bearer token, treat it as API-style auth:
        # CSRF does not apply because browsers do not automatically attach Authorization headers.
        auth_header = request.headers.get('authorization') or request.headers.get('Authorization')
        if auth_header and auth_header.lower().startswith('bearer '):
            return await call_next(request)

        # Only enforce CSRF when a cookie-based session is in use.
        session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if not session_cookie:
            return await call_next(request)

        csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME)
        csrf_header = request.headers.get(CSRF_HEADER_NAME)
        if csrf_cookie and csrf_header and hmac.compare_digest(csrf_cookie, csrf_header):
            return await call_next(request)
        return PlainTextResponse('CSRF required', status_code=403)

    return await call_next(request)

# --- Config ---
AUTH_SECRET = os.environ.get("AUTH_SECRET")
ADMIN_USER = os.environ.get("ADMIN_USER")
ADMIN_PASS = os.environ.get("ADMIN_PASS")
USER_USER = os.environ.get("USER_USER")
USER_PASS = os.environ.get("USER_PASS")
DEVICE_API_KEY = os.environ.get("DEVICE_API_KEY")
TOKEN_EXP_SECONDS = int(os.environ.get("TOKEN_EXP_SECONDS", "86400"))  # 24h
MEETINGS_ICS_TOKEN = os.environ.get("MEETINGS_ICS_TOKEN")
APP_TIMEZONE = ZoneInfo(os.environ.get("APP_TZ", "Europe/Rome"))
YOLO_WEBHOOK_KEY = os.environ.get("YOLO_WEBHOOK_KEY") or DEVICE_API_KEY
YOLO_CAMERAS_FILE = Path(os.environ.get("YOLO_CAMERAS_FILE", "yolo/cameras.yml")).resolve()
YOLO_VEHICLE_RECORDINGS_DIR = Path(os.environ.get("YOLO_VEHICLE_RECORDINGS_DIR", "/app/backend_data/vehicle_recordings")).resolve()
YOLO_VEHICLE_RECORDING_EXTS = {".mkv", ".mp4", ".mov", ".avi", ".webm"}
ONVIF_USER = os.environ.get("ONVIF_USER")
ONVIF_PASS = os.environ.get("ONVIF_PASS")
ONVIF_AUTH = (os.environ.get("ONVIF_AUTH") or "digest").strip().lower()
RTSP_PUBLIC_HOST = (os.environ.get("RTSP_PUBLIC_HOST") or "").strip()
RTSP_PUBLIC_PORT = (os.environ.get("RTSP_PUBLIC_PORT") or "").strip()
NVR_URL  = (os.environ.get("NVR_URL") or "").rstrip("/")
NVR_USER = (os.environ.get("NVR_USER") or ONVIF_USER or "").strip()
NVR_PASS = (os.environ.get("NVR_PASS") or ONVIF_PASS or "").strip()

# --- config validation (fail fast in prod) ---
if APP_ENV in {"prod", "production"}:
    if not AUTH_SECRET or AUTH_SECRET.strip() in {"", "change-me"} or len(AUTH_SECRET.strip()) < 32:
        raise RuntimeError("AUTH_SECRET is required in production (min 32 chars)")
    if not MEETINGS_ICS_TOKEN or MEETINGS_ICS_TOKEN.strip() in {"", "change-me-meetings-ics"}:
        raise RuntimeError("MEETINGS_ICS_TOKEN is required in production")
    if not DEVICE_API_KEY or DEVICE_API_KEY.strip() == "":
        raise RuntimeError("DEVICE_API_KEY is required in production")


ALL_ROLES = ["superad", "admin", "reception", "maintenance", "cleaning"]

MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER")
MQTT_PASS = os.environ.get("MQTT_PASS")

WIFI_SSID = os.environ.get("WIFI_SSID")
_WIFI_SECURITY_RAW = os.environ.get("WIFI_SECURITY")
WIFI_PASSWORD = os.environ.get("WIFI_PASSWORD")
WIFI_HIDDEN = (os.environ.get("WIFI_HIDDEN") or "").strip().lower() in {"1", "true", "yes", "on"}
WIFI_PORTAL_URL = os.environ.get("WIFI_PORTAL_URL")

def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


HOTSPOT_HOST = os.environ.get("HOTSPOT_HOST")
HOTSPOT_USER = os.environ.get("HOTSPOT_USER")
HOTSPOT_PASS = os.environ.get("HOTSPOT_PASS")
HOTSPOT_GUEST_MAX_DEVICES = _int_env("HOTSPOT_GUEST_MAX_DEVICES", 3)
HOTSPOT_GUEST_GRACE_SECONDS = _int_env("HOTSPOT_GUEST_GRACE_SECONDS", 6 * 3600)

MAIL_CREDENTIALS_FILE = os.environ.get("MAIL_CREDENTIALS_FILE")
MAIL_ACCOUNTS_RAW = os.environ.get("MAIL_ACCOUNTS")
MAIL_DEFAULT_DOMAIN = (os.environ.get("MAIL_DEFAULT_DOMAIN") or "").strip()
MAIL_IMAP_HOST = os.environ.get("MAIL_IMAP_HOST", "mail.itinerisresort.com")
MAIL_IMAP_PORT = _int_env("MAIL_IMAP_PORT", 993)
MAIL_IMAP_SSL = _bool_env("MAIL_IMAP_SSL", True)
MAIL_BODY_MAX_CHARS = _int_env("MAIL_BODY_MAX_CHARS", 200000)
MAIL_SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", "mail.itinerisresort.com")
MAIL_SMTP_PORT = _int_env("MAIL_SMTP_PORT", 465)
MAIL_SMTP_SSL = _bool_env("MAIL_SMTP_SSL", True)
MAIL_SMTP_STARTTLS = _bool_env("MAIL_SMTP_STARTTLS", False)
MAIL_SENT_FOLDER = os.environ.get("MAIL_SENT_FOLDER", "Sent")
MAIL_ATTACHMENT_MAX_BYTES = _int_env("MAIL_ATTACHMENT_MAX_BYTES", 25 * 1024 * 1024)

# --- Energy guard (Linea B) ---
ENERGY_LINE_B_GUARD_ENABLED = _bool_env("ENERGY_LINE_B_GUARD_ENABLED", False)
ENERGY_LINE_B_DEVICE_ID = os.environ.get("ENERGY_LINE_B_DEVICE_ID", "refoss_em06_linea_b").strip()
ENERGY_LINE_B_POWER_KEY = os.environ.get("ENERGY_LINE_B_POWER_KEY", "power").strip() or "power"
ENERGY_LINE_B_THRESHOLD_W = _int_env("ENERGY_LINE_B_THRESHOLD_W", 1000)
ENERGY_LINE_B_MIN_DURATION_S = _int_env("ENERGY_LINE_B_MIN_DURATION_S", 10)
ENERGY_LINE_B_COOLDOWN_S = _int_env("ENERGY_LINE_B_COOLDOWN_S", 300)
ENERGY_LINE_B_RESET_S = _int_env("ENERGY_LINE_B_RESET_S", 60)
ENERGY_LINE_B_USE_ABS = _bool_env("ENERGY_LINE_B_USE_ABS", True)
ENERGY_LINE_B_POLL_S = _int_env("ENERGY_LINE_B_POLL_S", 5)
ENERGY_LINE_B_UNITS_RAW = os.environ.get("ENERGY_LINE_B_UNITS", "1,2,3")
ENERGY_LINE_B_AUTORESTORE = _bool_env("ENERGY_LINE_B_AUTORESTORE", False)

# --- Telegram notifications ---
TELEGRAM_BOT_TOKEN = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
TELEGRAM_API_BASE = (os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/")

# --- Web push notifications ---
WEBPUSH_ENABLED = _bool_env("WEBPUSH_ENABLED", False)
WEBPUSH_VAPID_PUBLIC = (os.environ.get("WEBPUSH_VAPID_PUBLIC") or "").strip()
WEBPUSH_VAPID_PRIVATE = (os.environ.get("WEBPUSH_VAPID_PRIVATE") or "").strip()
WEBPUSH_VAPID_SUBJECT = (os.environ.get("WEBPUSH_VAPID_SUBJECT") or "mailto:info@itinerisresort.com").strip()

DEVICE_CONFIGS = build_device_configs()
device_store = DeviceStore()
for cfg in DEVICE_CONFIGS:
    device_store.register(cfg["id"], cfg)

DEVICE_MQTT_SETTINGS = {
    "host": MQTT_HOST,
    "port": MQTT_PORT,
    "username": MQTT_USER,
    "password": MQTT_PASS,
    "client_id": os.environ.get("DEVICE_MQTT_CLIENT_ID", "piscina-backend-devices"),
}

device_bridge: DeviceMQTTBridge | None = None

# --- ACS (Centrale Termica) ---
acs_store = acs_module.ACSStore()
acs_bridge: acs_module.ACSMQTTBridge | None = None
energy_guard_thread: threading.Thread | None = None
energy_guard_stop = threading.Event()
energy_history_thread: threading.Thread | None = None
energy_history_stop = threading.Event()

ENERGY_HISTORY_ENABLED = _bool_env("ENERGY_HISTORY_ENABLED", True)
ENERGY_HISTORY_INTERVAL_S = max(60, _int_env("ENERGY_HISTORY_INTERVAL_S", 3600))
ENERGY_HISTORY_FILE = Path(os.environ.get("ENERGY_HISTORY_FILE", "data/energy_hourly.jsonl")).resolve()

hotspot_manager = HotspotManager(
    HOTSPOT_HOST,
    HOTSPOT_USER,
    HOTSPOT_PASS,
    max_guest_devices=HOTSPOT_GUEST_MAX_DEVICES,
    guest_grace_seconds=HOTSPOT_GUEST_GRACE_SECONDS,
)

STAFF_WIFI_ROLES = {"superad", "admin", "reception", "maintenance", "cleaning"}
THERMAL_COMMAND_TOPIC = os.environ.get("THERMAL_COMMAND_TOPIC", "itineris/thermal/command")
THERMAL_CONTROL_ROLES = ["superad", "admin", "maintenance"]
FANCOIL_COMMAND_TOPIC = os.environ.get("FANCOIL_COMMAND_TOPIC", "itineris/fancoil/command")

THERMAL_FC_PANEL_MAP: Dict[int, int] = {
    1: 30,
    2: 31,
    3: 32,
    4: 33,
    5: 34,
    6: 35,
    7: 36,
    8: 37,
    9: 38,
    10: 39,
    11: 40,
    12: 41,
    13: 42,
    14: 43,
    15: 44,
    16: 45,
    17: 46,
    18: 47,
    19: 48,
    20: 49,
    21: 50,
    22: 51,
    23: 52,
    24: 53,
    25: 54,
    26: 55,
}
THERMAL_ZONE_REGISTER_BASE = 285
THERMAL_REGISTER_UNIT_ENABLE_BASE = 74
THERMAL_COIL_UNIT_ENABLE_BASE = 4
THERMAL_MAX_FANCOILS = int(os.environ.get("THERMAL_MAX_FANCOILS", "32"))


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return float(default)
    try:
        return float(raw)
    except ValueError:
        return float(default)


def _parse_int_list(raw: str) -> list[int]:
    if not raw:
        return []
    items = []
    for chunk in str(raw).replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            items.append(int(chunk))
        except ValueError:
            continue
    return items


THERMAL_PRESET_COMFORT = _env_float("THERMAL_PRESET_COMFORT", 20.0)
THERMAL_PRESET_ECONOMY = _env_float("THERMAL_PRESET_ECONOMY", 18.0)
THERMAL_PRESET_COMFORT_HEAT = _env_float(
    "THERMAL_PRESET_COMFORT_HEAT", THERMAL_PRESET_COMFORT
)
THERMAL_PRESET_COMFORT_COOL = _env_float(
    "THERMAL_PRESET_COMFORT_COOL", THERMAL_PRESET_COMFORT
)
THERMAL_PRESET_ECONOMY_HEAT = _env_float(
    "THERMAL_PRESET_ECONOMY_HEAT", 17.0
)
THERMAL_PRESET_ECONOMY_COOL = _env_float(
    "THERMAL_PRESET_ECONOMY_COOL", 30.0
)
THERMAL_PRESET_RIPOSO_HEAT = _env_float(
    "THERMAL_PRESET_RIPOSO_HEAT", 14.0
)
THERMAL_PRESET_RIPOSO_COOL = _env_float(
    "THERMAL_PRESET_RIPOSO_COOL", 30.0
)
THERMAL_PRESET_TARGETS = {
    "comfort": {
        "heat": THERMAL_PRESET_COMFORT_HEAT,
        "cool": THERMAL_PRESET_COMFORT_COOL,
        "default": THERMAL_PRESET_COMFORT,
    },
    "economy": {
        "heat": THERMAL_PRESET_ECONOMY_HEAT,
        "cool": THERMAL_PRESET_ECONOMY_COOL,
        "default": THERMAL_PRESET_ECONOMY,
    },
    "riposo": {
        "heat": THERMAL_PRESET_RIPOSO_HEAT,
        "cool": THERMAL_PRESET_RIPOSO_COOL,
        "default": THERMAL_PRESET_RIPOSO_HEAT,
    },
}
THERMAL_PRESET_TARGETS["sleep"] = THERMAL_PRESET_TARGETS["riposo"]
THERMAL_PRESET_TARGETS["confort"] = THERMAL_PRESET_TARGETS["comfort"]
THERMAL_SETPOINT_MIN_C = _env_float("THERMAL_SETPOINT_MIN_C", 5.0)
THERMAL_SETPOINT_MAX_C = _env_float("THERMAL_SETPOINT_MAX_C", 35.0)
THERMAL_MODBUS_HOST = os.environ.get("THERMAL_MODBUS_HOST", "192.168.50.200")
THERMAL_MODBUS_PORT = int(os.environ.get("THERMAL_MODBUS_PORT", "502"))
THERMAL_MODBUS_UNIT = int(os.environ.get("THERMAL_MODBUS_UNIT", "1"))
THERMAL_MODBUS_TIMEOUT = float(os.environ.get("THERMAL_MODBUS_TIMEOUT", "2.5"))
THERMAL_REGISTER_FORCE_MODE_BASE = 606
THERMAL_REGISTER_HOT_SET_BASE = 734
THERMAL_REGISTER_COOL_SET_BASE = 798
THERMAL_REGISTER_TEMP_BASE = 222
FAN_MODE_RAW_TO_KEY = {
    0: "local",
    1: "off",
    2: "auto",
    3: "speed1",
    4: "speed2",
    5: "speed3",
    6: "aux",
}

FANCOIL_WARMUP_LOG_PATH = Path(
    os.environ.get("FANCOIL_WARMUP_LOG", "data/fancoil_warmup_log.csv")
).resolve()


def _publish_mqtt_message(
    topic: str,
    payload: dict[str, Any],
    *,
    qos: int = 1,
    retries: int = 2,
    retry_delay_s: float = 0.20,
) -> None:
    auth = None
    username = DEVICE_MQTT_SETTINGS.get("username")
    password = DEVICE_MQTT_SETTINGS.get("password")
    if username:
        auth = {"username": username, "password": password or ""}

    last_exc = None
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            mqtt_publish.single(
                topic,
                payload=json.dumps(payload),
                hostname=DEVICE_MQTT_SETTINGS["host"],
                port=DEVICE_MQTT_SETTINGS["port"],
                auth=auth,
                client_id=os.environ.get("THERMAL_COMMAND_CLIENT_ID", "piscina-backend-thermal"),
                qos=qos,
                retain=False,
            )
            if attempt > 1:
                logger.info("MQTT publish ok after retry attempt=%s topic=%s", attempt, topic)
            return
        except Exception as exc:
            last_exc = exc
            logger.warning("MQTT publish failed attempt=%s/%s topic=%s err=%s", attempt, attempts, topic, exc)
            if attempt < attempts:
                time.sleep(max(0.05, float(retry_delay_s)))

    raise last_exc if last_exc else RuntimeError("MQTT publish failed")


# --- Mail helpers ---
_MAIL_UID_RE = re.compile(rb"UID (\d+)")
_MAIL_SIZE_RE = re.compile(rb"RFC822.SIZE (\d+)")
_MAIL_FLAGS_RE = re.compile(rb"FLAGS \((.*?)\)")


def _decode_header_value(value: str | bytes | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded += part.decode(enc or "utf-8", errors="replace")
        else:
            decoded += str(part)
    return decoded


def _format_address(value: str | None) -> str:
    if not value:
        return ""
    name, addr = parseaddr(value)
    name = _decode_header_value(name)
    if name and addr:
        return f"{name} <{addr}>"
    return addr or name or value


def _clean_header(value: str | None) -> str:
    value = (value or "").strip()
    return value.replace("\r", " ").replace("\n", " ")


def _normalize_mail_user(user: str) -> str:
    user = user.strip()
    if not user:
        return user
    if "@" not in user or user.endswith("@"):
        if MAIL_DEFAULT_DOMAIN:
            return f"{user.rstrip('@')}@{MAIL_DEFAULT_DOMAIN}"
    return user


@lru_cache(maxsize=1)
def _load_mail_accounts() -> dict[str, str]:
    accounts: dict[str, str] = {}
    raw_env = (MAIL_ACCOUNTS_RAW or "").strip()
    if raw_env:
        if raw_env.startswith("{"):
            try:
                data = json.loads(raw_env)
                if isinstance(data, dict):
                    for user, password in data.items():
                        user = _normalize_mail_user(str(user))
                        password = str(password)
                        if user and password:
                            accounts[user] = password
            except Exception:
                pass
        if not accounts:
            entries = [part.strip() for part in raw_env.split(";") if part.strip()]
            for entry in entries:
                if "|" in entry:
                    user, password = entry.split("|", 1)
                elif ":" in entry:
                    user, password = entry.split(":", 1)
                else:
                    continue
                user = _normalize_mail_user(user.strip())
                password = password.strip()
                if user and password:
                    accounts[user] = password
        if accounts:
            return accounts
    if not MAIL_CREDENTIALS_FILE:
        return {}
    path = Path(MAIL_CREDENTIALS_FILE)
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        user = _normalize_mail_user(parts[0])
        password = " ".join(parts[1:]).strip()
        if not user or not password:
            continue
        accounts[user] = password
    return accounts


def _get_mail_accounts() -> dict[str, str]:
    return dict(_load_mail_accounts())


def _get_mail_password(account: str) -> str:
    accounts = _get_mail_accounts()
    if account not in accounts:
        raise HTTPException(status_code=404, detail="Casella non trovata")
    return accounts[account]


def _imap_connect(account: str, password: str) -> imaplib.IMAP4:
    if MAIL_IMAP_SSL:
        server = imaplib.IMAP4_SSL(MAIL_IMAP_HOST, MAIL_IMAP_PORT)
    else:
        server = imaplib.IMAP4(MAIL_IMAP_HOST, MAIL_IMAP_PORT)
    server.login(account, password)
    return server


def _parse_fetch_meta(meta: bytes) -> dict[str, Any]:
    uid = None
    size = None
    flags: list[str] = []
    if meta:
        match = _MAIL_UID_RE.search(meta)
        if match:
            uid = int(match.group(1))
        match = _MAIL_SIZE_RE.search(meta)
        if match:
            size = int(match.group(1))
        match = _MAIL_FLAGS_RE.search(meta)
        if match:
            raw_flags = match.group(1).split()
            flags = [f.decode(errors="ignore") for f in raw_flags]
    return {"uid": uid, "size": size, "flags": flags}


def _parse_header_bytes(header_bytes: bytes) -> dict[str, str]:
    if not header_bytes:
        return {"from": "", "to": "", "subject": "", "date": ""}
    msg = BytesParser(policy=email_default).parsebytes(header_bytes)
    return {
        "from": _clean_header(_format_address(msg.get("from", ""))),
        "to": _clean_header(_format_address(msg.get("to", ""))),
        "subject": _clean_header(_decode_header_value(msg.get("subject", ""))),
        "date": _clean_header(msg.get("date", "") or ""),
    }


def _truncate_text(value: str, limit: int) -> str:
    if limit <= 0:
        return value
    if value and len(value) > limit:
        return value[: max(0, limit - 3)] + "..."
    return value


def _iter_message_parts(message):
    idx = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        yield idx, part
        idx += 1


def _safe_filename(name: str | None, fallback: str) -> str:
    name = _clean_header(_decode_header_value(name))
    name = name.replace("/", "_").replace("\\", "_").replace("\"", "'")
    name = name.strip()
    return name or fallback


def _fetch_message_bytes(server: imaplib.IMAP4, uid: int) -> bytes | None:
    status, data = server.uid("fetch", str(uid), "(RFC822)")
    if status != "OK":
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) > 1:
            return item[1]
    return None


def _parse_recipients(raw_value: str) -> list[str]:
    addresses = []
    for _name, addr in getaddresses([raw_value]):
        addr = (addr or "").strip()
        if addr:
            addresses.append(addr)
    return addresses


def _build_email_message(
    *,
    account: str,
    to_raw: str,
    subject: str,
    text: str | None,
    html: str | None,
    attachments: list[tuple[str, str, bytes]],
    in_reply_to: str | None = None,
    references: str | None = None,
) -> tuple[bytes, list[str]]:
    to_addrs = _parse_recipients(to_raw)
    if not to_addrs:
        raise HTTPException(status_code=400, detail="Destinatario non valido")

    if attachments:
        root = MIMEMultipart("mixed")
        alt = MIMEMultipart("alternative")
        if text:
            alt.attach(MIMEText(text, "plain", "utf-8"))
        if html:
            alt.attach(MIMEText(html, "html", "utf-8"))
        if alt.get_payload():
            root.attach(alt)
        msg = root
    else:
        if html and text:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(text, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
        elif html:
            msg = MIMEText(html, "html", "utf-8")
        else:
            msg = MIMEText(text or "", "plain", "utf-8")

    msg["From"] = account
    msg["To"] = to_raw
    msg["Subject"] = subject or ""
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    for filename, content_type, data in attachments:
        maintype, subtype = (content_type or "application/octet-stream").split("/", 1)
        part = MIMEBase(maintype, subtype)
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)

    raw_bytes = msg.as_bytes()
    return raw_bytes, to_addrs


def _smtp_send_message(account: str, password: str, to_addrs: list[str], raw_msg: bytes) -> None:
    if MAIL_SMTP_SSL:
        server = smtplib.SMTP_SSL(MAIL_SMTP_HOST, MAIL_SMTP_PORT, timeout=20)
    else:
        server = smtplib.SMTP(MAIL_SMTP_HOST, MAIL_SMTP_PORT, timeout=20)
    try:
        server.ehlo()
        if not MAIL_SMTP_SSL and MAIL_SMTP_STARTTLS:
            server.starttls()
            server.ehlo()
        server.login(account, password)
        server.sendmail(account, to_addrs, raw_msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def _append_sent_message(account: str, password: str, raw_msg: bytes) -> None:
    server = None
    try:
        server = _imap_connect(account, password)
        date_time = imaplib.Time2Internaldate(time.time())
        server.append(MAIL_SENT_FOLDER, "(\\Seen)", date_time, raw_msg)
    finally:
        if server is not None:
            try:
                server.logout()
            except Exception:
                pass

# --- Models ---
def _resolve_fc_target(fc_id: int) -> dict[str, int | None]:
    """Resolve a fan-coil target.

    Note: in this project the Modbus *control plane* is VMF-E6 (unit-id THERMAL_MODBUS_UNIT).
    Room panels (VMF-E4/E4X) are treated as HMI/read-only for remote supervision; any
    attempted panel write is intentionally a no-op at higher layers.

    The panel map is kept only as metadata (for UI grouping / troubleshooting).
    """
    if not isinstance(fc_id, int) or fc_id < 1 or fc_id > THERMAL_MAX_FANCOILS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fan-coil non valido")
    panel = THERMAL_FC_PANEL_MAP.get(fc_id)
    idx = fc_id - 1
    register = THERMAL_ZONE_REGISTER_BASE + idx
    return {
        "fc_id": fc_id,
        "panel": panel,
        "panel_address": panel,
        "register": register,
        "controller_unit": THERMAL_MODBUS_UNIT,
    }


def _normalize_temperature_c(value: float) -> float:
    if not math.isfinite(value):
        raise HTTPException(status_code=400, detail="Temperatura non valida")
    if value < THERMAL_SETPOINT_MIN_C or value > THERMAL_SETPOINT_MAX_C:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Temperatura fuori intervallo consentito "
                f"{THERMAL_SETPOINT_MIN_C:.1f}–{THERMAL_SETPOINT_MAX_C:.1f} °C"
            ),
        )
    return float(value)


def _publish_zone_setpoint(
    fc_id: int,
    *,
    heat_c: float | None = None,
    cool_c: float | None = None,
    preset: str | None = None,
) -> None:
    target = _resolve_fc_target(fc_id)
    if heat_c is None and cool_c is None:
        raise HTTPException(status_code=400, detail="Temperatura non specificata")
    if heat_c is None:
        heat_c = cool_c
    if cool_c is None:
        cool_c = heat_c
    heat_c = _normalize_temperature_c(float(heat_c))
    cool_c = _normalize_temperature_c(float(cool_c))
    heat_raw = int(round(heat_c * 10))
    cool_raw = int(round(cool_c * 10))
    payload = {
        "type": "zone_setpoint",
        "fc_id": target["fc_id"],
        "fc": target["fc_id"],
        "id": target["fc_id"],
        "unit": target["fc_id"],
        "panel": target["panel"],
        "panel_address": target["panel_address"],
        "register": target["register"],
        "temperature_heat_c": heat_c,
        "temperature_heat_raw": heat_raw,
        "temperature_cool_c": cool_c,
        "temperature_cool_raw": cool_raw,
        "temperature_c": heat_c,
        "temperature_raw": heat_raw,
        "source": "backend",
        "controller": "vmf-e6",
    }
    logger.info(
        "FC CMD zone_setpoint fc_id=%s panel=%s register=%s heat_c=%.1f cool_c=%.1f payload=%s",
        target["fc_id"],
        target["panel"],
        target["register"],
        heat_c,
        cool_c,
        payload,
    )
    if preset:
        payload["preset"] = preset
    try:
        _publish_mqtt_message(FANCOIL_COMMAND_TOPIC, payload, qos=1, retries=3, retry_delay_s=0.20)
    except Exception as exc:  # pragma: no cover - MQTT failure path
        logger.exception(
            "Errore pubblicazione comando setpoint (fc_id=%s)", fc_id,
        )
        raise HTTPException(status_code=500, detail="MQTT publish failed") from exc


def _publish_zone_mode(fc_id: int, *, mode: str) -> None:
    target = _resolve_fc_target(fc_id)
    payload = {
        "type": "zone_mode",
        "fc_id": target["fc_id"],
        "fc": target["fc_id"],
        "id": target["fc_id"],
        "unit": target["fc_id"],
        "panel": target["panel"],
        "panel_address": target["panel_address"],
        "register": target["register"],
        "mode": mode,
        "source": "backend",
        "controller": "vmf-e6",
    }
    logger.info(
        "FC CMD zone_mode fc_id=%s panel=%s register=%s mode=%s payload=%s",
        target["fc_id"],
        target["panel"],
        target["register"],
        mode,
        payload,
    )
    try:
        _publish_mqtt_message(FANCOIL_COMMAND_TOPIC, payload, qos=1, retries=3, retry_delay_s=0.20)
    except Exception as exc:  # pragma: no cover - MQTT failure path
        logger.exception(
            "Errore pubblicazione comando fan mode (fc_id=%s)", fc_id,
        )
        raise HTTPException(status_code=500, detail="MQTT publish failed") from exc


def _publish_zone_force_setpoint(fc_id: int, *, enabled: bool) -> None:
    target = _resolve_fc_target(fc_id)
    payload = {
        "type": "force_setpoint",
        "fc": target["fc_id"],
        "id": target["fc_id"],
        "unit": target["fc_id"],
        "panel": target["panel"],
        "panel_address": target["panel_address"],
        "register": target["register"],
        "force": bool(enabled),
        "force_setpoint": bool(enabled),
        "enabled": bool(enabled),
        "value": 1 if enabled else 0,
        "source": "backend",
        "controller": "vmf-e6",
    }
    logger.info(
        "FC CMD force_setpoint fc_id=%s panel=%s register=%s enabled=%s payload=%s",
        target["fc_id"],
        target["panel"],
        target["register"],
        bool(enabled),
        payload,
    )
    try:
        _publish_mqtt_message(FANCOIL_COMMAND_TOPIC, payload, qos=1, retries=3, retry_delay_s=0.20)
    except Exception as exc:  # pragma: no cover - MQTT failure path
        logger.exception(
            "Errore pubblicazione forzatura setpoint (fc_id=%s)", fc_id,
        )
        raise HTTPException(status_code=500, detail="MQTT publish failed") from exc


def _publish_fancoil_toggle(fc_id: int, *, enabled: bool) -> None:
    target = _resolve_fc_target(fc_id)
    payload = {
        "type": "toggle",
        "fc": target["fc_id"],
        "id": target["fc_id"],
        "unit": target["fc_id"],
        "panel": target["panel"],
        "panel_address": target["panel_address"],
        "register": target["register"],
        "enable": bool(enabled),
        "enabled": bool(enabled),
        "value": 1 if enabled else 0,
        "source": "backend",
        "controller": "vmf-e6",
    }
    logger.info(
        "FC CMD toggle fc_id=%s panel=%s register=%s enabled=%s payload=%s",
        target["fc_id"],
        target["panel"],
        target["register"],
        bool(enabled),
        payload,
    )
    try:
        _publish_mqtt_message(FANCOIL_COMMAND_TOPIC, payload, qos=1, retries=3, retry_delay_s=0.20)
    except Exception as exc:  # pragma: no cover - MQTT failure path
        logger.exception(
            "Errore pubblicazione comando fancoil (fc_id=%s)", fc_id,
        )
        raise HTTPException(status_code=500, detail="MQTT publish failed") from exc


def _read_modbus_register(client: ModbusTcpClient, address: int) -> int | None:
    for attempt in range(3):
        try:
            response = client.read_holding_registers(
                address=address,
                count=1,
                slave=THERMAL_MODBUS_UNIT,
            )
        except ModbusException:
            response = None
        if response and not response.isError():
            return int(response.registers[0])
        time.sleep(0.05)
    return None


def _read_fc_modbus_status(fc_id: int) -> dict[str, Any]:
    target = _resolve_fc_target(fc_id)
    client = ModbusTcpClient(
        host=THERMAL_MODBUS_HOST,
        port=THERMAL_MODBUS_PORT,
        timeout=THERMAL_MODBUS_TIMEOUT,
    )
    if not client.connect():
        raise HTTPException(
            status_code=503,
            detail="Gateway Modbus non raggiungibile",
        )
    idx = fc_id - 1
    try:
        force_mode_raw = _read_modbus_register(
            client, THERMAL_REGISTER_FORCE_MODE_BASE + idx
        )
        hot_raw = _read_modbus_register(client, THERMAL_REGISTER_HOT_SET_BASE + idx)
        cool_raw = _read_modbus_register(client, THERMAL_REGISTER_COOL_SET_BASE + idx)
        ambient_raw = _read_modbus_register(client, THERMAL_REGISTER_TEMP_BASE + idx)
    finally:
        client.close()

    def _to_celsius(raw_value: int | None) -> float | None:
        if raw_value is None:
            return None
        return float(raw_value) / 10.0

    fan_mode_key = FAN_MODE_RAW_TO_KEY.get(force_mode_raw)
    payload = {
        "fc_id": fc_id,
        "panel": target["panel"],
        "register": target["register"],
        "force_mode_raw": force_mode_raw,
        "setpoint_heat_raw": hot_raw,
        "setpoint_heat_c": _to_celsius(hot_raw),
        "setpoint_cool_raw": cool_raw,
        "setpoint_cool_c": _to_celsius(cool_raw),
        "ambient_raw": ambient_raw,
        "ambient_c": _to_celsius(ambient_raw),
        "fan_mode_key": fan_mode_key,
        "timestamp": int(time.time()),
    }
    return payload


def _execute_modbus_writes(actions: list[tuple[str, int, int | bool]]):
    client = ModbusTcpClient(
        host=THERMAL_MODBUS_HOST,
        port=THERMAL_MODBUS_PORT,
        timeout=THERMAL_MODBUS_TIMEOUT,
    )
    if not client.connect():
        raise HTTPException(status_code=503, detail="Gateway Modbus non raggiungibile")
    try:
        for kind, address, value in actions:
            if kind == "register":
                response = client.write_register(
                    address=address,
                    value=int(value),
                    device_id=THERMAL_MODBUS_UNIT,
                )
            elif kind == "coil":
                response = client.write_coil(
                    address=address,
                    value=bool(value),
                    device_id=THERMAL_MODBUS_UNIT,
                )
            else:
                raise HTTPException(status_code=500, detail="Tipo comando Modbus non valido")
            if response.isError():
                raise HTTPException(status_code=500, detail=f"Scrittura Modbus fallita @ {address}")
    except ModbusException as exc:
        raise HTTPException(status_code=500, detail=f"Errore Modbus: {exc}") from exc
    finally:
        client.close()


def _set_unit_enable(unit_id: int, enabled: bool):
    payload = {
        "type": "toggle",
        "pdc": unit_id,
        "unit": unit_id,
        "enable": enabled,
        "enabled": enabled,
        "value": 1 if enabled else 0,
        "source": "backend",
        "controller": "vmf-e6",
    }
    try:
        _publish_mqtt_message(THERMAL_COMMAND_TOPIC, payload)
    except Exception as exc:  # pragma: no cover - MQTT failure path
        logger.exception("Errore pubblicazione toggle unità esterna")
        raise HTTPException(status_code=500, detail="MQTT publish failed") from exc


def _send_webpush_notification(title: str, body: str, data: dict | None = None) -> None:
    if not WEBPUSH_ENABLED:
        return
    if not WEBPUSH_VAPID_PUBLIC or not WEBPUSH_VAPID_PRIVATE:
        logger.warning("WebPush non configurato (mancano chiavi VAPID)")
        return
    subscriptions = notif.list_subscriptions()
    if not subscriptions:
        return
    payload = json.dumps(
        {
            "title": title,
            "body": body,
            "data": data or {},
        }
    )
    for sub in subscriptions:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=WEBPUSH_VAPID_PRIVATE,
                vapid_claims={"sub": WEBPUSH_VAPID_SUBJECT},
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                endpoint = sub.get("endpoint")
                if endpoint:
                    notif.remove_subscription(endpoint)
            else:
                logger.exception("WebPush failed")


def _send_telegram_text(text: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        ).raise_for_status()
        logger.warning("Telegram notifica inviata: %s", text)
    except Exception as exc:
        logger.warning("Telegram invio fallito: %s", exc)


def _send_telegram_photo(
    snapshot_url: str,
    caption: str,
    boxes: Optional[List[Dict[str, Any]]] = None,
    zoom_first_box: bool = False,
) -> None:
    """Fetch a camera snapshot and send it to Telegram as a photo.

    If `boxes` is provided, draw YOLO bounding boxes on the snapshot before sending.
    If `zoom_first_box` is True, crop around the first bbox before drawing/sending.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    auth = _camera_http_auth()
    img_bytes: bytes | None = None

    # Alcune cam chiudono la connessione sporadicamente (RemoteDisconnected):
    # ritenta 3 volte prima del fallback a solo testo.
    fetch_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(
                snapshot_url,
                auth=auth,
                timeout=(3, 7),
                headers={"Connection": "close"},
            )
            if resp.status_code == 200 and resp.content:
                img_bytes = resp.content
                break
            fetch_err = Exception(f"HTTP {resp.status_code}")
        except Exception as exc:
            fetch_err = exc
        if attempt < 3:
            time.sleep(0.35)

    if img_bytes is None and fetch_err is not None:
        logger.warning(
            "Telegram: errore fetch snapshot %s (3 tentativi): %s",
            snapshot_url,
            fetch_err,
        )

    # Optional overlay with bounding boxes (+ optional zoom crop)
    if img_bytes and boxes:
        try:
            with Image.open(io.BytesIO(img_bytes)) as im:
                im = im.convert("RGB")

                crop_x = 0.0
                crop_y = 0.0
                if zoom_first_box:
                    first_bbox = None
                    for det in boxes:
                        bbox = det.get("bbox") if isinstance(det, dict) else None
                        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
                            first_bbox = bbox
                            break
                    if first_bbox is not None:
                        try:
                            x1, y1, x2, y2 = [float(v) for v in first_bbox[:4]]
                            bw = max(1.0, x2 - x1)
                            bh = max(1.0, y2 - y1)
                            cx = (x1 + x2) / 2.0
                            cy = (y1 + y2) / 2.0

                            zoom_scale = 2.2
                            cw = max(220.0, bw * zoom_scale)
                            ch = max(160.0, bh * zoom_scale)

                            left = max(0.0, cx - cw / 2.0)
                            top = max(0.0, cy - ch / 2.0)
                            right = min(float(im.width), left + cw)
                            bottom = min(float(im.height), top + ch)

                            # re-clamp if near border
                            left = max(0.0, right - cw)
                            top = max(0.0, bottom - ch)

                            crop_x, crop_y = left, top
                            im = im.crop((int(left), int(top), int(right), int(bottom)))
                        except Exception as exc:
                            logger.warning("Telegram: zoom bbox fallito: %s", exc)

                draw = ImageDraw.Draw(im)
                for det in boxes:
                    bbox = det.get("bbox") if isinstance(det, dict) else None
                    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                        continue
                    try:
                        x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
                    except Exception:
                        continue

                    x1 -= crop_x
                    x2 -= crop_x
                    y1 -= crop_y
                    y2 -= crop_y

                    label = str(det.get("class") or det.get("label") or "obj")
                    conf = det.get("confidence")
                    if isinstance(conf, (float, int)):
                        txt = f"{label} {float(conf):.2f}"
                    else:
                        txt = label
                    draw.rectangle([x1, y1, x2, y2], outline=(255, 32, 32), width=3)
                    ty = y1 - 16 if y1 > 20 else y1 + 4
                    draw.text((x1 + 2, ty), txt, fill=(255, 32, 32))

                out = io.BytesIO()
                im.save(out, format="JPEG", quality=90)
                img_bytes = out.getvalue()
        except Exception as exc:
            logger.warning("Telegram: overlay bbox fallito: %s", exc)

    try:
        if img_bytes:
            url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
            requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"photo": ("snapshot.jpg", img_bytes, "image/jpeg")},
                timeout=15,
            ).raise_for_status()
        else:
            url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": caption},
                timeout=10,
            ).raise_for_status()
        logger.warning("Telegram notifica inviata: %s", caption)
    except Exception as exc:
        logger.warning("Telegram invio fallito: %s", exc)


def _energy_line_b_guard_loop() -> None:
    if not ENERGY_LINE_B_DEVICE_ID:
        logger.warning("Energy guard: device id mancante, task disattivato")
        return
    units = [u for u in _parse_int_list(ENERGY_LINE_B_UNITS_RAW) if u > 0]
    if not units:
        logger.warning("Energy guard: nessuna unità esterna configurata, task disattivato")
        return
    threshold = max(0, int(ENERGY_LINE_B_THRESHOLD_W))
    min_duration = max(0, int(ENERGY_LINE_B_MIN_DURATION_S))
    cooldown = max(0, int(ENERGY_LINE_B_COOLDOWN_S))
    reset_after = max(0, int(ENERGY_LINE_B_RESET_S))
    poll = max(1, int(ENERGY_LINE_B_POLL_S))

    above_since: float | None = None
    below_since: float | None = None
    tripped = False
    last_action = 0.0

    logger.warning(
        "Energy guard attivo: device=%s key=%s threshold=%sW units=%s autorestore=%s",
        ENERGY_LINE_B_DEVICE_ID,
        ENERGY_LINE_B_POWER_KEY,
        threshold,
        units,
        ENERGY_LINE_B_AUTORESTORE,
    )

    while not energy_guard_stop.is_set():
        snapshot = device_store.snapshot()
        entry = next((item for item in snapshot if item.get("id") == ENERGY_LINE_B_DEVICE_ID), None)
        payload = entry.get("data") if isinstance(entry, dict) else None
        if not isinstance(payload, dict):
            energy_guard_stop.wait(poll)
            continue

        raw_value = payload.get(ENERGY_LINE_B_POWER_KEY)
        try:
            power = float(raw_value)
        except (TypeError, ValueError):
            energy_guard_stop.wait(poll)
            continue

        if ENERGY_LINE_B_USE_ABS:
            power = abs(power)

        now = time.time()
        if power > threshold:
            above_since = above_since or now
            below_since = None
            if (
                not tripped
                and (now - above_since) >= min_duration
                and (now - last_action) >= cooldown
            ):
                for unit_id in units:
                    # stessa logica operativa della UI: comando per singola unità, con breve attesa
                    # e un retry se il bus è occupato o la prima scrittura non attecchisce.
                    done = False
                    for attempt in (1, 2):
                        try:
                            _set_unit_enable(unit_id, False)
                            done = True
                            break
                        except Exception:
                            logger.exception(
                                "Energy guard: errore spegnimento unità %s (tentativo %s)",
                                unit_id,
                                attempt,
                            )
                            time.sleep(0.8)
                    if not done:
                        logger.error("Energy guard: unità %s non spenta dopo retry", unit_id)
                    time.sleep(0.8)
                tripped = True
                last_action = now
                logger.warning(
                    "Energy guard: assorbimento %.1fW su Linea B, spegnimento unità %s",
                    power,
                    units,
                )
                _send_webpush_notification(
                    "Linea B in assorbimento",
                    f"Assorbimento {power:.0f} W: spente le unità esterne {', '.join(str(u) for u in units)}.",
                    {"kind": "energy_guard", "state": "off"},
                )
                _send_telegram_text(
                    f"⚡ Linea B in assorbimento: {power:.0f} W\n"
                    f"Spente unità esterne: {', '.join(str(u) for u in units)}"
                )
        else:
            above_since = None
            if tripped:
                below_since = below_since or now
                if (now - below_since) >= reset_after:
                    if ENERGY_LINE_B_AUTORESTORE and (now - last_action) >= cooldown:
                        for unit_id in units:
                            done = False
                            for attempt in (1, 2):
                                try:
                                    _set_unit_enable(unit_id, True)
                                    done = True
                                    break
                                except Exception:
                                    logger.exception(
                                        "Energy guard: errore riaccensione unità %s (tentativo %s)",
                                        unit_id,
                                        attempt,
                                    )
                                    time.sleep(0.8)
                            if not done:
                                logger.error("Energy guard: unità %s non riaccesa dopo retry", unit_id)
                            time.sleep(0.8)

                        # safety pass: alcuni controller possono perdere un ON durante transizioni bus.
                        # ripete una seconda passata breve per riallineare tutte le unità.
                        time.sleep(8)
                        for unit_id in units:
                            try:
                                _set_unit_enable(unit_id, True)
                            except Exception:
                                logger.exception("Energy guard: safety-pass riaccensione fallita unità %s", unit_id)
                            time.sleep(0.6)

                        last_action = now
                        logger.warning(
                            "Energy guard: Linea B tornata a 0, riaccensione unità %s",
                            units,
                        )
                        _send_webpush_notification(
                            "Linea B a 0",
                            f"Linea B stabile a 0: riaccese le unità esterne {', '.join(str(u) for u in units)}.",
                            {"kind": "energy_guard", "state": "on"},
                        )
                        _send_telegram_text(
                            f"✅ Linea B tornata a 0\n"
                            f"Riaccese unità esterne: {', '.join(str(u) for u in units)}"
                        )
                    tripped = False
                    below_since = None

        energy_guard_stop.wait(poll)


def _resolve_preset_targets(preset: str) -> tuple[float, float]:
    entry = THERMAL_PRESET_TARGETS.get(preset)
    if entry is None:
        raise HTTPException(status_code=400, detail="Preset non supportato")
    if isinstance(entry, dict):
        heat_candidate = entry.get("heat", entry.get("default"))
        cool_candidate = entry.get("cool", entry.get("default"))
    else:
        heat_candidate = cool_candidate = entry
    if heat_candidate is None or cool_candidate is None:
        raise HTTPException(
            status_code=500,
            detail=f"Preset {preset} non configurato correttamente",
        )
    heat_value = _try_float(heat_candidate)
    cool_value = _try_float(cool_candidate)
    if heat_value is None or cool_value is None:
        raise HTTPException(
            status_code=500,
            detail=f"Preset {preset} contiene valori non numerici",
        )
    return (
        _normalize_temperature_c(heat_value),
        _normalize_temperature_c(cool_value),
    )


def _try_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isfinite(number):
        return number
    return None


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    must_change: bool = False


class MeResponse(BaseModel):
    username: str
    role: str
    exp: int
    roles: list[str] | None = None


class Lettura(BaseModel):
    conducibilita: float
    timestamp: int


class WifiInfoResponse(BaseModel):
    ssid: str = Field(description="Wi-Fi network name (SSID)")
    encryption: str = Field(default="nopass", description="Security type: nopass/WEP/WPA/WPA2/WPA3")
    hidden: bool = Field(default=False, description="Whether the SSID is hidden")
    portal_url: Optional[str] = Field(default=None, description="Optional captive portal URL to open after connecting")


class HotspotLoginRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    mode: Literal["staff", "guest"]
    username: Optional[str] = None
    password: Optional[str] = None
    remember_device: bool = Field(default=True, alias="rememberDevice")
    guest_token: Optional[str] = Field(default=None, alias="guestToken")
    mac: str
    ip: Optional[str] = None
    login_url: Optional[str] = Field(default=None, alias="loginUrl")
    login_direct_url: Optional[str] = Field(default=None, alias="loginDirectUrl")
    original_url: Optional[str] = Field(default=None, alias="originalUrl")
    chap_id: Optional[str] = Field(default=None, alias="chapId")
    chap_challenge: Optional[str] = Field(default=None, alias="chapChallenge")


class HotspotLoginResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    mode: Literal["staff", "guest"]
    redirect_url: Optional[str] = Field(default=None, alias="redirectUrl")
    message: Optional[str] = None
    role: Optional[str] = None


class MailAccountOut(BaseModel):
    address: str
    sent_folder: str = "Sent"


class MailMessageOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    uid: int
    subject: str = ""
    from_: str = Field(default="", alias="from")
    to: str = ""
    date: str = ""
    size: Optional[int] = None
    seen: bool = False


class MailAttachmentOut(BaseModel):
    id: str
    filename: Optional[str] = None
    content_type: Optional[str] = None


class MailMessageDetailOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    uid: int
    subject: str = ""
    from_: str = Field(default="", alias="from")
    to: str = ""
    date: str = ""
    text: Optional[str] = None
    html: Optional[str] = None
    attachments: list[MailAttachmentOut] = []


class MailSendResponse(BaseModel):
    status: str
    sent: bool
    saved: bool
    warning: Optional[str] = None


class ThermalUnitCommand(BaseModel):
    enabled: bool


class ThermalUnitModeCommand(BaseModel):
    mode: Literal["heat", "cool"] | None = None
    season: Literal["heat", "cool"] | None = None
    value: Literal["heat", "cool"] | None = None


class ThermalModeCommand(BaseModel):
    mode: Literal["heat", "cool"]


class ThermalGeneralCommand(BaseModel):
    enabled: bool


class ThermalZonePresetCommand(BaseModel):
    preset: Literal["comfort", "economy", "riposo", "sleep", "confort"]
    mode: Literal["heat", "cool"] | None = None


class ThermalZoneSetpointCommand(BaseModel):
    temperature_c: float | None = Field(
        default=None,
        validation_alias=AliasChoices("temperatureC", "temperature_c"),
    )
    temperature: float | None = Field(
        default=None,
        validation_alias=AliasChoices("temperature", "temperatureSetpoint"),
    )
    temperature_raw: int | None = Field(
        default=None,
        validation_alias=AliasChoices("temperatureRaw", "temperature_raw"),
    )
    temperature_heat_c: float | None = Field(
        default=None,
        validation_alias=AliasChoices("temperatureHeatC", "temperature_heat_c"),
    )
    temperature_cool_c: float | None = Field(
        default=None,
        validation_alias=AliasChoices("temperatureCoolC", "temperature_cool_c"),
    )
    temperature_heat_raw: int | None = Field(
        default=None,
        validation_alias=AliasChoices("temperatureHeatRaw", "temperature_heat_raw"),
    )
    temperature_cool_raw: int | None = Field(
        default=None,
        validation_alias=AliasChoices("temperatureCoolRaw", "temperature_cool_raw"),
    )
    @model_validator(mode="after")
    def _ensure_any_value(self):
        if (
            self.temperature_raw is None
            and self.temperature_c is None
            and self.temperature is None
            and self.temperature_heat_c is None
            and self.temperature_cool_c is None
            and self.temperature_heat_raw is None
            and self.temperature_cool_raw is None
        ):
            raise ValueError("Specify temperatura in °C o valore raw Modbus.")
        return self


class ThermalZoneFanModeCommand(BaseModel):
    mode: Literal["local", "off", "auto", "speed1", "speed2", "speed3", "aux"]


class ThermalZoneForceCommand(BaseModel):
    enabled: bool


class ThermalMachineStateCommand(BaseModel):
    enabled: bool


class PanelConfig(BaseModel):
    fan_v1_pct: int | None = None
    fan_v2_pct: int | None = None
    fan_v3_pct: int | None = None
    flaps_pct: int | None = None
    temp_offset_c: float | None = None
    unit: Literal["C", "F"] | None = None
    bms_forced_mode_display: bool | None = None
    bms_setpoint_view: Literal["offset", "actual"] | None = None
    probe_mode: str | None = None
    alarm_code: str | None = None
    addressing: int | None = None


class PanelConfigUpdate(PanelConfig):
    pass


# --- JWT helpers ---
http_bearer = HTTPBearer(auto_error=False)


def create_token(username: str, role: str) -> str:
    now = int(time.time())
    raw = role or ""
    roles_list = [part.strip() for part in raw.split(",") if part.strip()]
    if roles_list:
        primary_role = roles_list[0]
    else:
        primary_role = role
        roles_list = [role] if role else []
    payload: dict[str, Any] = {
        "sub": username,
        "role": primary_role,
        "iat": now,
        "exp": now + TOKEN_EXP_SECONDS,
    }
    if roles_list:
        payload["roles"] = roles_list
    return jwt.encode(payload, AUTH_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, AUTH_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


def require_role(allowed_roles: list[str]):
    def _dep(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(http_bearer),
    ):
        token: str | None = None
        if credentials and credentials.credentials:
            token = credentials.credentials
        if not token:
            token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authorization required')
        data = decode_token(token)
        role = data.get('role')
        roles = data.get('roles')
        effective_roles: list[str] = []
        if isinstance(roles, list):
            effective_roles = [str(r) for r in roles if isinstance(r, str)]
        if not effective_roles and isinstance(role, str):
            effective_roles = [role]
        if not any(r in allowed_roles for r in effective_roles):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Insufficient role')
        return data

    return _dep


def _sanitize_text(value: Optional[str], max_len: int = 200) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _sanitize_phone(value: Optional[str], max_len: int = 32) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned[:max_len]


def _sanitize_email(value: Optional[str], max_len: int = 120) -> Optional[str]:
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned or "@" not in cleaned:
        return None
    return cleaned[:max_len]

def _normalize_wifi_security(value: Optional[str]) -> str:
    if not value:
        return "nopass"
    raw = value.strip().lower()
    if raw in {"", "open", "none", "nopass"}:
        return "nopass"
    if raw in {"wep"}:
        return "WEP"
    if raw in {"wpa"}:
        return "WPA"
    if raw in {"wpa2"}:
        return "WPA2"
    if raw in {"wpa3"}:
        return "WPA3"
    return value.strip().upper()

WIFI_SECURITY = _normalize_wifi_security(_WIFI_SECURITY_RAW)

def _escape_wifi_value(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace(":", r"\:")
    )


def _load_wifi_info() -> WifiInfoResponse:
    if not WIFI_SSID:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wi-Fi network not configured")
    return WifiInfoResponse(
        ssid=WIFI_SSID,
        encryption=WIFI_SECURITY,
        hidden=bool(WIFI_HIDDEN),
        portal_url=WIFI_PORTAL_URL or None,
    )


def _build_wifi_payload(info: WifiInfoResponse) -> str:
    security = info.encryption.lower()
    wifi_type = "nopass" if security == "nopass" else info.encryption.upper()
    parts = [
        f"WIFI:T:{wifi_type};",
        f"S:{_escape_wifi_value(info.ssid)};",
    ]
    if wifi_type != "nopass":
        password = WIFI_PASSWORD or ""
        parts.append(f"P:{_escape_wifi_value(password)};")
    if info.hidden:
        parts.append("H:true;")
    return "".join(parts) + ";"


@lru_cache(maxsize=1)
def _wifi_qr_png_bytes() -> bytes:
    info = _load_wifi_info()
    payload = _build_wifi_payload(info)
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue()


def _select_redirect_target(body: HotspotLoginRequest) -> str:
    for candidate in (body.original_url, body.login_direct_url, body.login_url):
        if candidate:
            return candidate
    return "http://1.1.1.1/"


# --- Auth endpoints ---
@app.post("/api/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, request: Request, response: Response):
    ip = _client_ip(request)
    # Block if already banned
    banned, until_ts, _reason = adb.is_ip_banned(ip)
    if banned:
        raise HTTPException(status_code=429, detail="Too many attempts")

    # DB-based users
    u = adb.verify_password(body.username, body.password)
    if not u:
        # record failure + autoban
        banned_now, _until = adb.record_failed_login(
            ip=ip,
            username=body.username,
            max_fails=AUTH_MAX_FAILS,
            window_seconds=AUTH_FAIL_WINDOW_SECONDS,
            ban_seconds=AUTH_BAN_SECONDS,
        )
        if banned_now:
            raise HTTPException(status_code=429, detail="Too many attempts")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    # success: clear failures for this IP
    adb.clear_failures(ip)

    role = u["role"]
    must_change = bool(u.get("must_change"))
    token = create_token(u["username"], role)

    # Cookie-based session for SSO (Nginx auth_request)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="Lax",
        max_age=TOKEN_EXP_SECONDS,
        path="/",
    )
    csrf = secrets.token_urlsafe(32)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite="Lax",
        max_age=TOKEN_EXP_SECONDS,
        path="/",
    )
    return TokenResponse(access_token=token, role=role, must_change=must_change)


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/auth/change-password")
def change_password(body: ChangePasswordIn, user=Depends(require_role(["superad", "admin", "reception", "maintenance", "cleaning"]))):
    username = user.get("sub")
    if not body.new_password:
        raise HTTPException(status_code=400, detail="New password is required")
    ok = adb.change_password(username, body.old_password, body.new_password)
    if not ok:
        raise HTTPException(status_code=401, detail="Invalid current password")
    return {"status": "ok"}


@app.get("/api/auth/me", response_model=MeResponse)
def me(user=Depends(require_role(["superad", "admin", "reception", "maintenance", "cleaning"]))):
    role = user.get("role")
    roles = user.get("roles")
    roles_list: list[str] = []
    if isinstance(roles, list):
        roles_list = [str(r) for r in roles if isinstance(r, str)]
    elif isinstance(role, str):
        roles_list = [role]
    return MeResponse(username=user.get("sub"), role=role, exp=user.get("exp"), roles=roles_list)



@app.post('/api/auth/logout')
def logout(response: Response):
    # clear cookies
    response.delete_cookie(SESSION_COOKIE_NAME, path='/')
    response.delete_cookie(CSRF_COOKIE_NAME, path='/')
    return {'status': 'ok'}


@app.get('/api/auth/nginx/verify')
def nginx_verify(role: str = "superad", user=Depends(require_role(ALL_ROLES))):
    """
    Small helper endpoint used by Nginx `auth_request`.

    - default (`role=superad`): only superad can pass (for very sensitive UIs)
    - `role=surveillance` (or `role=admin_or_superad`): allow both superad and admin
    """
    raw_role = user.get("role")
    roles = user.get("roles")
    effective_roles: list[str] = []
    if isinstance(roles, list):
        effective_roles = [str(r) for r in roles if isinstance(r, str)]
    if not effective_roles and isinstance(raw_role, str):
        effective_roles = [raw_role]

    if role == "superad":
        allowed = ["superad"]
    elif role in {"surveillance", "admin_or_superad", "admin"}:
        allowed = ["superad", "admin"]
    else:
        # Safe default: treat as superadmin-only
        allowed = ["superad"]

    if not any(r in allowed for r in effective_roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient role for this resource",
        )

    return {"ok": True, "role": raw_role, "sub": user.get("sub")}


# --- Public/health ---
@app.get("/api/status")
def read_status():
    return {"status": "ok", "message": "Backend attivo ✅"}


@app.get("/api/public/wifi", response_model=WifiInfoResponse)
def read_wifi_info():
    return _load_wifi_info()


@app.get("/api/public/wifi/qr")
def read_wifi_qr():
    _load_wifi_info()  # ensures 404 if not configured
    payload = _wifi_qr_png_bytes()
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="image/png",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.post("/api/public/hotspot/login", response_model=HotspotLoginResponse)
def hotspot_login(body: HotspotLoginRequest):
    try:
        mac_norm = normalise_mac(body.mac)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Indirizzo MAC non valido")

    redirect_target = _select_redirect_target(body)

    try:
        if body.mode == "staff":
            if not body.username or not body.password:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Credenziali richieste")
            record = adb.verify_password(body.username, body.password)
            if not record:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenziali non valide")
            raw_role = record.get("role") or ""
            roles = [part.strip() for part in str(raw_role).split(",") if part.strip()]
            if not any(r in STAFF_WIFI_ROLES for r in roles):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Ruolo non autorizzato all'accesso Wi-Fi")
            comment = f"staff:{body.username}"
            if body.ip:
                comment = f"{comment} ip:{body.ip}"
            if body.remember_device:
                hotspot_manager.register_staff_device(body.username, mac_norm)
            hotspot_manager.ensure_bypass(mac_norm, comment=comment)
            hotspot_logger.info("Hotspot staff login for %s (%s) - roles=%s", body.username, mac_norm, ",".join(roles))
            return HotspotLoginResponse(
                mode="staff",
                redirect_url=redirect_target,
                message="Accesso autorizzato. Puoi tornare alla pagina precedente.",
                role=roles[0] if roles else None,
            )

        if body.mode == "guest":
            token = body.guest_token
            if not token:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="QR non valido o mancante")
            booking = bk.get_booking_by_token(token)
            if not booking:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Prenotazione non trovata o link scaduto")
            check_in = booking.get("check_in")
            check_out = booking.get("check_out")
            if not check_in or not check_out:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Prenotazione senza date valide")
            now = int(time.time())
            grace = hotspot_manager.guest_grace_seconds
            if now < check_in - grace or now > check_out + grace:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="QR utilizzabile solo durante il periodo della prenotazione.",
                )
            hotspot_manager.register_guest_device(booking["id"], mac_norm)
            comment = f"guest:booking={booking['id']}"
            if body.ip:
                comment = f"{comment} ip:{body.ip}"
            hotspot_manager.ensure_bypass(mac_norm, comment=comment)
            hotspot_logger.info(
                "Hotspot guest login booking=%s mac=%s devices<=%s",
                booking["id"],
                mac_norm,
                HOTSPOT_GUEST_MAX_DEVICES,
            )
            return HotspotLoginResponse(
                mode="guest",
                redirect_url=redirect_target,
                message="Connessione autorizzata. Puoi continuare la navigazione.",
            )

    except GuestDeviceLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Limite di {exc.max_devices} dispositivi già connessi con questo QR.",
        ) from exc
    except HotspotNotConfigured as exc:
        hotspot_logger.warning("Richiesta hotspot senza configurazione attiva")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Hotspot non configurato") from exc
    except HotspotUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Controller hotspot non raggiungibile, riprova tra qualche minuto.",
        ) from exc

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Modalità hotspot non supportata")


# --- Variabile per memorizzare l'ultima lettura ---
ultima_lettura = {"conducibilita": None, "timestamp": None}


# --- Ingest da dispositivo (role: dispositivi - via API key) ---
@app.post("/api/lettura")
async def ricevi_lettura(data: Lettura, x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    if not DEVICE_API_KEY:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="DEVICE_API_KEY not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, DEVICE_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    global ultima_lettura
    print(f"📥 Ricevuta conducibilità: {data.conducibilita:.0f} µS/cm alle {data.timestamp}")
    ultima_lettura = {"conducibilita": data.conducibilita, "timestamp": data.timestamp}
    return {"status": "ok"}


# --- Accesso ultima lettura (ruoli: admin, user) ---
@app.get("/api/ultima-lettura")
def get_ultima_lettura(user=Depends(require_role(["superad", "admin", "maintenance"]))):
    return ultima_lettura


# --- Event registry APIs ---
class FancoilWarmupEvent(BaseModel):
    fc_id: int
    room: str | None = None
    area: str | None = None
    address: int | None = None
    direction: str
    start_ts: int
    end_ts: int
    duration_seconds: float
    start_temp_c: float
    end_temp_c: float
    target_setpoint_c: float
    delta_start_c: float
    delta_end_c: float


class EventIn(BaseModel):
    camera: str
    state: str  # 'start' | 'stop'
    timestamp: int  # epoch seconds
    source: Optional[str] = "nvr"


class YoloDetectionItem(BaseModel):
    """Single detection within a YOLO frame (object class + label + confidence)."""
    class_: str = Field(default="", alias="class")
    label: str = ""
    confidence: float = 0.0
    bbox: Optional[list[float]] = None
    track_id: Optional[int] = None

    model_config = ConfigDict(populate_by_name=True)


class YoloUpdateIn(BaseModel):
    camera: str
    ts: float
    raw: Optional[str] = None
    data: Optional[Any] = None
    text: Optional[str] = None
    detections: list[YoloDetectionItem] = []

    class Config:
        arbitrary_types_allowed = True


class VehicleRecordingBulkDeleteIn(BaseModel):
    paths: list[str] = Field(default_factory=list)


@app.post("/api/events/webhook")
def nvr_event_webhook(item: EventIn, x_api_key: str | None = Header(default=None, alias="X-Webhook-Key")):
    key = os.environ.get("NVR_WEBHOOK_KEY")
    if not key:
        raise HTTPException(status_code=500, detail="NVR_WEBHOOK_KEY not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, key):
        raise HTTPException(status_code=401, detail="Invalid webhook key")
    eid = ev.create_or_update_event(item.camera, item.state, int(item.timestamp), source=item.source or "nvr")
    return {"status": "ok", "event_id": eid}


@app.get("/api/events")
def get_events(
    camera: Optional[str] = Query(default=None),
    since: Optional[int] = Query(default=None),
    until: Optional[int] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    user=Depends(require_role(["superad", "admin"]))
):
    return ev.list_events(camera=camera, since=since, until=until, limit=limit)


@app.post("/api/yolo/update")
def ingest_yolo(item: YoloUpdateIn, x_yolo_key: str | None = Header(default=None, alias="X-Yolo-Key")):
    if YOLO_WEBHOOK_KEY:
        if not x_yolo_key or not hmac.compare_digest(x_yolo_key, YOLO_WEBHOOK_KEY):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid YOLO key")
    payload = item.model_dump()
    yolo_store.update(item.camera, payload)

    # Run rules engine if detections are present
    if item.detections:
        cameras = _load_yolo_camera_config()
        camera_cfg = cameras.get(item.camera, {})
        det_dicts = [
            {
                "class": d.class_,
                "label": d.label,
                "confidence": d.confidence,
                "bbox": d.bbox,
                "track_id": d.track_id,
            }
            for d in item.detections
        ]
        rules_engine.process(
            camera_id=item.camera,
            camera_cfg=camera_cfg,
            detections=det_dicts,
            send_mqtt=_publish_mqtt_message,
            send_push=lambda title, body: _send_webpush_notification(title, body),
            store_alert=alert_store.add,
            send_telegram=lambda snapshot_url, caption, boxes=None, zoom_first_box=False: _send_telegram_photo(snapshot_url, caption, boxes, zoom_first_box),
        )

    return {"status": "ok"}


@app.get("/api/yolo/latest")
def latest_yolo(
    camera: Optional[str] = Query(default=None),
    user=Depends(require_role(["superad", "admin"]))
):
    data = yolo_store.latest(camera=camera)
    return {"cameras": data}


@app.get("/api/yolo/alerts")
def list_yolo_alerts(
    limit: int = Query(default=50, ge=1, le=200),
    user=Depends(require_role(["superad", "admin"]))
):
    """Restituisce gli ultimi allarmi triggerati dalle regole YOLO (veicolo, piscina, ecc.)."""
    return {"alerts": alert_store.list(limit=limit)}


@app.get("/api/yolo/vehicle-recordings")
def list_yolo_vehicle_recordings(
    camera: Optional[str] = Query(default=None),
    days: int = Query(default=14, ge=1, le=365),
    limit: int = Query(default=200, ge=1, le=2000),
    user=Depends(require_role(["superad", "admin"])),
):
    base_dir = YOLO_VEHICLE_RECORDINGS_DIR
    if not base_dir.exists() or not base_dir.is_dir():
        return {"recordings": []}

    camera = (camera or "").strip()
    now_ts = time.time()
    min_mtime = now_ts - (days * 86400)

    roots: list[Path]
    if camera:
        roots = [base_dir / camera]
    else:
        roots = [d for d in base_dir.iterdir() if d.is_dir()]

    out: list[dict] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for file_path in root.rglob("*"):
            if not file_path.is_file():
                continue
            suffix = file_path.suffix.lower()
            if suffix not in YOLO_VEHICLE_RECORDING_EXTS:
                continue
            # If both MKV and MP4 exist for same clip, prefer MP4 in listings.
            if suffix == ".mkv" and file_path.with_suffix('.mp4').exists():
                continue
            try:
                st = file_path.stat()
            except OSError:
                continue
            if st.st_mtime < min_mtime:
                continue
            try:
                rel = file_path.relative_to(base_dir).as_posix()
            except ValueError:
                continue
            cam = rel.split("/", 1)[0] if "/" in rel else (camera or "unknown")
            out.append({
                "camera": cam,
                "path": rel,
                "filename": file_path.name,
                "size_bytes": int(st.st_size),
                "mtime_ts": float(st.st_mtime),
            })

    out.sort(key=lambda x: x.get("mtime_ts", 0), reverse=True)
    if limit:
        out = out[:limit]
    return {"recordings": out}


@app.get("/api/yolo/vehicle-recordings/file")
def get_yolo_vehicle_recording_file(
    path: str = Query(..., min_length=1),
    download: bool = Query(default=False),
    user=Depends(require_role(["superad", "admin"])),
):
    base_dir = YOLO_VEHICLE_RECORDINGS_DIR
    if not base_dir.exists() or not base_dir.is_dir():
        raise HTTPException(status_code=404, detail="Archivio registrazioni non disponibile")

    raw = (path or "").strip().replace("\\", "/")
    while raw.startswith("/"):
        raw = raw[1:]
    if not raw:
        raise HTTPException(status_code=400, detail="Path non valido")

    file_path = (base_dir / raw).resolve()
    base_resolved = base_dir.resolve()
    try:
        file_path.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path non valido")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File non trovato")
    if file_path.suffix.lower() not in YOLO_VEHICLE_RECORDING_EXTS:
        raise HTTPException(status_code=400, detail="Formato file non supportato")

    import mimetypes

    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    }
    if download:
        return FileResponse(str(file_path), media_type=media_type, filename=file_path.name, headers=headers)
    return FileResponse(str(file_path), media_type=media_type, headers=headers)


@app.delete("/api/yolo/vehicle-recordings/file")
def delete_yolo_vehicle_recording_file(
    path: str = Query(..., min_length=1),
    user=Depends(require_role(["superad", "admin"])),
):
    base_dir = YOLO_VEHICLE_RECORDINGS_DIR
    if not base_dir.exists() or not base_dir.is_dir():
        raise HTTPException(status_code=404, detail="Archivio registrazioni non disponibile")

    raw = (path or "").strip().replace("\\", "/")
    while raw.startswith("/"):
        raw = raw[1:]
    if not raw:
        raise HTTPException(status_code=400, detail="Path non valido")

    file_path = (base_dir / raw).resolve()
    base_resolved = base_dir.resolve()
    try:
        file_path.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path non valido")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File non trovato")
    if file_path.suffix.lower() not in YOLO_VEHICLE_RECORDING_EXTS:
        raise HTTPException(status_code=400, detail="Formato file non supportato")

    try:
        file_path.unlink()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Impossibile eliminare il file: {exc}") from exc

    # pulizia cartelle vuote fino alla root recordings
    parent = file_path.parent
    while parent != base_resolved:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent

    logger.info("Recording eliminata: %s", raw)
    return {"status": "ok", "deleted": raw}


@app.post("/api/yolo/vehicle-recordings/delete")
def delete_yolo_vehicle_recordings_bulk(
    payload: VehicleRecordingBulkDeleteIn,
    user=Depends(require_role(["superad", "admin"])),
):
    base_dir = YOLO_VEHICLE_RECORDINGS_DIR
    if not base_dir.exists() or not base_dir.is_dir():
        raise HTTPException(status_code=404, detail="Archivio registrazioni non disponibile")

    paths = payload.paths or []
    if not paths:
        raise HTTPException(status_code=400, detail="Nessun path specificato")
    if len(paths) > 500:
        raise HTTPException(status_code=400, detail="Troppi file richiesti (max 500)")

    base_resolved = base_dir.resolve()
    results: list[dict[str, str]] = []
    deleted = 0

    for path in paths:
        raw = str(path or "").strip().replace("\\", "/")
        while raw.startswith("/"):
            raw = raw[1:]
        if not raw:
            results.append({"path": str(path), "status": "error", "detail": "Path non valido"})
            continue

        file_path = (base_dir / raw).resolve()
        try:
            file_path.relative_to(base_resolved)
        except ValueError:
            results.append({"path": raw, "status": "error", "detail": "Path non valido"})
            continue

        if not file_path.exists() or not file_path.is_file():
            results.append({"path": raw, "status": "error", "detail": "File non trovato"})
            continue
        if file_path.suffix.lower() not in YOLO_VEHICLE_RECORDING_EXTS:
            results.append({"path": raw, "status": "error", "detail": "Formato file non supportato"})
            continue

        try:
            file_path.unlink()
        except OSError as exc:
            results.append({"path": raw, "status": "error", "detail": f"Impossibile eliminare il file: {exc}"})
            continue

        parent = file_path.parent
        while parent != base_resolved:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

        logger.info("Recording eliminata (bulk): %s", raw)
        deleted += 1
        results.append({"path": raw, "status": "deleted"})

    failed = len([r for r in results if r.get("status") != "deleted"])
    return {
        "status": "ok",
        "deleted": deleted,
        "failed": failed,
        "results": results,
    }


@app.get("/api/yolo/plates")
def list_authorized_plates(user=Depends(require_role(["superad", "admin"]))):
    """Restituisce la lista delle targhe autorizzate."""
    return {"plates": rules_engine.load_plates()}


@app.post("/api/yolo/plates")
def add_authorized_plate(
    plate: str = Query(..., description="Targa da autorizzare (es. AB123CD)"),
    user=Depends(require_role(["superad", "admin"])),
):
    """Aggiunge una targa alla lista autorizzate."""
    rules_engine.add_plate(plate)
    return {"status": "ok", "plates": rules_engine.load_plates()}


@app.delete("/api/yolo/plates/{plate}")
def remove_authorized_plate(
    plate: str,
    user=Depends(require_role(["superad", "admin"])),
):
    """Rimuove una targa dalla lista autorizzate."""
    removed = rules_engine.remove_plate(plate)
    if not removed:
        raise HTTPException(status_code=404, detail="Targa non trovata")
    return {"status": "ok", "plates": rules_engine.load_plates()}


@app.get("/api/thermal/rooms/monitor", response_model=dict)
def get_fancoil_warmup_events(
    limit: int = Query(default=500, ge=1, le=5000),
    user=Depends(require_role(ALL_ROLES)),
):
    """
    Restituisce gli eventi di warm-up dei fan-coil registrati nel CSV.
    L'output è pensato per essere consumato dal frontend (monitor camere).
    """
    if not FANCOIL_WARMUP_LOG_PATH.exists():
        return {"events": []}

    import csv  # import locale per evitare dipendenza globale

    events: list[FancoilWarmupEvent] = []
    try:
        with FANCOIL_WARMUP_LOG_PATH.open("r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    start = datetime.fromisoformat(row["start_ts"])
                    end = datetime.fromisoformat(row["end_ts"])
                except Exception:
                    # se non è ISO (es. vecchio formato), tenta parse come epoch
                    try:
                        start = datetime.fromtimestamp(float(row["start_ts"]), tz=APP_TIMEZONE)
                        end = datetime.fromtimestamp(float(row["end_ts"]), tz=APP_TIMEZONE)
                    except Exception:
                        continue
                try:
                    fc_id = int(row.get("fc_id") or 0)
                except Exception:
                    continue
                if fc_id <= 0:
                    continue
                try:
                    duration = float(row.get("duration_seconds") or 0.0)
                    start_temp_c = float(row.get("start_temp_c") or 0.0)
                    end_temp_c = float(row.get("end_temp_c") or 0.0)
                    target_setpoint_c = float(row.get("target_setpoint_c") or 0.0)
                    delta_start_c = float(row.get("delta_start_c") or 0.0)
                    delta_end_c = float(row.get("delta_end_c") or 0.0)
                except Exception:
                    continue
                address_raw = row.get("address")
                try:
                    address = int(address_raw) if address_raw not in ("", None) else None
                except Exception:
                    address = None
                events.append(
                    FancoilWarmupEvent(
                        fc_id=fc_id,
                        room=row.get("room") or None,
                        area=row.get("area") or None,
                        address=address,
                        direction=row.get("direction") or "unknown",
                        start_ts=int(start.timestamp()),
                        end_ts=int(end.timestamp()),
                        duration_seconds=duration,
                        start_temp_c=start_temp_c,
                        end_temp_c=end_temp_c,
                        target_setpoint_c=target_setpoint_c,
                        delta_start_c=delta_start_c,
                        delta_end_c=delta_end_c,
                    ),
                )
    except FileNotFoundError:
        return {"events": []}

    # tieni solo gli eventi più recenti
    events.sort(key=lambda e: e.start_ts, reverse=True)
    events = events[:limit]
    events.sort(key=lambda e: e.start_ts)
    return {"events": [e.model_dump() for e in events]}


def _normalize_camera_player(value: Any) -> Optional[Dict[str, Any]]:
    if not value:
        return None
    if isinstance(value, str):
        url = value.strip()
        if not url:
            return None
        return {"url": url, "type": "mjpeg"}
    if isinstance(value, dict):
        url = value.get("url") or value.get("src") or value.get("href")
        if isinstance(url, str):
            url = url.strip()
        if not url:
            return None
        player_type_raw = value.get("type") or value.get("kind") or "mjpeg"
        player_type = player_type_raw.strip().lower() if isinstance(player_type_raw, str) else "mjpeg"
        player: Dict[str, Any] = {"url": url, "type": player_type}
        snapshot_url = value.get("snapshot") or value.get("snapshot_url")
        if isinstance(snapshot_url, str):
            snapshot_url = snapshot_url.strip()
        if snapshot_url:
            player["snapshot_url"] = snapshot_url
        stream_url = value.get("stream_url") or value.get("mjpeg_url") or value.get("stream")
        if isinstance(stream_url, str):
            stream_url = stream_url.strip()
        if stream_url:
            player["stream_url"] = stream_url
        label = value.get("label") or value.get("title")
        if isinstance(label, str):
            clean_label = label.strip()
            if clean_label:
                player["label"] = clean_label
        for key in ("width", "height"):
            raw = value.get(key)
            if raw is None:
                continue
            try:
                player[key] = int(raw)
            except (TypeError, ValueError):
                continue
        refresh = value.get("refresh") or value.get("refresh_seconds")
        if refresh is not None:
            try:
                player["refresh"] = int(refresh)
            except (TypeError, ValueError):
                pass
        return player
    return None


def _discover_nvr_cameras() -> Dict[str, Any]:
    """Interroga il Dahua NVR via Digest API e ritorna le cam attive.

    Ritorna {} se NVR_URL non è configurato o la chiamata fallisce.
    """
    if not NVR_URL:
        return {}
    try:
        import re
        url = f"{NVR_URL}/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice"
        auth = HTTPDigestAuth(NVR_USER, NVR_PASS) if NVR_USER else None
        resp = requests.get(url, auth=auth, timeout=5)
        if resp.status_code != 200:
            logger.warning("NVR discovery HTTP %s", resp.status_code)
            return {}
        entries: Dict[str, Dict[str, str]] = {}
        for line in resp.text.splitlines():
            m = re.match(
                r"table\.RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.(.+)=(.*)$",
                line,
            )
            if not m:
                continue
            idx, key, val = m.group(1), m.group(2), m.group(3).strip()
            entries.setdefault(idx, {})[key] = val
        cameras: Dict[str, Any] = {}
        for idx, fields in sorted(entries.items(), key=lambda x: int(x[0])):
            ip   = fields.get("Address", "")
            name = fields.get("VideoInputs[0].Name", "").strip()
            if not ip or ip == "192.168.0.0" or not name:
                continue
            cameras[name] = {
                "rtsp": f"rtsp://{ip}:554/cam/realmonitor?channel=1&subtype=0",
                "player": {
                    "type": "snapshot",
                    "url": f"http://{ip}/cgi-bin/mjpg/video.cgi?channel=1&subtype=0",
                    "snapshot_url": f"http://{ip}/cgi-bin/snapshot.cgi?channel=1&subtype=0",
                    "refresh": 1,
                },
            }
        logger.info("NVR discovery: %d cam — %s", len(cameras), list(cameras))
        return cameras
    except Exception:
        logger.warning("NVR discovery fallita", exc_info=True)
        return {}


def _load_yolo_camera_config() -> Dict[str, Dict[str, Any]]:
    # Carica cameras.yml (override/rules)
    try:
        with YOLO_CAMERAS_FILE.open("r", encoding="utf-8") as fp:
            raw = yaml.safe_load(fp) or {}
    except FileNotFoundError:
        raw = {}
    except Exception:
        logger.exception("Errore lettura %s", YOLO_CAMERAS_FILE)
        raw = {}
    static = raw.get("cameras") or {}

    # Merge con NVR: base NVR + override da cameras.yml
    nvr = _discover_nvr_cameras()
    merged: Dict[str, Any] = {}
    for name, nvr_cfg in nvr.items():
        if name in static:
            entry = dict(nvr_cfg)
            entry.update({k: v for k, v in static[name].items() if v is not None})
            merged[name] = entry
        else:
            merged[name] = nvr_cfg
    for name, cfg in static.items():
        if name not in merged:
            merged[name] = cfg

    normalized: Dict[str, Dict[str, Any]] = {}
    for name, cfg in merged.items():
        if not isinstance(cfg, dict):
            continue
        rtsp = cfg.get("rtsp")
        if not rtsp:
            continue
        auth_rtsp = _rewrite_rtsp_host(_inject_rtsp_auth(str(rtsp)))
        normalized[name] = {
            "id": name,
            "label": cfg.get("label") or name.replace("_", " ").title(),
            "rtsp": auth_rtsp,
            "classes": cfg.get("classes") or [],
            "player": _normalize_camera_player(
                cfg.get("player") or cfg.get("player_url") or cfg.get("preview_url")
            ),
            "rules": cfg.get("rules") or {},
        }
    return normalized


def _inject_rtsp_auth(rtsp: str) -> str:
    """Inject ONVIF credentials into an RTSP URL when missing."""
    if not ONVIF_USER:
        return rtsp
    try:
        parsed = urllib.parse.urlparse(rtsp)
    except Exception:
        return rtsp
    if parsed.username:
        return rtsp
    host = parsed.hostname or ""
    if not host:
        return rtsp
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    user = urllib.parse.quote(ONVIF_USER, safe="")
    if ONVIF_PASS:
        pwd = urllib.parse.quote(ONVIF_PASS, safe="")
        auth = f"{user}:{pwd}"
    else:
        auth = user
    netloc = f"{auth}@{netloc}"
    parsed = parsed._replace(netloc=netloc)
    return parsed.geturl()


def _rewrite_rtsp_host(rtsp: str) -> str:
    """Rewrite RTSP host/port to a public endpoint when configured."""
    if not RTSP_PUBLIC_HOST:
        return rtsp
    try:
        parsed = urllib.parse.urlparse(rtsp)
    except Exception:
        return rtsp
    host = parsed.hostname or ""
    if not host:
        return rtsp
    userinfo = ""
    if parsed.username:
        user = urllib.parse.quote(parsed.username, safe="")
        if parsed.password:
            pwd = urllib.parse.quote(parsed.password, safe="")
            userinfo = f"{user}:{pwd}@"
        else:
            userinfo = f"{user}@"
    port = RTSP_PUBLIC_PORT or (str(parsed.port) if parsed.port else "")
    netloc = f"{userinfo}{RTSP_PUBLIC_HOST}"
    if port:
        netloc = f"{netloc}:{port}"
    parsed = parsed._replace(netloc=netloc)
    return parsed.geturl()


def _camera_http_auth():
    if not ONVIF_USER:
        return None
    if ONVIF_AUTH == "basic":
        return HTTPBasicAuth(ONVIF_USER, ONVIF_PASS or "")
    return HTTPDigestAuth(ONVIF_USER, ONVIF_PASS or "")


@app.get("/api/yolo/cameras")
def list_yolo_cameras(user=Depends(require_role(["superad", "admin"]))):
    cameras = list(_load_yolo_camera_config().values())
    return {"cameras": cameras}


@app.get("/api/yolo/cameras/{camera_id}/snapshot")
def yolo_camera_snapshot(
    camera_id: str,
    user=Depends(require_role(["superad", "admin"])),
):
    cameras = _load_yolo_camera_config()
    camera = cameras.get(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera non trovata")
    player = camera.get("player") or {}
    if not isinstance(player, dict):
        raise HTTPException(status_code=404, detail="Anteprima non configurata per questa camera")
    url = player.get("snapshot_url") or player.get("url")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=404, detail="Anteprima non configurata per questa camera")
    url = url.strip()
    auth = _camera_http_auth()
    try:
        resp = requests.get(url, auth=auth, timeout=5, stream=True)
    except requests.RequestException as exc:
        logger.warning("Errore snapshot camera %s: %s", camera_id, exc)
        raise HTTPException(status_code=502, detail="Snapshot camera non raggiungibile") from exc
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Snapshot non valido (HTTP {resp.status_code})",
        )
    content_type = resp.headers.get("Content-Type") or "image/jpeg"
    return StreamingResponse(resp.iter_content(chunk_size=65536), media_type=content_type, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"})


@app.get("/api/yolo/cameras/{camera_id}/stream")
def yolo_camera_stream(
    camera_id: str,
    user=Depends(require_role(["superad", "admin"])),
):
    cameras = _load_yolo_camera_config()
    camera = cameras.get(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera non trovata")
    player = camera.get("player") or {}
    if not isinstance(player, dict):
        raise HTTPException(status_code=404, detail="Stream non configurato per questa camera")
    url = player.get("stream_url") or player.get("url")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=404, detail="Stream non configurato per questa camera")
    url = url.strip()
    auth = _camera_http_auth()
    try:
        resp = requests.get(url, auth=auth, timeout=(5, 20), stream=True)
    except requests.RequestException as exc:
        logger.warning("Errore stream camera %s: %s", camera_id, exc)
        raise HTTPException(status_code=502, detail="Stream camera non raggiungibile") from exc
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Stream non valido (HTTP {resp.status_code})",
        )
    content_type = resp.headers.get("Content-Type") or "multipart/x-mixed-replace"

    def _iter_stream():
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return StreamingResponse(_iter_stream(), media_type=content_type, headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache", "Expires": "0"})


@app.get("/api/yolo/cameras/{camera_id}/playlist")
def yolo_camera_playlist(
    camera_id: str,
    user=Depends(require_role(["superad", "admin"]))
):
    cameras = _load_yolo_camera_config()
    camera = cameras.get(camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera non trovata")
    rtsp = _rewrite_rtsp_host(_inject_rtsp_auth(camera["rtsp"]))
    # VLC spesso usa UDP per RTSP: forziamo TCP e un minimo caching.
    playlist = (
        "#EXTM3U\n"
        "#EXTVLCOPT:rtsp-tcp\n"
        "#EXTVLCOPT:network-caching=1000\n"
        f"#EXTINF:-1,{camera['label']}\n"
        f"{rtsp}\n"
    )
    filename = f"{camera_id}.m3u"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return PlainTextResponse(playlist, media_type="audio/x-mpegurl", headers=headers)


@app.get("/api/devices")
def list_devices(user=Depends(require_role(ALL_ROLES))):
    return {"devices": device_store.snapshot()}


class PushSubscriptionIn(BaseModel):
    endpoint: str
    keys: Dict[str, str]
    expirationTime: Optional[int] = None


class PushUnsubscribeIn(BaseModel):
    endpoint: str


@app.get("/api/notifications/vapid")
def get_vapid_public_key(user=Depends(require_role(ALL_ROLES))):
    if not WEBPUSH_ENABLED or not WEBPUSH_VAPID_PUBLIC:
        raise HTTPException(status_code=503, detail="WebPush non configurato")
    return {"publicKey": WEBPUSH_VAPID_PUBLIC}


@app.post("/api/notifications/subscribe")
def subscribe_notifications(body: PushSubscriptionIn, user=Depends(require_role(ALL_ROLES))):
    if not WEBPUSH_ENABLED:
        raise HTTPException(status_code=503, detail="WebPush non configurato")
    if not body.endpoint or not body.keys:
        raise HTTPException(status_code=400, detail="Subscription non valida")
    notif.upsert_subscription(body.model_dump(), user=user.get("sub") if isinstance(user, dict) else None)
    return {"status": "ok"}


@app.post("/api/notifications/unsubscribe")
def unsubscribe_notifications(body: PushUnsubscribeIn, user=Depends(require_role(ALL_ROLES))):
    if not WEBPUSH_ENABLED:
        raise HTTPException(status_code=503, detail="WebPush non configurato")
    if not body.endpoint:
        raise HTTPException(status_code=400, detail="Endpoint non valido")
    notif.remove_subscription(body.endpoint)
    return {"status": "ok"}


@app.post("/api/thermal/units/{unit_id}/state")
def set_thermal_unit_state(
    unit_id: int,
    body: ThermalUnitCommand,
    user=Depends(require_role(THERMAL_CONTROL_ROLES)),
):
    if unit_id < 1 or unit_id > 3:
        raise HTTPException(status_code=400, detail="Unit ID must be between 1 e 3")
    try:
        _set_unit_enable(unit_id, bool(body.enabled))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Errore scrittura stato unità esterna")
        raise HTTPException(status_code=500, detail="Modbus write failed") from exc
    return {"status": "ok"}


@app.post("/api/thermal/units/{unit_id}/mode")
def set_thermal_unit_mode(
    unit_id: int,
    body: ThermalUnitModeCommand,
    user=Depends(require_role(THERMAL_CONTROL_ROLES)),
):
    if unit_id < 1 or unit_id > 3:
        raise HTTPException(status_code=400, detail="Unit ID must be between 1 e 3")

    def _resolve_mode() -> str:
        for candidate in (body.mode, body.season, body.value):
            if candidate:
                normalized = candidate.lower()
                if normalized in ("heat", "cool"):
                    return normalized
        raise HTTPException(status_code=400, detail="Modalità non valida (heat|cool)")

    mode = _resolve_mode()
    season_code = 1 if mode == "cool" else 0
    payload = {
        "type": "season",
        "pdc": unit_id,
        "unit": unit_id,
        "mode": mode,
        "season": mode,
        "season_code": season_code,
        "value": mode,
        "source": "backend",
        "controller": "vmf-e6",
    }
    try:
        _publish_mqtt_message(THERMAL_COMMAND_TOPIC, payload)
    except Exception as exc:
        logger.exception("Errore pubblicazione stagione unità esterna")
        raise HTTPException(status_code=500, detail="MQTT publish failed") from exc
    return {"status": "ok", "unit": unit_id, "mode": mode, "season_code": season_code}


@app.post("/api/thermal/mode")
def set_thermal_mode(
    body: ThermalModeCommand, user=Depends(require_role(THERMAL_CONTROL_ROLES))
):
    mode = body.mode.lower()
    payload = {"type": "season", "mode": mode}
    try:
        _publish_mqtt_message(THERMAL_COMMAND_TOPIC, payload)
    except Exception as exc:
        logger.exception("Errore pubblicazione comando modalità termica")
        raise HTTPException(status_code=500, detail="MQTT publish failed") from exc
    return {"status": "ok", "mode": mode}


@app.post("/api/thermal/general")
def set_thermal_general_state(
    body: ThermalGeneralCommand, user=Depends(require_role(THERMAL_CONTROL_ROLES))
):
    enabled = bool(body.enabled)
    payload = {"type": "general", "enabled": enabled}
    try:
        _publish_mqtt_message(THERMAL_COMMAND_TOPIC, payload)
    except Exception as exc:
        logger.exception("Errore pubblicazione comando generale impianto")
        raise HTTPException(status_code=500, detail="MQTT publish failed") from exc
    return {"status": "ok", "enabled": enabled}




# --- VMF-E4/E4X (HMI) endpoints: intentionally no-op writes ---

class ThermalPanelConfigCommand(BaseModel):
    """Configuration fields exposed by VMF-E4/E4X panels.

    Remote writes are intentionally treated as no-ops in this stack; the source of truth
    for supervision/control is VMF-E6 via Modbus and Node-RED flows.
    """

    fan_v1_pct: int | None = None
    fan_v2_pct: int | None = None
    fan_v3_pct: int | None = None
    flaps: int | None = None
    temp_offset_tenths: int | None = None
    unit: str | None = None  # 'C' or 'F'
    bms_forced_mode_display: bool | None = None
    bms_setpoint_view: str | None = None  # 'offset' or 'actual'
    probe_mode: str | None = None


@app.get("/api/thermal/panels/e4")
def list_e4_panels(user=Depends(require_role(["superad", "admin", "maintenance"]))):
    """Return E4/E4X panels metadata (read-only).

    The mapping is used for UI grouping and troubleshooting.
    """
    panels = []
    for fc_id, panel in sorted(THERMAL_FC_PANEL_MAP.items()):
        panels.append({"fc_id": fc_id, "panel_address": panel})
    return {"status": "ok", "panels": panels}


@app.post("/api/thermal/panels/e4/{panel_address}/config")
def set_e4_panel_config(
    panel_address: int,
    body: ThermalPanelConfigCommand,
    user=Depends(require_role(["superad"]))
):
    """No-op endpoint for VMF-E4/E4X panel configuration.

    Kept to maintain API compatibility and provide a clear, explicit response to callers.
    """
    logger.info(
        "NO-OP E4 panel config request (panel=%s user=%s payload=%s)",
        panel_address,
        user.get("username") if isinstance(user, dict) else user,
        body.model_dump(exclude_none=True),
    )
    return {
        "status": "noop",
        "panel_address": panel_address,
        "reason": "VMF-E4/E4X is treated as HMI/read-only. Apply changes via VMF-E6 control plane.",
    }

@app.get("/api/thermal/config")
def get_thermal_config(user=Depends(require_role(THERMAL_CONTROL_ROLES))):
    def _prepare_preset(entry: Any) -> dict[str, float]:
        if isinstance(entry, dict):
            out: dict[str, float] = {}
            for key, value in entry.items():
                numeric = _try_float(value)
                if numeric is not None:
                    out[key] = numeric
            return out
        numeric = _try_float(entry)
        return {"default": numeric} if numeric is not None else {}

    return {
        "setpoint_min_c": THERMAL_SETPOINT_MIN_C,
        "setpoint_max_c": THERMAL_SETPOINT_MAX_C,
        "presets": {
            name: _prepare_preset(value) for name, value in THERMAL_PRESET_TARGETS.items()
        },
        "register_base": THERMAL_ZONE_REGISTER_BASE,
        "fc_panel_map": THERMAL_FC_PANEL_MAP,
    }


@app.post("/api/thermal/machines/{fc_id}/state")
def set_machine_state(
    fc_id: int,
    body: ThermalMachineStateCommand,
    user=Depends(require_role(THERMAL_CONTROL_ROLES)),
):
    _publish_fancoil_toggle(fc_id, enabled=bool(body.enabled))
    return {"status": "ok", "fc_id": fc_id, "enabled": bool(body.enabled)}


@app.post("/api/thermal/machines/{fc_id}/preset")
def set_machine_preset(
    fc_id: int,
    body: ThermalZonePresetCommand,
    user=Depends(require_role(THERMAL_CONTROL_ROLES)),
):
    def _matches_target(status_payload: dict[str, Any] | None, target_heat_c: float, target_cool_c: float) -> bool:
        if not isinstance(status_payload, dict):
            return False
        heat = _try_float(status_payload.get("setpoint_heat_c"))
        cool = _try_float(status_payload.get("setpoint_cool_c"))
        if heat is None or cool is None:
            return False
        return abs(heat - target_heat_c) <= 0.2 and abs(cool - target_cool_c) <= 0.2

    preset = body.preset.lower()
    heat_c, cool_c = _resolve_preset_targets(preset)

    last_status: dict[str, Any] | None = None
    applied = False
    resend_done = False

    for publish_attempt in range(2):
        _publish_zone_force_setpoint(fc_id, enabled=True)
        _publish_zone_setpoint(
            fc_id,
            heat_c=heat_c,
            cool_c=cool_c,
            preset=preset,
        )

        for _ in range(6):
            time.sleep(0.6)
            try:
                last_status = _read_fc_modbus_status(fc_id)
            except Exception:
                continue
            if _matches_target(last_status, heat_c, cool_c):
                applied = True
                break

        if applied:
            break
        if publish_attempt == 0:
            resend_done = True
            logger.warning(
                "Preset not yet applied after first publish, retrying once fc_id=%s preset=%s",
                fc_id,
                preset,
            )

    return {
        "status": "ok" if applied else "pending",
        "fc_id": fc_id,
        "preset": preset,
        "temperature_heat_c": heat_c,
        "temperature_cool_c": cool_c,
        "temperature_heat_raw": int(round(heat_c * 10)),
        "temperature_cool_raw": int(round(cool_c * 10)),
        "applied": bool(applied),
        "resend": bool(resend_done),
        "status_readback": last_status,
    }


@app.post("/api/thermal/machines/{fc_id}/setpoint")
def set_machine_setpoint(
    fc_id: int,
    body: ThermalZoneSetpointCommand,
    user=Depends(require_role(THERMAL_CONTROL_ROLES)),
):
    def raw_to_celsius(raw_value: int | float | None) -> float | None:
        if raw_value is None:
            return None
        numeric = _try_float(raw_value)
        if numeric is None:
            return None
        return numeric / 10.0

    def _matches_target(status_payload: dict[str, Any] | None, target_heat_c: float, target_cool_c: float) -> bool:
        if not isinstance(status_payload, dict):
            return False
        heat = _try_float(status_payload.get("setpoint_heat_c"))
        cool = _try_float(status_payload.get("setpoint_cool_c"))
        if heat is None or cool is None:
            return False
        return abs(heat - target_heat_c) <= 0.2 and abs(cool - target_cool_c) <= 0.2

    heat_c = None
    cool_c = None

    heat_c = (
        heat_c
        or raw_to_celsius(body.temperature_heat_raw)
        or _try_float(body.temperature_heat_c)
    )
    cool_c = (
        cool_c
        or raw_to_celsius(body.temperature_cool_raw)
        or _try_float(body.temperature_cool_c)
    )

    common_raw_c = raw_to_celsius(body.temperature_raw)
    common_c = _try_float(body.temperature_c if body.temperature_c is not None else body.temperature)

    if heat_c is None:
        heat_c = common_raw_c if common_raw_c is not None else common_c
    if cool_c is None:
        cool_c = common_raw_c if common_raw_c is not None else common_c

    if heat_c is None and cool_c is None:
        raise HTTPException(status_code=400, detail="Temperatura non specificata")

    resolved_heat_c = _normalize_temperature_c(
        heat_c if heat_c is not None else (cool_c if cool_c is not None else 0.0)
    )
    resolved_cool_c = _normalize_temperature_c(
        cool_c if cool_c is not None else resolved_heat_c
    )
    heat_raw = int(round(resolved_heat_c * 10))
    cool_raw = int(round(resolved_cool_c * 10))

    last_status: dict[str, Any] | None = None
    applied = False
    resend_done = False

    for publish_attempt in range(2):
        _publish_zone_force_setpoint(fc_id, enabled=True)
        _publish_zone_setpoint(
            fc_id,
            heat_c=resolved_heat_c,
            cool_c=resolved_cool_c,
        )

        for _ in range(6):
            time.sleep(0.6)
            try:
                last_status = _read_fc_modbus_status(fc_id)
            except Exception:
                continue
            if _matches_target(last_status, resolved_heat_c, resolved_cool_c):
                applied = True
                break

        if applied:
            break
        if publish_attempt == 0:
            resend_done = True
            logger.warning(
                "Setpoint not yet applied after first publish, retrying once (fc_id=%s target_heat=%.1f target_cool=%.1f)",
                fc_id,
                resolved_heat_c,
                resolved_cool_c,
            )

    return {
        "status": "ok" if applied else "pending",
        "fc_id": fc_id,
        "temperature_heat_c": resolved_heat_c,
        "temperature_heat_raw": heat_raw,
        "temperature_cool_c": resolved_cool_c,
        "temperature_cool_raw": cool_raw,
        "applied": bool(applied),
        "resend": bool(resend_done),
        "status_readback": last_status,
    }


@app.post("/api/thermal/machines/{fc_id}/force")
def set_machine_force_setpoint(
    fc_id: int,
    body: ThermalZoneForceCommand,
    user=Depends(require_role(THERMAL_CONTROL_ROLES)),
):
    _publish_zone_force_setpoint(fc_id, enabled=bool(body.enabled))
    return {"status": "ok", "fc_id": fc_id, "enabled": bool(body.enabled)}


@app.get("/api/thermal/machines/{fc_id}/status")
def get_machine_modbus_status(
    fc_id: int,
    user=Depends(require_role(THERMAL_CONTROL_ROLES)),
):
    return _read_fc_modbus_status(fc_id)


@app.post("/api/thermal/machines/{fc_id}/mode")
def set_machine_fan_mode(
    fc_id: int,
    body: ThermalZoneFanModeCommand,
    user=Depends(require_role(THERMAL_CONTROL_ROLES)),
):
    target_mode = body.mode
    applied = False
    resend_done = False
    last_status: dict[str, Any] | None = None

    for publish_attempt in range(2):
        _publish_zone_mode(fc_id, mode=target_mode)
        for _ in range(6):
            time.sleep(0.6)
            try:
                last_status = _read_fc_modbus_status(fc_id)
            except Exception:
                continue
            if isinstance(last_status, dict) and last_status.get("fan_mode_key") == target_mode:
                applied = True
                break
        if applied:
            break
        if publish_attempt == 0:
            resend_done = True
            logger.warning(
                "Mode not yet applied after first publish, retrying once fc_id=%s mode=%s",
                fc_id,
                target_mode,
            )

    return {
        "status": "ok" if applied else "pending",
        "fc_id": fc_id,
        "mode": target_mode,
        "applied": bool(applied),
        "resend": bool(resend_done),
        "status_readback": last_status,
    }


@app.get("/api/vmf/machines/{fc_id}/panel", response_model=PanelConfig)
def get_vmf_panel_config(
    fc_id: int,
    user=Depends(require_role(["superad"])),
):
    snapshot = device_store.snapshot()
    fancoil_device = None
    for item in snapshot:
        device_id = item.get("id")
        kind = item.get("kind")
        if device_id == "thermal_fancoils" or kind == "fancoil":
            fancoil_device = item
            break
    if not fancoil_device or not isinstance(fancoil_device.get("data"), dict):
        raise HTTPException(status_code=503, detail="Stato fan-coil non disponibile")
    data = fancoil_device["data"]
    fancoils = data.get("fancoils") or []
    target = None
    for entry in fancoils:
        try:
            entry_id = int(entry.get("id"))
        except Exception:
            continue
        if entry_id == fc_id:
            target = entry
            break
    if not target:
        raise HTTPException(status_code=404, detail="Fan-coil non trovato")
    panel = target.get("panel")
    if not isinstance(panel, dict):
        raise HTTPException(status_code=404, detail="Configurazione pannello non disponibile")
    return PanelConfig(**panel)


@app.post("/api/vmf/machines/{fc_id}/panel")
def set_vmf_panel_config(
    fc_id: int,
    body: PanelConfigUpdate,
    user=Depends(require_role(["superad"])),
):
    payload: dict[str, Any] = {
        "type": "panel_config",
        "fc_id": fc_id,
        "id": fc_id,
        "unit": fc_id,
        "source": "backend",
    }
    update_fields = body.model_dump(exclude_none=True)
    payload.update(update_fields)
    try:
        _publish_mqtt_message(FANCOIL_COMMAND_TOPIC, payload, qos=1, retries=3, retry_delay_s=0.20)
    except Exception as exc:
        logger.exception("Errore pubblicazione panel_config (fc_id=%s)", fc_id)
        raise HTTPException(status_code=500, detail="MQTT publish failed") from exc
    return {"status": "queued", "fc_id": fc_id, "updated_fields": sorted(update_fields.keys())}




def _extract_energy_channels_from_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    for key in ("channels", "circuits", "status", "measurements"):
        value = payload.get(key)
        if isinstance(value, list):
            return [entry for entry in value if isinstance(entry, dict)]
    return []


def _sum_energy_metric(channels: list[dict[str, Any]], *keys: str) -> float | None:
    total = 0.0
    found = False
    for ch in channels:
        for key in keys:
            val = _try_float(ch.get(key))
            if val is not None:
                total += val
                found = True
                break
    return total if found else None


def _detect_energy_line(dev: dict[str, Any], payload: dict[str, Any]) -> str:
    dev_id = str(dev.get("id") or "").lower()
    dev_name = str(dev.get("name") or "").lower()
    merged = f"{dev_id} {dev_name}"
    if "linea_a" in merged or "linea a" in merged or "line_a" in merged or "line-a" in merged:
        return "A"
    if "linea_b" in merged or "linea b" in merged or "line_b" in merged or "line-b" in merged:
        return "B"

    payload_line = str(payload.get("line") or payload.get("line_id") or payload.get("line_name") or "").upper().strip()
    if payload_line in {"A", "B"}:
        return payload_line

    return "UNKNOWN"


def _collect_energy_history_rows() -> list[dict[str, Any]]:
    snapshot = device_store.snapshot()
    rows: list[dict[str, Any]] = []
    now_ts = int(time.time())
    for dev in snapshot:
        dev_id = str(dev.get("id") or "")
        kind = str(dev.get("kind") or "").lower()
        if not dev_id:
            continue
        if (
            "energy" not in dev_id.lower()
            and "refoss_em06" not in dev_id.lower()
            and kind not in {"energy", "power", "consumption", "meter"}
        ):
            continue
        payload = dev.get("data") if isinstance(dev.get("data"), dict) else {}
        channels = _extract_energy_channels_from_payload(payload)
        power = _sum_energy_metric(channels, "power_w", "power", "watts")
        if power is None:
            power = _try_float(payload.get("power") or payload.get("total_power"))
        day = _sum_energy_metric(channels, "day_energy_kwh", "day_energy", "today_energy", "energy_today")
        week = _sum_energy_metric(channels, "week_energy_kwh", "week_energy")
        month = _sum_energy_metric(channels, "month_energy_kwh", "month_energy")
        total = _sum_energy_metric(channels, "total_energy", "energy_total", "energy", "total_kwh")

        line = _detect_energy_line(dev, payload)

        rows.append(
            {
                "ts": now_ts,
                "device_id": dev_id,
                "line": line,
                "power_w": power,
                "day_kwh": day,
                "week_kwh": week,
                "month_kwh": month,
                "total_kwh": total,
            }
        )
    return rows


def _energy_history_loop() -> None:
    path = ENERGY_HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Energy history collector attivo: file=%s interval=%ss", path, ENERGY_HISTORY_INTERVAL_S)
    while not energy_history_stop.is_set():
        try:
            rows = _collect_energy_history_rows()
            if rows:
                with path.open("a", encoding="utf-8") as fp:
                    for row in rows:
                        fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.exception("Energy history collector error")
        energy_history_stop.wait(ENERGY_HISTORY_INTERVAL_S)


@app.get("/api/energy/history")
def get_energy_history(
    hours: int = Query(default=168, ge=1, le=24 * 90),
    user=Depends(require_role(THERMAL_CONTROL_ROLES)),
):
    path = ENERGY_HISTORY_FILE
    if not path.exists():
        return {"points": []}
    cutoff = int(time.time()) - int(hours) * 3600
    points: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                ts = int(row.get("ts") or 0)
                if ts >= cutoff:
                    points.append(row)
    except Exception:
        logger.exception("Errore lettura energy history")
        raise HTTPException(status_code=500, detail="Errore lettura storico energia")
    return {"points": points}


@app.on_event("startup")
def _startup():
    # Ensure DB exists for events
    os.makedirs(os.path.dirname(ev.DB_PATH), exist_ok=True)
    ev.init_db()
    # Ensure users DB exists and a bootstrap admin/admin if needed
    os.makedirs(os.path.dirname(adb.DB_PATH), exist_ok=True)
    adb.init_db()
    adb.ensure_bootstrap_user()
    # Tasks DB
    tk.init_db()
    # Work logs DB
    wl.init_db()
    # Rooms/bookings
    bk.init_db()
    # external calendars
    xcal.init_db()
    inv.init_db()
    mt.init_db()

    global device_bridge
    if DEVICE_CONFIGS:
        bridge = DeviceMQTTBridge(device_store, DEVICE_CONFIGS, DEVICE_MQTT_SETTINGS)
        device_bridge = bridge
        bridge.start()
        logger.info("Device bridge attivo per %s dispositivi", len(DEVICE_CONFIGS))
    else:
        logger.info("Nessuna configurazione dispositivi MQTT trovata, bridge disattivato")

    # --- ACS bridge ---
    global acs_bridge
    _acs_br = acs_module.ACSMQTTBridge(acs_store, DEVICE_MQTT_SETTINGS)
    acs_bridge = _acs_br
    _acs_br.start()

    global energy_guard_thread
    global energy_history_thread
    if ENERGY_LINE_B_GUARD_ENABLED:
        energy_guard_stop.clear()
        energy_guard_thread = threading.Thread(
            target=_energy_line_b_guard_loop,
            name="energy-guard-lineb",
            daemon=True,
        )
        energy_guard_thread.start()
    else:
        logger.info("Energy guard Linea B disattivato")

    if ENERGY_HISTORY_ENABLED:
        energy_history_stop.clear()
        energy_history_thread = threading.Thread(
            target=_energy_history_loop,
            name="energy-history",
            daemon=True,
        )
        energy_history_thread.start()
    else:
        logger.info("Energy history collector disattivato")

@app.on_event("shutdown")
def _shutdown():
    if device_bridge:
        device_bridge.stop()
    if acs_bridge:
        acs_bridge.stop()
    if energy_guard_thread:
        energy_guard_stop.set()
        energy_guard_thread.join(timeout=2.0)
    if energy_history_thread:
        energy_history_stop.set()
        energy_history_thread.join(timeout=2.0)



# --- ACS Centrale Termica ---

@app.get("/api/acs/state")
def acs_get_state(user=Depends(require_role(acs_module.ACS_ROLES))):
    """Snapshot completo stato ESP32 ACS (temperature, attuatori, allarmi, antileg)."""
    return acs_store.snapshot()


class ACSAntilegIn(BaseModel):
    request: bool


@app.post("/api/acs/antileg")
def acs_set_antileg(body: ACSAntilegIn, user=Depends(require_role(acs_module.ACS_ROLES))):
    """Invia richiesta antilegionella all'ESP32 via MQTT."""
    if not acs_bridge:
        raise HTTPException(status_code=503, detail="ACS bridge non avviato")
    ok = acs_bridge.publish_cmd({"antileg_request": body.request})
    if not ok:
        raise HTTPException(status_code=503, detail="Publish MQTT fallito")
    return {"ok": True, "antileg_request": body.request}


class ACSManualModeIn(BaseModel):
    enabled: bool


class ACSRelayIn(BaseModel):
    name: Literal["C2", "CR", "P4", "P5", "VALVE"]
    state: bool


class ACSPWMIn(BaseModel):
    duty: int = Field(ge=0, le=100)


class ACSSetpointIn(BaseModel):
    key: Literal["solar_target_c", "pdc_target_c", "recirc_target_c", "antileg_target_c"]
    value: float


@app.post("/api/acs/manual-mode")
def acs_set_manual_mode(body: ACSManualModeIn, user=Depends(require_role(acs_module.ACS_ROLES))):
    if not acs_bridge:
        raise HTTPException(status_code=503, detail="ACS bridge non avviato")
    ok = acs_bridge.publish_cmd({"manual_mode": body.enabled})
    if not ok:
        raise HTTPException(status_code=503, detail="Publish MQTT fallito")
    return {"ok": True, "manual_mode": body.enabled}


@app.post("/api/acs/relay")
def acs_set_relay(body: ACSRelayIn, user=Depends(require_role(acs_module.ACS_ROLES))):
    if not acs_bridge:
        raise HTTPException(status_code=503, detail="ACS bridge non avviato")
    ok = acs_bridge.publish_cmd({"relay": {"name": body.name, "state": body.state}})
    if not ok:
        raise HTTPException(status_code=503, detail="Publish MQTT fallito")
    return {"ok": True, "relay": {"name": body.name, "state": body.state}}


@app.post("/api/acs/pwm")
def acs_set_pwm(body: ACSPWMIn, user=Depends(require_role(acs_module.ACS_ROLES))):
    if not acs_bridge:
        raise HTTPException(status_code=503, detail="ACS bridge non avviato")
    ok = acs_bridge.publish_cmd({"pwm": {"duty": body.duty}})
    if not ok:
        raise HTTPException(status_code=503, detail="Publish MQTT fallito")
    return {"ok": True, "pwm": {"duty": body.duty}}


@app.post("/api/acs/setpoint")
def acs_set_setpoint(body: ACSSetpointIn, user=Depends(require_role(acs_module.ACS_ROLES))):
    if not acs_bridge:
        raise HTTPException(status_code=503, detail="ACS bridge non avviato")
    ok = acs_bridge.publish_cmd({"setpoint": {"key": body.key, "value": body.value}})
    if not ok:
        raise HTTPException(status_code=503, detail="Publish MQTT fallito")
    return {"ok": True, "setpoint": {"key": body.key, "value": body.value}}


# --- Tasks APIs ---
class TaskCreateIn(BaseModel):
    type: str  # 'cleaning' | 'maintenance'
    title: str
    description: Optional[str] = None


class WorklogIn(BaseModel):
    date: str
    hours: float
    notes: Optional[str] = None


class WorklogAdminIn(WorklogIn):
    username: str


@app.post("/api/tasks")
def create_task(item: TaskCreateIn, user=Depends(require_role(["superad", "admin", "reception"]))):
    if item.type not in ("cleaning", "maintenance"):
        raise HTTPException(status_code=400, detail="type must be cleaning or maintenance")
    tid = tk.create_task(item.type, item.title, item.description, created_by=user.get("sub"))
    return {"status": "ok", "task_id": tid}


@app.get("/api/worklogs/me", response_model=list[dict])
def list_my_worklogs(
    limit: int = Query(default=60, ge=1, le=365),
    user=Depends(require_role(ALL_ROLES)),
):
    username = user.get("sub")
    return wl.list_user_worklogs(username, limit=limit)


@app.post("/api/worklogs/me", response_model=dict)
def upsert_my_worklog(item: WorklogIn, user=Depends(require_role(ALL_ROLES))):
    username = user.get("sub")
    try:
        dt = datetime.strptime(item.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato data non valido, usa YYYY-MM-DD")
    if item.hours < 0 or item.hours > 24:
        raise HTTPException(status_code=400, detail="Ore lavorate devono essere tra 0 e 24")
    work_date = dt.isoformat()
    wl.upsert_user_worklog(username, work_date, float(item.hours), item.notes)
    return {"status": "ok"}


@app.get("/api/worklogs", response_model=list[dict])
def list_worklogs_admin(
    username: str = Query(...),
    limit: int = Query(default=60, ge=1, le=365),
    user=Depends(require_role(["superad", "admin"])),
):
    return wl.list_user_worklogs(username, limit=limit)


@app.post("/api/worklogs", response_model=dict)
def upsert_worklog_admin(item: WorklogAdminIn, user=Depends(require_role(["superad", "admin"]))):
    try:
        dt = datetime.strptime(item.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato data non valido, usa YYYY-MM-DD")
    if item.hours < 0 or item.hours > 24:
        raise HTTPException(status_code=400, detail="Ore lavorate devono essere tra 0 e 24")
    work_date = dt.isoformat()
    wl.upsert_user_worklog(item.username, work_date, float(item.hours), item.notes)
    return {"status": "ok"}


@app.get("/api/tasks")
def list_tasks(
    t_type: Optional[str] = Query(default=None),
    status_q: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=1000),
    user=Depends(require_role(["superad", "admin", "reception", "maintenance", "cleaning"]))
):
    if t_type and t_type not in ("cleaning", "maintenance"):
        raise HTTPException(status_code=400, detail="invalid type filter")
    if status_q and status_q not in ("open", "in_progress", "done"):
        raise HTTPException(status_code=400, detail="invalid status filter")
    return tk.list_tasks(user.get("role"), user.get("sub"), t_type, status_q, limit)


class TaskUpdateIn(BaseModel):
    status: str  # 'open'|'in_progress'|'done'


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, body: TaskUpdateIn, user=Depends(require_role(["superad", "admin", "maintenance", "cleaning"]))):
    ok = tk.update_task_status(task_id, body.status, user.get("role"), user.get("sub"))
    if not ok:
        raise HTTPException(status_code=403, detail="Not allowed or invalid status")
    return {"status": "ok"}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int, user=Depends(require_role(["superad", "admin", "maintenance", "cleaning"]))):
    ok = tk.delete_task(task_id, user.get("role"), user.get("sub"))
    if not ok:
        raise HTTPException(status_code=400, detail="Task can be deleted only 24h after completion")
    return {"status": "ok"}


# --- Meetings / shared calendar ---
class MeetingCreateIn(BaseModel):
    title: str
    starts_at: int  # epoch seconds
    ends_at: int  # epoch seconds
    description: Optional[str] = None
    location: Optional[str] = None


class MeetingOut(BaseModel):
    id: int
    title: str
    description: Optional[str]
    location: Optional[str]
    starts_at: int
    ends_at: int
    created_by: str


@app.get("/api/meetings", response_model=list[MeetingOut])
def list_meetings(
    limit: int = Query(default=200, ge=1, le=500),
    since: Optional[int] = Query(default=None),
    user=Depends(require_role(["superad", "admin", "reception", "maintenance", "cleaning"]))
):
    now = since if since is not None else int(time.time()) - 86400
    meetings = mt.list_meetings(limit=limit, since=now)
    return [
        MeetingOut(
            id=m["id"],
            title=m["title"],
            description=m["description"] or None,
            location=m["location"] or None,
            starts_at=m["starts_at"],
            ends_at=m["ends_at"],
            created_by=m["created_by"],
        )
        for m in meetings
    ]


@app.post("/api/meetings", response_model=dict)
def create_meeting(item: MeetingCreateIn, user=Depends(require_role(["superad", "admin"]))):
    if not item.title or not item.title.strip():
        raise HTTPException(status_code=400, detail="Title is required")
    if item.ends_at <= item.starts_at:
        raise HTTPException(status_code=400, detail="ends_at must be after starts_at")
    meeting_id = mt.create_meeting(
        title=item.title.strip(),
        description=item.description.strip() if item.description else None,
        location=item.location.strip() if item.location else None,
        starts_at=int(item.starts_at),
        ends_at=int(item.ends_at),
        created_by=user.get("sub"),
    )
    return {"status": "ok", "meeting_id": meeting_id}


@app.delete("/api/meetings/{meeting_id}")
def delete_meeting(meeting_id: int, user=Depends(require_role(["superad", "admin"]))):
    if not mt.delete_meeting(meeting_id):
        raise HTTPException(status_code=404, detail="Meeting not found")
    return {"status": "ok"}


@app.get("/api/meetings/ics-token", response_model=dict)
def get_meetings_ics_token(user=Depends(require_role(["superad", "admin", "reception"]))):
    if not MEETINGS_ICS_TOKEN:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=503,
            detail="Token ICS non configurato. Contatta l'amministratore per configurare MEETINGS_ICS_TOKEN."
        )
    return {"token": MEETINGS_ICS_TOKEN}


@app.get("/api/meetings/ics")
def meetings_ics(token: str = Query(...)):
    if token != MEETINGS_ICS_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    meetings = mt.list_meetings(limit=500, since=int(time.time()) - 86400 * 7)
    ics_lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Piscina Manager//Meetings//EN",
        "CALSCALE:GREGORIAN",
    ]
    for meeting in meetings:
        uid = f"meeting-{meeting['id']}@piscina-manager"
        start = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(meeting["starts_at"]))
        end = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(meeting["ends_at"]))
        created = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(meeting["created_at"]))
        summary = meeting["title"].replace("\\", "\\\\").replace(",", "\\,")
        description = (meeting["description"] or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,")
        location = (meeting["location"] or "").replace("\\", "\\\\").replace(",", "\\,")
        ics_lines.extend([
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{created}",
            f"DTSTART:{start}",
            f"DTEND:{end}",
            f"SUMMARY:{summary}",
            f"DESCRIPTION:{description}",
            f"LOCATION:{location}",
            "END:VEVENT",
        ])
    ics_lines.append("END:VCALENDAR")
    body = "\r\n".join(ics_lines) + "\r\n"
    return Response(content=body, media_type="text/calendar")


@app.post("/api/meetings/ics")
def create_meeting_via_ics(token: str = Query(...), item: Optional[MeetingCreateIn] = None, body: str = Body(None)):
    """
    Webhook per creare eventi dal calendario esterno (es. Google Calendar, Outlook).
    Accetta token ICS e dati evento in JSON o ICS raw.
    Usato per sincronizzazione bidirezionale.
    """
    # Validazione token
    if token != MEETINGS_ICS_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    # Se il corpo è ICS raw, parsare i dati
    if body and isinstance(body, str) and "BEGIN:VEVENT" in body:
        try:
            from icalendar import Calendar
            cal = Calendar.from_ical(body)
            for component in cal.walk('VEVENT'):
                summary = str(component.get('summary', 'Evento da calendario esterno'))
                description = str(component.get('description', ''))
                location = str(component.get('location', ''))
                dtstart = component.get('dtstart')
                dtend = component.get('dtend')
                if dtstart and dtend:
                    starts_at = int(dtstart.dt.timestamp()) if hasattr(dtstart.dt, 'timestamp') else int(dtstart.dt)
                    ends_at = int(dtend.dt.timestamp()) if hasattr(dtend.dt, 'timestamp') else int(dtend.dt)
                    mt.create_meeting(
                        title=summary.strip(),
                        description=description.strip() if description else None,
                        location=location.strip() if location else None,
                        starts_at=starts_at,
                        ends_at=ends_at,
                        created_by="external_calendar"
                    )
            return {"status": "ok", "message": "Evento importato dal calendario"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Errore nel parsing ICS: {str(e)}")
    
    # Se JSON
    if item:
        if not item.title or not item.title.strip():
            raise HTTPException(status_code=400, detail="Title is required")
        if item.ends_at <= item.starts_at:
            raise HTTPException(status_code=400, detail="ends_at must be after starts_at")
        meeting_id = mt.create_meeting(
            title=item.title.strip(),
            description=item.description.strip() if item.description else None,
            location=item.location.strip() if item.location else None,
            starts_at=int(item.starts_at),
            ends_at=int(item.ends_at),
            created_by="external_calendar",
        )
        return {"status": "ok", "meeting_id": meeting_id}
    
    raise HTTPException(status_code=400, detail="Dati evento non forniti")

# --- User admin (superad only) ---
class UserCreateIn(BaseModel):
    username: str
    role: str  # one of superad, admin, reception, maintenance, cleaning
    password: str


@app.post("/internal/reset-admin")
def internal_reset_admin(new_password: str = Body(...)):
    """Reset the admin user's password (development only).

    This endpoint is disabled in production.
    Call with JSON body: {"new_password": "mypassword"}
    """
    if APP_ENV in {"prod", "production"}:
        raise HTTPException(status_code=403, detail="Not allowed in production")
    # Ensure ADMIN_USER is set
    admin_user = ADMIN_USER or os.environ.get("ADMIN_USER", "admin")
    if not admin_user:
        raise HTTPException(status_code=500, detail="ADMIN_USER not configured")
    # Force set password
    from . import auth_db as adb_mod
    adb_mod.force_set_password(admin_user, new_password, role="admin")
    return {"status": "ok", "message": f"Password for {admin_user} reset"}


@app.post("/api/users")
def create_user(item: UserCreateIn, user=Depends(require_role(["superad", "admin"]))):
    if not item.username or not item.username.strip():
        raise HTTPException(status_code=400, detail="Username is required")
    if item.password is None or item.password == "":
        raise HTTPException(status_code=400, detail="Password is required")
    username = item.username.strip()
    must_change = 1 if username == item.password else 0
    ok = adb.add_user(username, item.role, item.password, must_change=must_change)
    if not ok:
        raise HTTPException(status_code=400, detail="Invalid role or user exists")
    return {"status": "ok"}

# --- Rooms / Bookings ---
class BookingCreateIn(BaseModel):
    room_id: int
    check_in: int  # epoch seconds
    check_out: int # epoch seconds
    status: Optional[str] = "confirmed"
    guest1_name: Optional[str] = None
    guest1_allergens: Optional[str] = None
    guest2_name: Optional[str] = None
    guest2_allergens: Optional[str] = None
    pets_count: Optional[int] = 0
    notes: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    arrival_time: Optional[str] = None
    privacy_consent: Optional[bool] = False


class PublicCheckinOut(BaseModel):
    room_id: int
    room_name: str
    check_in: int
    check_out: int
    guest1_name: Optional[str]
    guest1_allergens: Optional[str]
    guest2_name: Optional[str]
    guest2_allergens: Optional[str]
    pets_count: int
    notes: Optional[str]
    contact_email: Optional[str]
    contact_phone: Optional[str]
    arrival_time: Optional[str]
    privacy_consent: bool
    checkin_completed_at: Optional[int]
    checkin_confirmed_at: Optional[int]
    breakfast_menu: Optional[str]
    breakfast_custom: Optional[list[str]]
    breakfast_excluded: list[str] = Field(default_factory=list)
    breakfast_extra_menus: list[str] = Field(default_factory=list)
    breakfast_extra_items: list[str] = Field(default_factory=list)
    breakfast_deadline: Optional[int]
    breakfast_service_start: Optional[int]
    breakfast_service_end: Optional[int]
    breakfast_deadline_label: Optional[str]
    breakfast_service_start_label: Optional[str]
    breakfast_service_end_label: Optional[str]
    breakfast_days: list["BreakfastDayOut"] = Field(default_factory=list)


class PublicCheckinIn(BaseModel):
    guest1_name: Optional[str] = None
    guest1_allergens: Optional[str] = None
    guest2_name: Optional[str] = None
    guest2_allergens: Optional[str] = None
    pets_count: int = Field(default=0, ge=0, le=10)
    notes: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    arrival_time: Optional[str] = None
    privacy_consent: bool


class BreakfastSetIn(BaseModel):
    menu: Optional[str] = None
    custom_items: Optional[list[str]] = None
    excluded_items: Optional[list[str]] = None
    extra_menus: Optional[list[str]] = None
    extra_items: Optional[list[str]] = None


class PublicBreakfastIn(BaseModel):
    service_date: str
    menu: Optional[str] = None
    custom_items: Optional[list[str]] = None
    excluded_items: Optional[list[str]] = None
    extra_menus: Optional[list[str]] = None
    extra_items: Optional[list[str]] = None

class BreakfastDayOut(BaseModel):
    service_date: str
    deadline_ts: Optional[int]
    deadline_label: Optional[str]
    service_start_ts: Optional[int]
    service_start_label: Optional[str]
    service_end_ts: Optional[int]
    service_end_label: Optional[str]
    selection_menu: Optional[str]
    selection_custom: list[str] = Field(default_factory=list)
    selection_excluded: list[str] = Field(default_factory=list)
    selection_extra_menus: list[str] = Field(default_factory=list)
    selection_extra_items: list[str] = Field(default_factory=list)
    deadline_passed: bool = False


def _breakfast_day_info(service_date: date) -> dict[str, Optional[int | str]]:
    service_start_dt = datetime.combine(service_date, dt_time(hour=8, minute=0), APP_TIMEZONE)
    service_end_dt = datetime.combine(service_date, dt_time(hour=10, minute=0), APP_TIMEZONE)
    deadline_dt = datetime.combine(service_date - timedelta(days=1), dt_time(hour=23, minute=59, second=59), APP_TIMEZONE)
    return {
        "deadline_ts": int(deadline_dt.timestamp()),
        "deadline_label": deadline_dt.strftime("%d/%m/%Y %H:%M"),
        "service_start_ts": int(service_start_dt.timestamp()),
        "service_start_label": service_start_dt.strftime("%H:%M"),
        "service_end_ts": int(service_end_dt.timestamp()),
        "service_end_label": service_end_dt.strftime("%H:%M"),
    }


def _compute_breakfast_days(booking: dict) -> list[BreakfastDayOut]:
    plan = booking.get("breakfast_plan") or {}
    if not isinstance(plan, dict):
        plan = {}
    check_in_ts = booking.get("check_in")
    check_out_ts = booking.get("check_out")
    if not check_in_ts or not check_out_ts:
        return []
    start_date = datetime.fromtimestamp(check_in_ts, APP_TIMEZONE).date()
    checkout_dt = datetime.fromtimestamp(check_out_ts, APP_TIMEZONE)
    end_date = checkout_dt.date()
    # Consenti la colazione anche il giorno del check-out
    if end_date < start_date:
        end_date = start_date
    now_ts = int(time.time())
    days: list[BreakfastDayOut] = []
    current = start_date
    while current <= end_date:
        info = _breakfast_day_info(current)
        selection = plan.get(current.isoformat(), {}) if isinstance(plan, dict) else {}
        menu = selection.get("menu") if isinstance(selection, dict) else None
        custom_raw = selection.get("custom") if isinstance(selection, dict) else []
        if isinstance(custom_raw, list):
            custom = [str(item) for item in custom_raw if str(item).strip()]
        elif custom_raw:
            custom = [str(custom_raw)]
        else:
            custom = []
        excluded_raw = selection.get("excluded") if isinstance(selection, dict) else []
        if isinstance(excluded_raw, list):
            excluded = [str(item) for item in excluded_raw if str(item).strip()]
        elif excluded_raw:
            excluded = [str(excluded_raw)]
        else:
            excluded = []
        extra_menus_raw = selection.get("extra_menus") if isinstance(selection, dict) else []
        if isinstance(extra_menus_raw, list):
            extra_menus = [str(item) for item in extra_menus_raw if str(item).strip()]
        elif extra_menus_raw:
            extra_menus = [str(extra_menus_raw)]
        else:
            extra_menus = []
        extra_items_raw = selection.get("extra_items") if isinstance(selection, dict) else []
        if isinstance(extra_items_raw, list):
            extra_items = [str(item) for item in extra_items_raw if str(item).strip()]
        elif extra_items_raw:
            extra_items = [str(extra_items_raw)]
        else:
            extra_items = []
        day = BreakfastDayOut(
            service_date=current.isoformat(),
            deadline_ts=info["deadline_ts"],
            deadline_label=info["deadline_label"],
            service_start_ts=info["service_start_ts"],
            service_start_label=info["service_start_label"],
            service_end_ts=info["service_end_ts"],
            service_end_label=info["service_end_label"],
            selection_menu=menu,
            selection_custom=custom,
            selection_excluded=excluded,
            selection_extra_menus=extra_menus,
            selection_extra_items=extra_items,
            deadline_passed=bool(info["deadline_ts"] and now_ts > info["deadline_ts"]),
        )
        days.append(day)
        current += timedelta(days=1)
    return days


def _select_upcoming_day(days: list[BreakfastDayOut]) -> Optional[BreakfastDayOut]:
    now_ts = int(time.time())
    for day in days:
        if day.deadline_ts and now_ts <= day.deadline_ts:
            return day
    return days[-1] if days else None


@app.get("/api/rooms/overview")
def rooms_overview(user=Depends(require_role(["superad", "admin", "reception"]))):
    return bk.rooms_overview()


@app.post("/api/bookings")
def create_booking(item: BookingCreateIn, user=Depends(require_role(["superad", "admin", "reception"]))):
    if item.check_out <= item.check_in:
        raise HTTPException(status_code=400, detail="check_out must be after check_in")
    bid, token = bk.create_booking(item.model_dump())
    return {"status": "ok", "booking_id": bid, "checkin_token": token}


@app.get("/api/bookings")
def get_bookings(
    since: Optional[int] = Query(default=None),
    until: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None),
    room_id: Optional[int] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    user=Depends(require_role(["superad", "admin", "reception"]))
):
    if status is not None and status not in ("pending", "confirmed", "canceled"):
        raise HTTPException(status_code=400, detail="invalid status filter")
    return bk.list_bookings(since=since, until=until, status=status, room_id=room_id, limit=limit)


class BookingUpdateIn(BaseModel):
    status: str  # pending|confirmed|canceled


@app.patch("/api/bookings/{booking_id}")
def update_booking(booking_id: int, body: BookingUpdateIn, user=Depends(require_role(["superad", "admin", "reception"]))):
    if not bk.update_booking_status(booking_id, body.status):
        raise HTTPException(status_code=400, detail="invalid status or booking not found")
    return {"status": "ok"}


@app.delete("/api/bookings/{booking_id}")
def delete_booking(booking_id: int, user=Depends(require_role(["superad", "admin"]))):
    if not bk.delete_booking(booking_id):
        raise HTTPException(status_code=404, detail="Prenotazione non trovata")
    return {"status": "ok"}


@app.get("/api/bookings/{booking_id}/checkin-link")
def get_checkin_link(booking_id: int, user=Depends(require_role(["superad", "admin", "reception"]))):
    booking = bk.get_booking(booking_id)
    if not booking:
        raise HTTPException(status_code=404, detail="Prenotazione non trovata")
    return {
        "token": booking.get("checkin_token"),
        "completed_at": booking.get("checkin_completed_at"),
    }


@app.post("/api/bookings/{booking_id}/checkin-link/regenerate")
def regenerate_checkin_link(booking_id: int, user=Depends(require_role(["superad", "admin", "reception"]))):
    token = bk.regenerate_checkin_token(booking_id)
    if not token:
        raise HTTPException(status_code=404, detail="Prenotazione non trovata")
    return {"status": "ok", "token": token}


@app.post("/api/bookings/{booking_id}/confirm-arrival")
def confirm_arrival(booking_id: int, user=Depends(require_role(["superad", "admin", "reception"]))):
    confirmed_at = bk.confirm_arrival(booking_id)
    if confirmed_at is None:
        raise HTTPException(status_code=404, detail="Prenotazione non trovata")
    return {"status": "ok", "confirmed_at": confirmed_at}


@app.post("/api/bookings/{booking_id}/breakfast")
def set_breakfast(booking_id: int, body: BreakfastSetIn, user=Depends(require_role(["superad", "admin", "reception"]))):
    menu_value = _sanitize_text(body.menu, 120)
    custom: Optional[list[str]] = None
    if body.custom_items:
        sanitized = []
        for item in body.custom_items:
            clean = _sanitize_text(item, 120)
            if clean:
                sanitized.append(clean)
        custom = sanitized if sanitized else None
    excluded: Optional[list[str]] = None
    if body.excluded_items:
        sanitized_excluded = []
        for item in body.excluded_items:
            clean = _sanitize_text(item, 120)
            if clean:
                sanitized_excluded.append(clean)
        excluded = sanitized_excluded if sanitized_excluded else None
    extra_menus: Optional[list[str]] = None
    if body.extra_menus:
        sanitized_extra_menus = []
        for item in body.extra_menus:
            clean = _sanitize_text(item, 120)
            if clean:
                sanitized_extra_menus.append(clean)
        extra_menus = sanitized_extra_menus if sanitized_extra_menus else None
    extra_items: Optional[list[str]] = None
    if body.extra_items:
        sanitized_extra_items = []
        for item in body.extra_items:
            clean = _sanitize_text(item, 120)
            if clean:
                sanitized_extra_items.append(clean)
        extra_items = sanitized_extra_items if sanitized_extra_items else None
    if not bk.set_breakfast_preferences(
        booking_id,
        menu_value,
        custom,
        excluded_items=excluded,
        extra_menus=extra_menus,
        extra_items=extra_items,
    ):
        raise HTTPException(status_code=404, detail="Prenotazione non trovata")
    return {
        "status": "ok",
        "menu": menu_value,
        "custom_items": custom or [],
        "excluded_items": excluded or [],
        "extra_menus": extra_menus or [],
        "extra_items": extra_items or [],
    }


@app.post("/api/bookings/{booking_id}/breakfast/{service_date}/serve")
def serve_breakfast(booking_id: int, service_date: str, user=Depends(require_role(["superad", "admin", "reception"]))):
    result = bk.mark_breakfast_served(booking_id, service_date)
    if not result:
        raise HTTPException(status_code=404, detail="Prenotazione o data non trovata")
    return result


@app.post("/api/bookings/{booking_id}/breakfast/{service_date}/skip")
def skip_breakfast(booking_id: int, service_date: str, user=Depends(require_role(["superad", "admin", "reception"]))):
    result = bk.mark_breakfast_skipped(booking_id, service_date)
    if not result:
        raise HTTPException(status_code=404, detail="Prenotazione o data non trovata")
    return result


@app.get("/api/public/checkin/{token}", response_model=PublicCheckinOut)
def public_checkin_view(token: str):
    booking = bk.get_booking_by_token(token)
    if not booking:
        raise HTTPException(status_code=404, detail="Link non valido")
    now = int(time.time())
    if now > booking["check_out"]:
        raise HTTPException(status_code=410, detail="Link scaduto: soggiorno concluso")
    if booking.get("status") == "canceled":
        raise HTTPException(status_code=410, detail="Prenotazione annullata")
    room = bk.get_room(booking["room_id"])
    room_name = (room.get("name") if room else None) or bk.default_room_name(booking["room_id"])
    days = _compute_breakfast_days(booking)
    upcoming = _select_upcoming_day(days)
    return PublicCheckinOut(
        room_id=booking["room_id"],
        room_name=room_name,
        check_in=booking["check_in"],
        check_out=booking["check_out"],
        guest1_name=booking.get("guest1_name"),
        guest1_allergens=booking.get("guest1_allergens"),
        guest2_name=booking.get("guest2_name"),
        guest2_allergens=booking.get("guest2_allergens"),
        pets_count=int(booking.get("pets_count") or 0),
        notes=booking.get("notes"),
        contact_email=booking.get("contact_email"),
        contact_phone=booking.get("contact_phone"),
        arrival_time=booking.get("arrival_time"),
        privacy_consent=bool(booking.get("privacy_consent")),
        checkin_completed_at=booking.get("checkin_completed_at"),
        checkin_confirmed_at=booking.get("checkin_confirmed_at"),
        breakfast_menu=upcoming.selection_menu if upcoming else None,
        breakfast_custom=upcoming.selection_custom if upcoming else [],
        breakfast_excluded=upcoming.selection_excluded if upcoming else [],
        breakfast_extra_menus=upcoming.selection_extra_menus if upcoming else [],
        breakfast_extra_items=upcoming.selection_extra_items if upcoming else [],
        breakfast_deadline=upcoming.deadline_ts if upcoming else None,
        breakfast_service_start=upcoming.service_start_ts if upcoming else None,
        breakfast_service_end=upcoming.service_end_ts if upcoming else None,
        breakfast_deadline_label=upcoming.deadline_label if upcoming else None,
        breakfast_service_start_label=upcoming.service_start_label if upcoming else None,
        breakfast_service_end_label=upcoming.service_end_label if upcoming else None,
        breakfast_days=days,
    )


@app.post("/api/public/checkin/{token}")
def public_checkin_submit(token: str, body: PublicCheckinIn):
    booking = bk.get_booking_by_token(token)
    if not booking:
        raise HTTPException(status_code=404, detail="Link non valido")
    now = int(time.time())
    if now > booking["check_out"]:
        raise HTTPException(status_code=410, detail="Link scaduto: soggiorno concluso")
    if booking.get("status") == "canceled":
        raise HTTPException(status_code=410, detail="Prenotazione annullata")
    if not body.privacy_consent:
        raise HTTPException(status_code=400, detail="È necessario accettare il trattamento dei dati")
    payload = body.model_dump()
    payload["guest1_name"] = _sanitize_text(payload.get("guest1_name"), 120)
    payload["guest1_allergens"] = _sanitize_text(payload.get("guest1_allergens"), 200)
    payload["guest2_name"] = _sanitize_text(payload.get("guest2_name"), 120)
    payload["guest2_allergens"] = _sanitize_text(payload.get("guest2_allergens"), 200)
    payload["notes"] = _sanitize_text(payload.get("notes"), 500)
    payload["contact_email"] = _sanitize_email(payload.get("contact_email"))
    payload["contact_phone"] = _sanitize_phone(payload.get("contact_phone"))
    payload["arrival_time"] = _sanitize_text(payload.get("arrival_time"), 60)
    payload["pets_count"] = max(0, min(int(payload.get("pets_count") or 0), 10))
    payload["privacy_consent"] = True
    if not bk.submit_checkin(token, payload):
        raise HTTPException(status_code=500, detail="Impossibile salvare i dati di check-in")
    return {"status": "ok"}


@app.post("/api/public/checkin/{token}/breakfast")
def public_breakfast_submit(token: str, body: PublicBreakfastIn):
    booking = bk.get_booking_by_token(token)
    if not booking:
        raise HTTPException(status_code=404, detail="Link non valido")
    now = int(time.time())
    if now > booking["check_out"]:
        raise HTTPException(status_code=410, detail="Link scaduto: soggiorno concluso")
    if booking.get("status") == "canceled":
        raise HTTPException(status_code=410, detail="Prenotazione annullata")
    if not booking.get("checkin_confirmed_at"):
        raise HTTPException(status_code=403, detail="La reception deve prima confermare l'arrivo")
    try:
        service_date = datetime.strptime(body.service_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato data non valido")

    check_in_date = datetime.fromtimestamp(booking["check_in"], APP_TIMEZONE).date()
    check_out_dt = datetime.fromtimestamp(booking["check_out"], APP_TIMEZONE)
    check_out_date = check_out_dt.date()
    if service_date < check_in_date or service_date > check_out_date:
        raise HTTPException(status_code=400, detail="Data fuori dal soggiorno")

    day_info = _breakfast_day_info(service_date)
    if day_info["deadline_ts"] and now > day_info["deadline_ts"]:
        raise HTTPException(status_code=403, detail="Le ordinazioni sono chiuse: tempo limite superato")
    menu_value = _sanitize_text(body.menu, 120)
    custom: Optional[list[str]] = None
    if body.custom_items:
        sanitized = []
        for item in body.custom_items:
            clean = _sanitize_text(item, 120)
            if clean:
                sanitized.append(clean)
        custom = sanitized if sanitized else None
    excluded: Optional[list[str]] = None
    if body.excluded_items:
        sanitized_excluded = []
        for item in body.excluded_items:
            clean = _sanitize_text(item, 120)
            if clean:
                sanitized_excluded.append(clean)
        excluded = sanitized_excluded if sanitized_excluded else None
    extra_menus: Optional[list[str]] = None
    if body.extra_menus:
        sanitized_extra_menus = []
        for item in body.extra_menus:
            clean = _sanitize_text(item, 120)
            if clean:
                sanitized_extra_menus.append(clean)
        extra_menus = sanitized_extra_menus if sanitized_extra_menus else None
    extra_items: Optional[list[str]] = None
    if body.extra_items:
        sanitized_extra_items = []
        for item in body.extra_items:
            clean = _sanitize_text(item, 120)
            if clean:
                sanitized_extra_items.append(clean)
        extra_items = sanitized_extra_items if sanitized_extra_items else None
    plan = bk.set_breakfast_for_date(
        booking["id"],
        service_date.isoformat(),
        menu_value,
        custom or [],
        excluded_items=excluded or [],
        extra_menus=extra_menus or [],
        extra_items=extra_items or [],
    )
    if plan is None:
        raise HTTPException(status_code=500, detail="Impossibile salvare la colazione")
    updated_booking = bk.get_booking(booking["id"]) or booking
    days = _compute_breakfast_days(updated_booking)
    updated_day = next((day for day in days if day.service_date == service_date.isoformat()), None)
    return {
        "status": "ok",
        "service_date": service_date.isoformat(),
        "menu": updated_day.selection_menu if updated_day else menu_value,
        "custom_items": updated_day.selection_custom if updated_day else (custom or []),
        "excluded_items": updated_day.selection_excluded if updated_day else (excluded or []),
        "extra_menus": updated_day.selection_extra_menus if updated_day else (extra_menus or []),
        "extra_items": updated_day.selection_extra_items if updated_day else (extra_items or []),
        "breakfast_days": [day.model_dump() for day in days],
    }

# --- Inventory (colazione) ---
class InventoryItem(BaseModel):
    item: str
    quantity: int
    unit: Optional[str] = None


class InventoryBulk(BaseModel):
    items: list[InventoryItem]


@app.get("/api/inventory")
def get_inventory(user=Depends(require_role(["superad", "admin", "reception"]))):
    return inv.list_inventory()


@app.patch("/api/inventory")
def patch_inventory(body: InventoryBulk, user=Depends(require_role(["superad", "admin", "reception"]))):
    inv.upsert_items([i.model_dump() for i in body.items])
    return {"status": "ok"}


@app.post("/api/inventory")
def post_inventory(item: InventoryItem, user=Depends(require_role(["superad", "admin", "reception"]))):
    inv.set_item(item.item, item.quantity, item.unit)
    return {"status": "ok"}

# --- External calendars (Booking iCal) ---
class CalendarSetIn(BaseModel):
    url: str
    source: Optional[str] = "booking"
    checkin_hour: Optional[int] = 15
    checkout_hour: Optional[int] = 11


@app.post("/api/rooms/{room_id}/calendar")
def set_room_calendar(room_id: int, body: CalendarSetIn, user=Depends(require_role(["superad", "admin"]))):
    xcal.set_room_calendar(room_id, body.url, body.source or "booking", int(body.checkin_hour or 15), int(body.checkout_hour or 11))
    return {"status": "ok"}


@app.get("/api/rooms/{room_id}/calendar/preview")
def preview_room_calendar(room_id: int, limit: int = 20, user=Depends(require_role(["superad", "admin", "reception"]))):
    cfg = xcal.get_room_calendar(room_id)
    if not cfg:
        return []
    try:
        ics = xcal.fetch_ics(cfg['url'])
        return xcal.parse_upcoming_ics(ics, checkin_hour=cfg['checkin_hour'], checkout_hour=cfg['checkout_hour'], limit=limit)
    except ValueError as e:
        # Return error as a properly formatted error response
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        from fastapi import HTTPException
        logger.exception(f"Errore nel preview calendario per camera {room_id}")
        raise HTTPException(status_code=500, detail=f"Errore nel caricamento del calendario: {str(e)}")


class UserUpdateIn(BaseModel):
    role: Optional[str] = None
    new_password: Optional[str] = None
    must_change: Optional[bool] = None


@app.get("/api/users")
def list_users(user=Depends(require_role(["superad", "admin"]))):
    return adb.list_users()


@app.patch("/api/users/{username}")
def update_user(username: str, body: UserUpdateIn, user=Depends(require_role(["superad", "admin"]))):
    updated = False
    if body.role is not None:
        if not adb.set_role(username, body.role):
            raise HTTPException(status_code=400, detail="Invalid role or user not found")
        updated = True
    if body.new_password is not None:
        if body.new_password == "":
            raise HTTPException(status_code=400, detail="Password is required")
        must_change = 1 if username == body.new_password else 0
        if not adb.reset_password(username, body.new_password, must_change):
            raise HTTPException(status_code=400, detail="User not found")
        updated = True
    if not updated:
        raise HTTPException(status_code=400, detail="No changes provided")
    return {"status": "ok"}


@app.delete("/api/users/{username}")
def delete_user(username: str, user=Depends(require_role(["superad", "admin"]))):
    if username == user.get("sub"):
        raise HTTPException(status_code=400, detail="Cannot delete your own user")
    if not adb.delete_user(username):
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "ok"}


# --- Mail (IMAP) ---
MAIL_VIEW_ROLES = ["superad", "admin", "reception"]


@app.get("/api/mail/accounts", response_model=list[MailAccountOut])
def list_mail_accounts(user=Depends(require_role(MAIL_VIEW_ROLES))):
    accounts = _get_mail_accounts()
    return [MailAccountOut(address=addr, sent_folder=MAIL_SENT_FOLDER) for addr in sorted(accounts.keys())]


@app.get("/api/mail/messages", response_model=list[MailMessageOut])
def list_mail_messages(
    account: str = Query(..., description="Mailbox account"),
    folder: str = Query("INBOX", description="IMAP folder"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user=Depends(require_role(MAIL_VIEW_ROLES)),
):
    password = _get_mail_password(account)
    server = None
    try:
        server = _imap_connect(account, password)
        status, _ = server.select(folder, readonly=True)
        if status != "OK":
            raise HTTPException(status_code=400, detail="Cartella non valida")
        status, data = server.uid("search", None, "ALL")
        if status != "OK" or not data:
            return []
        raw_uids = data[0].split() if data[0] else []
        if not raw_uids:
            return []
        uids_sorted = sorted(raw_uids, key=lambda x: int(x), reverse=True)
        slice_uids = uids_sorted[offset: offset + limit]
        if not slice_uids:
            return []
        uid_set = ",".join(
            uid.decode(errors="ignore") if isinstance(uid, bytes) else str(uid)
            for uid in slice_uids
        )
        status, data = server.uid(
            "fetch",
            uid_set,
            "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)] FLAGS RFC822.SIZE)",
        )
        if status != "OK" or not data:
            return []
        messages: list[MailMessageOut] = []
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta, header_bytes = item
            meta_info = _parse_fetch_meta(meta or b"")
            uid_val = meta_info.get("uid")
            if uid_val is None:
                continue
            headers = _parse_header_bytes(header_bytes or b"")
            flags = meta_info.get("flags") or []
            seen = any(flag == "\\Seen" for flag in flags)
            messages.append(
                MailMessageOut(
                    uid=uid_val,
                    subject=headers.get("subject", ""),
                    from_=headers.get("from", ""),
                    to=headers.get("to", ""),
                    date=headers.get("date", ""),
                    size=meta_info.get("size"),
                    seen=seen,
                )
            )
        messages.sort(key=lambda m: m.uid, reverse=True)
        return messages
    except imaplib.IMAP4.error:
        raise HTTPException(status_code=502, detail="Errore accesso IMAP")
    finally:
        if server is not None:
            try:
                server.logout()
            except Exception:
                pass


@app.get("/api/mail/message/{uid}", response_model=MailMessageDetailOut)
def get_mail_message(
    uid: int,
    account: str = Query(..., description="Mailbox account"),
    folder: str = Query("INBOX", description="IMAP folder"),
    user=Depends(require_role(MAIL_VIEW_ROLES)),
):
    password = _get_mail_password(account)
    server = None
    try:
        server = _imap_connect(account, password)
        status, _ = server.select(folder, readonly=False)
        if status != "OK":
            raise HTTPException(status_code=400, detail="Cartella non valida")
        raw_msg = _fetch_message_bytes(server, uid)
        if not raw_msg:
            raise HTTPException(status_code=404, detail="Messaggio non trovato")
        try:
            server.uid("store", str(uid), "+FLAGS", "(\\Seen)")
        except imaplib.IMAP4.error:
            # Alcuni server/cartelle possono essere read-only: non blocchiamo la lettura.
            pass
        message = BytesParser(policy=email_default).parsebytes(raw_msg)
        subject = _clean_header(_decode_header_value(message.get("subject", "")))
        from_value = _clean_header(_format_address(message.get("from", "")))
        to_value = _clean_header(_format_address(message.get("to", "")))
        date_value = _clean_header(message.get("date", "") or "")

        text_parts: list[str] = []
        html_parts: list[str] = []
        attachments: list[MailAttachmentOut] = []
        for idx, part in _iter_message_parts(message):
            content_type = part.get_content_type()
            content_disposition = part.get_content_disposition()
            filename = part.get_filename()
            if filename or content_disposition == "attachment":
                safe_name = _safe_filename(filename, f"allegato-{uid}-{idx}")
                attachments.append(
                    MailAttachmentOut(
                        id=str(idx),
                        filename=safe_name,
                        content_type=content_type,
                    )
                )
                continue
            try:
                payload = part.get_content()
            except Exception:
                payload = ""
            if content_type == "text/plain":
                if payload:
                    text_parts.append(payload)
            elif content_type == "text/html":
                if payload:
                    html_parts.append(payload)

        text_body = "\n\n-----\n\n".join([p for p in text_parts if p])
        html_body = "\n<hr />\n".join([p for p in html_parts if p])
        text_body = _truncate_text(text_body, MAIL_BODY_MAX_CHARS)
        html_body = _truncate_text(html_body, MAIL_BODY_MAX_CHARS)

        return MailMessageDetailOut(
            uid=uid,
            subject=subject,
            from_=from_value,
            to=to_value,
            date=date_value,
            text=text_body or None,
            html=html_body or None,
            attachments=attachments,
        )
    except imaplib.IMAP4.error:
        raise HTTPException(status_code=502, detail="Errore accesso IMAP")
    finally:
        if server is not None:
            try:
                server.logout()
            except Exception:
                pass


@app.get("/api/mail/attachment/{uid}/{part_id}")
def get_mail_attachment(
    uid: int,
    part_id: str,
    account: str = Query(..., description="Mailbox account"),
    folder: str = Query("INBOX", description="IMAP folder"),
    user=Depends(require_role(MAIL_VIEW_ROLES)),
):
    password = _get_mail_password(account)
    server = None
    try:
        server = _imap_connect(account, password)
        status, _ = server.select(folder, readonly=True)
        if status != "OK":
            raise HTTPException(status_code=400, detail="Cartella non valida")
        raw_msg = _fetch_message_bytes(server, uid)
        if not raw_msg:
            raise HTTPException(status_code=404, detail="Messaggio non trovato")
        message = BytesParser(policy=email_default).parsebytes(raw_msg)
        for idx, part in _iter_message_parts(message):
            if str(idx) != part_id:
                continue
            filename = _safe_filename(part.get_filename(), f"allegato-{uid}-{idx}")
            content_type = part.get_content_type() or "application/octet-stream"
            payload = part.get_payload(decode=True) or b""
            headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
            return Response(content=payload, media_type=content_type, headers=headers)
        raise HTTPException(status_code=404, detail="Allegato non trovato")
    except imaplib.IMAP4.error:
        raise HTTPException(status_code=502, detail="Errore accesso IMAP")
    finally:
        if server is not None:
            try:
                server.logout()
            except Exception:
                pass


@app.post("/api/mail/send", response_model=MailSendResponse)
def send_mail(
    account: str = Form(...),
    to: str = Form(...),
    subject: str = Form(""),
    text: str | None = Form(None),
    html: str | None = Form(None),
    reply_to_uid: int | None = Form(None),
    reply_folder: str = Form("INBOX"),
    attachments: list[UploadFile] = File(default_factory=list),
    user=Depends(require_role(MAIL_VIEW_ROLES)),
):
    password = _get_mail_password(account)

    if not text and not html and not attachments:
        raise HTTPException(status_code=400, detail="Messaggio vuoto")

    in_reply_to = None
    references = None
    if reply_to_uid:
        server = None
        try:
            server = _imap_connect(account, password)
            status, _ = server.select(reply_folder, readonly=True)
            if status != "OK":
                raise HTTPException(status_code=400, detail="Cartella non valida")
            raw_msg = _fetch_message_bytes(server, reply_to_uid)
            if raw_msg:
                msg = BytesParser(policy=email_default).parsebytes(raw_msg)
                message_id = msg.get("message-id")
                if message_id:
                    in_reply_to = message_id
                    existing_refs = msg.get("references")
                    if existing_refs:
                        references = f"{existing_refs} {message_id}"
                    else:
                        references = message_id
        finally:
            if server is not None:
                try:
                    server.logout()
                except Exception:
                    pass

    files_payload: list[tuple[str, str, bytes]] = []
    total_size = 0
    for idx, upload in enumerate(attachments or []):
        data = upload.file.read()
        total_size += len(data)
        if total_size > MAIL_ATTACHMENT_MAX_BYTES:
            raise HTTPException(status_code=413, detail="Allegati troppo grandi")
        filename = _safe_filename(upload.filename, f"allegato-{idx}")
        content_type = upload.content_type or "application/octet-stream"
        files_payload.append((filename, content_type, data))

    raw_msg, to_addrs = _build_email_message(
        account=account,
        to_raw=to,
        subject=subject,
        text=text,
        html=html,
        attachments=files_payload,
        in_reply_to=in_reply_to,
        references=references,
    )

    _smtp_send_message(account, password, to_addrs, raw_msg)

    saved = True
    warning = None
    try:
        _append_sent_message(account, password, raw_msg)
    except Exception:
        saved = False
        warning = "Impossibile salvare in posta inviata"

    return MailSendResponse(status="ok", sent=True, saved=saved, warning=warning)


@app.post("/api/mail/message/{uid}/move")
def move_mail_message(
    uid: int,
    account: str = Query(...),
    folder: str = Query("INBOX"),
    destination: str = Query(..., description="Cartella di destinazione"),
    user=Depends(require_role(MAIL_VIEW_ROLES)),
):
    """Sposta un messaggio in un'altra cartella (es. Cestino, Archivio, Spam)."""
    password = _get_mail_password(account)
    server = None
    try:
        server = _imap_connect(account, password)
        status, _ = server.select(folder, readonly=False)
        if status != "OK":
            raise HTTPException(status_code=400, detail="Cartella sorgente non valida")
        result, _ = server.uid("copy", str(uid), destination)
        if result != "OK":
            raise HTTPException(status_code=400, detail=f"Impossibile copiare in '{destination}'")
        server.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
        server.expunge()
        return {"status": "ok"}
    except imaplib.IMAP4.error as e:
        raise HTTPException(status_code=502, detail=f"Errore IMAP: {e}")
    finally:
        if server is not None:
            try:
                server.logout()
            except Exception:
                pass


@app.post("/api/mail/message/{uid}/flag")
def flag_mail_message(
    uid: int,
    account: str = Query(...),
    folder: str = Query("INBOX"),
    seen: bool = Query(..., description="True=letto, False=non letto"),
    user=Depends(require_role(MAIL_VIEW_ROLES)),
):
    """Segna un messaggio come letto o non letto."""
    password = _get_mail_password(account)
    server = None
    try:
        server = _imap_connect(account, password)
        status, _ = server.select(folder, readonly=False)
        if status != "OK":
            raise HTTPException(status_code=400, detail="Cartella non valida")
        op = "+FLAGS" if seen else "-FLAGS"
        server.uid("store", str(uid), op, "(\\Seen)")
        return {"status": "ok"}
    except imaplib.IMAP4.error as e:
        raise HTTPException(status_code=502, detail=f"Errore IMAP: {e}")
    finally:
        if server is not None:
            try:
                server.logout()
            except Exception:
                pass
