# --- state.py ---
# Stato globale condiviso tra tutti i moduli.

import time

try:
    import ujson as json
except ImportError:
    import json

import config

# ── Temperature (°C float, None = invalid/non ancora letta) ──────────────────
temps = {label: None for label in config.SENSOR_LABELS}

# ── Stato attuatori reali ─────────────────────────────────────────────────────
c1_duty = 0
relay_states = {name: False for name in config.OUTPUT_PINS}
relay_available = {name: False for name in config.OUTPUT_PINS}

# Legacy keys usate già nel portale
c2_on = False
cr_on = False
piscina_on = False
valve_on = False

# ── Comandi manuali da portale ────────────────────────────────────────────────
manual_mode = False
manual_pwm_duty = 0
manual_relays = {name: False for name in config.OUTPUT_PINS}

# ── Setpoint configurabili ────────────────────────────────────────────────────
setpoints = {key: meta['default'] for key, meta in config.SETPOINTS.items()}
setpoint_meta = {
    key: {
        'label': meta['label'],
        'min': meta['min'],
        'max': meta['max'],
        'step': meta['step'],
    }
    for key, meta in config.SETPOINTS.items()
}

# ── Allarmi sensori ───────────────────────────────────────────────────────────
alarms = {
    'ALARM_SENSORS_PANELS': False,
    'ALARM_SENSORS_C2': False,
    'ALARM_SENSORS_CR': False,
    'ALARM_S4_INVALID': False,
}

# ── Stato interno placeholder per logiche future ─────────────────────────────
c1_on_state = False
c1_latched_hard_stop = False
c2_on_state = False
cr_on_state = False
cr_emerg_mode = False

# ── Antilegionella ────────────────────────────────────────────────────────────
antileg_request = False
antileg_ok = False
antileg_ok_ts = None
antileg_hold_start = None

# ── Block 2 outputs ───────────────────────────────────────────────────────────
block2_outputs = {
    'gas_enable': False,
    'valve_relay': False,
    'pdc_cmd_start_c2': False,
    'heat_pump': False,
    'piscina_pump': False,
}

# ── Snapshot ──────────────────────────────────────────────────────────────────
last_snapshot_ts = 0


def _sync_legacy_relays():
    global c2_on, cr_on, piscina_on, valve_on
    c2_on = bool(relay_states.get('C2'))
    cr_on = bool(relay_states.get('CR'))
    piscina_on = bool(relay_states.get('PISCINA_PUMP'))
    valve_on = bool(relay_states.get('VALVE'))


def _clamp_float(value, low, high):
    if value < low:
        return low
    if value > high:
        return high
    return value


def _normalize_setpoint(key, value):
    meta = config.SETPOINTS.get(key)
    if meta is None:
        raise KeyError(key)
    value = float(value)
    return _clamp_float(value, meta['min'], meta['max'])


def _save_settings():
    payload = {
        'setpoints': setpoints,
    }
    with open(config.SETPOINTS_FILE, 'w') as fp:
        fp.write(json.dumps(payload))


def load_settings():
    try:
        with open(config.SETPOINTS_FILE, 'r') as fp:
            raw = fp.read()
        payload = json.loads(raw)
    except Exception:
        print('[state] settings assenti, uso default')
        return False

    loaded = payload.get('setpoints', payload) if isinstance(payload, dict) else {}
    changed = False
    for key in config.SETPOINTS:
        if key not in loaded:
            continue
        try:
            setpoints[key] = _normalize_setpoint(key, loaded[key])
            changed = True
        except Exception as e:
            print('[state] setpoint {} invalido: {}'.format(key, e))

    if changed:
        print('[state] setpoint caricati da {}'.format(config.SETPOINTS_FILE))
    return changed


def save_settings():
    try:
        _save_settings()
        print('[state] setpoint salvati')
        return True
    except Exception as e:
        print('[state] save error:', e)
        return False


def set_temp(label, value):
    if label in temps:
        temps[label] = value


def set_all_temps(values):
    for label in temps:
        temps[label] = values.get(label)
    refresh_sensor_alarms()


def refresh_sensor_alarms():
    alarms['ALARM_SENSORS_PANELS'] = any(temps.get(label) is None for label in ('S1', 'S2', 'S3'))
    alarms['ALARM_SENSORS_C2'] = any(temps.get(label) is None for label in ('S2', 'S3', 'S4', 'S5'))
    alarms['ALARM_SENSORS_CR'] = temps.get('S6') is None and temps.get('S7') is None
    alarms['ALARM_S4_INVALID'] = temps.get('S4') is None


def set_c1_duty(duty_percent):
    global c1_duty
    c1_duty = max(0, min(100, int(duty_percent)))


def set_relay_output(name, value):
    if name not in relay_states:
        return
    relay_states[name] = bool(value)
    _sync_legacy_relays()


def set_relay_available(name, value):
    if name in relay_available:
        relay_available[name] = bool(value)


def set_manual_mode(enabled):
    global manual_mode
    manual_mode = bool(enabled)


def set_manual_pwm_duty(duty_percent):
    global manual_pwm_duty
    manual_pwm_duty = max(0, min(100, int(duty_percent)))


def set_manual_relay(name, value):
    if name not in manual_relays:
        raise KeyError(name)
    manual_relays[name] = bool(value)


def set_block2_outputs(values):
    """Update Block 2 outputs."""
    for name in block2_outputs:
        block2_outputs[name] = bool(values.get(name, False))


def snapshot():
    """Restituisce dict serializzabile per logging/MQTT."""
    return {
        'ts': time.time(),
        'temps': dict(temps),
        'c1_duty': c1_duty,
        'c2_on': c2_on,
        'cr_on': cr_on,
        'piscina_on': piscina_on,
        'valve_on': valve_on,
        'relays': dict(relay_states),
        'relay_available': dict(relay_available),
        'manual_mode': manual_mode,
        'manual_relays': dict(manual_relays),
        'manual_pwm_duty': manual_pwm_duty,
        'setpoints': dict(setpoints),
        'setpoint_meta': dict(setpoint_meta),
        'alarms': dict(alarms),
        'c1_latch': c1_latched_hard_stop,
        'cr_emerg': cr_emerg_mode,
        'antileg_ok': antileg_ok,
        'antileg_ok_ts': antileg_ok_ts,
        'antileg_request': antileg_request,
        'block2': dict(block2_outputs),
    }
