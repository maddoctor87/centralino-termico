# --- state.py ---
# Stato globale condiviso tra tutti i moduli.

import time

try:
    import ujson as json
except ImportError:
    import json

import config

# ── Temperature ───────────────────────────────────────────────────────────────
temps = {label: None for label in config.SENSOR_LABELS}

# ── Stato attuatori ───────────────────────────────────────────────────────────
c1_duty = 0
relay_states     = {name: False for name in config.RELAY_OUTPUTS}
relay_available  = {name: False for name in config.RELAY_OUTPUTS}
manual_relays    = {name: False for name in config.RELAY_OUTPUTS}

# Legacy keys per portale
c2_on    = False
cr_on    = False
p4_on    = False
valve_on = False

# ── Comandi manuali ───────────────────────────────────────────────────────────
manual_mode     = False
manual_pwm_duty = 0

# ── Setpoint ──────────────────────────────────────────────────────────────────
setpoints = {key: meta['default'] for key, meta in config.SETPOINTS.items()}
setpoint_meta = {
    key: {
        'label': meta['label'],
        'min':   meta['min'],
        'max':   meta['max'],
        'step':  meta['step'],
    }
    for key, meta in config.SETPOINTS.items()
}

# ── Allarmi ───────────────────────────────────────────────────────────────────
alarms = {
    'ALARM_SENSORS_PANELS': False,
    'ALARM_SENSORS_C2':     False,
    'ALARM_SENSORS_CR':     False,
    'ALARM_S4_INVALID':     False,
}

# ── Stato logiche ─────────────────────────────────────────────────────────────
c1_on_state          = False
c1_latched_hard_stop = False
c2_on_state          = False
cr_on_state          = False
cr_emerg_mode        = False

# ── Antilegionella ────────────────────────────────────────────────────────────
antileg_request   = False
antileg_ok        = False
antileg_ok_ts     = None
antileg_hold_start= None

# ── Snapshot ts ───────────────────────────────────────────────────────────────
last_snapshot_ts = 0


def _sync_legacy_relays():
    global c2_on, cr_on, p4_on, valve_on
    c2_on    = bool(relay_states.get('C2'))
    cr_on    = bool(relay_states.get('CR'))
    p4_on    = bool(relay_states.get('P4'))
    valve_on = bool(relay_states.get('VALVE'))


def _clamp_float(value, low, high):
    if value < low:  return low
    if value > high: return high
    return value


def _normalize_setpoint(key, value):
    meta = config.SETPOINTS.get(key)
    if meta is None:
        raise KeyError(key)
    return _clamp_float(float(value), meta['min'], meta['max'])


def _save_settings():
    with open(config.SETPOINTS_FILE, 'w') as fp:
        fp.write(json.dumps({'setpoints': setpoints}))


def load_settings():
    try:
        with open(config.SETPOINTS_FILE, 'r') as fp:
            payload = json.loads(fp.read())
    except Exception:
        print('[state] settings assenti, uso default')
        return False

    loaded  = payload.get('setpoints', payload) if isinstance(payload, dict) else {}
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
    alarms['ALARM_SENSORS_PANELS'] = any(temps.get(l) is None for l in ('S1', 'S2', 'S3'))
    alarms['ALARM_SENSORS_C2']     = any(temps.get(l) is None for l in ('S2', 'S3', 'S4', 'S5'))
    alarms['ALARM_SENSORS_CR']     = temps.get('S6') is None and temps.get('S7') is None
    alarms['ALARM_S4_INVALID']     = temps.get('S4') is None


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


def snapshot():
    return {
        'ts':             time.time(),
        'temps':          dict(temps),
        'c1_duty':        c1_duty,
        'c2_on':          c2_on,
        'cr_on':          cr_on,
        'p4_on':          p4_on,
        'valve_on':       valve_on,
        'relays':         dict(relay_states),
        'relay_available':dict(relay_available),
        'manual_mode':    manual_mode,
        'manual_relays':  dict(manual_relays),
        'manual_pwm_duty':manual_pwm_duty,
        'setpoints':      dict(setpoints),
        'setpoint_meta':  dict(setpoint_meta),
        'alarms':         dict(alarms),
        'c1_latch':       c1_latched_hard_stop,
        'cr_emerg':       cr_emerg_mode,
        'antileg_ok':     antileg_ok,
        'antileg_ok_ts':  antileg_ok_ts,
        'antileg_request':antileg_request,
    }