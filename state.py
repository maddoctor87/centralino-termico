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
local_temps = {label: None for label in config.SENSOR_LABELS}
remote_temps = {label: None for label in config.SENSOR_LABELS}
temp_sources = {label: None for label in config.SENSOR_LABELS}
temp_remote_received_at = None
temp_remote_payload_ts = None
temp_remote_topic = None

# ── Ingressi digitali ─────────────────────────────────────────────────────────
inputs = {}

for name in getattr(config, 'MCP_INPUT_MAP', {}):
    inputs[name] = False

for name in getattr(config, 'DIRECT_INPUT_MAP', {}):
    inputs[name] = False

for name in getattr(config, 'INPUT_ALIASES', {}):
    inputs[name] = False

for name in getattr(config, 'UNMAPPED_INPUTS', ()):
    inputs[name] = False


# ── Stato attuatori ───────────────────────────────────────────────────────────
c1_wilo_duty_pct = config.C1_WILO_STANDBY_DUTY_PCT
relay_states = {name: False for name in config.RELAY_OUTPUTS}
relay_available = {name: False for name in config.RELAY_OUTPUTS}
manual_relays = {name: False for name in config.RELAY_OUTPUTS}

# Legacy keys per portale / retrocompatibilità
c2_on = False
cr_on = False
p4_on = False
p5_on = False
valve_on = False
piscina_pump_on = False
heat_pump_on = False
gas_enable_on = False
pdc_cmd_start_acr_on = False

# ── Comandi manuali ───────────────────────────────────────────────────────────
manual_mode = False
manual_c1_wilo_duty_pct = config.C1_WILO_STANDBY_DUTY_PCT
pool_just_filled = bool(getattr(config, 'POOL_JUST_FILLED', False))

# ── Setpoint ──────────────────────────────────────────────────────────────────
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

# ── Allarmi ───────────────────────────────────────────────────────────────────
alarms = {
    'ALARM_SENSORS_PANELS': False,
    'ALARM_SENSORS_C2': False,
    'ALARM_SENSORS_CR': False,
    'ALARM_S4_INVALID': False,
    'ALARM_C2_FB_MISMATCH': False,
}

# ── Stato logiche ─────────────────────────────────────────────────────────────
c1_on_state = False
c1_active = False
c1_latched_hard_stop = False
c2_on_state = False
cr_on_state = False
cr_emerg_mode = False
block2_outputs = {
    'gas_enable': False,
    'valve': False,
    'pdc_cmd_start_acr': False,
    'heat_pump': False,
    'piscina_pump': False,
}

# ── Feedback C2 NC ────────────────────────────────────────────────────────────
c2_fb_expected = None
c2_fb_last_change_ts = 0
c2_fb_alarm = False

# ── Antilegionella ────────────────────────────────────────────────────────────
antileg_request = False
antileg_ok = False
antileg_ok_ts = None
antileg_hold_start = None

# ── Snapshot ts ───────────────────────────────────────────────────────────────
last_snapshot_ts = 0


def _sync_legacy_relays():
    global c2_on, cr_on, p4_on, p5_on, valve_on
    global piscina_pump_on, heat_pump_on, gas_enable_on, pdc_cmd_start_acr_on
    c2_on = bool(relay_states.get('C2'))
    cr_on = bool(relay_states.get('CR'))
    p4_on = bool(relay_states.get('HEAT_PUMP'))
    p5_on = bool(relay_states.get('CR'))
    valve_on = bool(relay_states.get('VALVE'))
    piscina_pump_on = bool(relay_states.get('PISCINA_PUMP'))
    heat_pump_on = bool(relay_states.get('HEAT_PUMP'))
    gas_enable_on = bool(relay_states.get('GAS_ENABLE'))
    pdc_cmd_start_acr_on = bool(relay_states.get('PDC_CMD_START_ACR'))


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


# ── Temperature ───────────────────────────────────────────────────────────────

def _temp_fallback_enabled():
    return bool(getattr(config, 'MQTT_TEMP_FALLBACK_ENABLED', False))


def _temp_fallback_stale_s():
    try:
        return max(5, int(getattr(config, 'MQTT_TEMP_FALLBACK_STALE_S', 45)))
    except Exception:
        return 45


def _remote_temps_fresh():
    if not _temp_fallback_enabled():
        return False
    if temp_remote_received_at is None:
        return False
    return (time.time() - temp_remote_received_at) <= _temp_fallback_stale_s()


def _rebuild_effective_temps():
    remote_valid = _remote_temps_fresh()
    for label in temps:
        local_value = local_temps.get(label)
        remote_value = remote_temps.get(label) if remote_valid else None

        if local_value is not None:
            temps[label] = local_value
            temp_sources[label] = 'local'
        elif remote_value is not None:
            temps[label] = remote_value
            temp_sources[label] = 'mqtt'
        else:
            temps[label] = None
            temp_sources[label] = None
    refresh_sensor_alarms()


def set_temp(label, value, source='local', payload_ts=None, topic=None):
    if label not in temps:
        return

    if source == 'remote':
        global temp_remote_received_at, temp_remote_payload_ts, temp_remote_topic
        remote_temps[label] = value
        temp_remote_received_at = time.time()
        temp_remote_payload_ts = payload_ts
        temp_remote_topic = topic
    else:
        local_temps[label] = value

    _rebuild_effective_temps()


def set_all_temps(values, source='local', payload_ts=None, topic=None):
    global temp_remote_received_at, temp_remote_payload_ts, temp_remote_topic

    target = remote_temps if source == 'remote' else local_temps
    values = values if isinstance(values, dict) else {}

    for label in temps:
        target[label] = values.get(label)

    if source == 'remote':
        temp_remote_received_at = time.time()
        temp_remote_payload_ts = payload_ts
        temp_remote_topic = topic

    _rebuild_effective_temps()


def clear_remote_temps():
    global temp_remote_received_at, temp_remote_payload_ts, temp_remote_topic
    for label in remote_temps:
        remote_temps[label] = None
    temp_remote_received_at = None
    temp_remote_payload_ts = None
    temp_remote_topic = None
    _rebuild_effective_temps()


def refresh_sensor_alarms():
    alarms['ALARM_SENSORS_PANELS'] = any(temps.get(l) is None for l in ('S1', 'S2', 'S3'))
    alarms['ALARM_SENSORS_C2'] = any(temps.get(l) is None for l in ('S2', 'S3', 'S4', 'S5'))
    alarms['ALARM_SENSORS_CR'] = temps.get('S6') is None and temps.get('S7') is None
    alarms['ALARM_S4_INVALID'] = temps.get('S4') is None


# ── Ingressi ──────────────────────────────────────────────────────────────────

def set_input(name, value):
    inputs[name] = bool(value)


def set_inputs(values):
    for name, value in values.items():
        inputs[name] = bool(value)


def get_input(name, default=False):
    return bool(inputs.get(name, default))


# ── Allarmi ───────────────────────────────────────────────────────────────────

def set_alarm(name, value):
    alarms[name] = bool(value)


def get_alarm(name, default=False):
    return bool(alarms.get(name, default))


# ── Feedback C2 NC ────────────────────────────────────────────────────────────

def set_c2_fb_expected(value):
    global c2_fb_expected
    if value is None:
        c2_fb_expected = None
    else:
        c2_fb_expected = bool(value)


def get_c2_fb_expected():
    return c2_fb_expected


def set_c2_fb_last_change_ts(value):
    global c2_fb_last_change_ts
    c2_fb_last_change_ts = value


def get_c2_fb_last_change_ts():
    return c2_fb_last_change_ts


def set_c2_fb_alarm(value):
    global c2_fb_alarm
    c2_fb_alarm = bool(value)
    alarms['ALARM_C2_FB_MISMATCH'] = bool(value)


def get_c2_fb_alarm():
    return c2_fb_alarm


# ── Attuatori ─────────────────────────────────────────────────────────────────

def set_c1_wilo_duty_pct(wilo_duty_pct):
    global c1_wilo_duty_pct
    c1_wilo_duty_pct = max(0, min(100, int(wilo_duty_pct)))


def set_c1_active(value):
    global c1_active, c1_on_state
    c1_active = bool(value)
    c1_on_state = c1_active


def get_c1_active():
    return bool(c1_active)


def set_c1_latch(value):
    global c1_latched_hard_stop
    c1_latched_hard_stop = bool(value)


def get_c1_latch():
    return bool(c1_latched_hard_stop)


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


def get_manual_mode():
    return bool(manual_mode)


def set_manual_c1_wilo_duty_pct(wilo_duty_pct):
    global manual_c1_wilo_duty_pct
    manual_c1_wilo_duty_pct = max(0, min(100, int(wilo_duty_pct)))


def set_pool_just_filled(enabled):
    global pool_just_filled
    pool_just_filled = bool(enabled)


def get_pool_just_filled():
    return bool(pool_just_filled)


def set_manual_relay(name, value):
    if name not in manual_relays:
        raise KeyError(name)
    manual_relays[name] = bool(value)


def set_block2_outputs(values):
    if not isinstance(values, dict):
        return
    for key in block2_outputs:
        if key in values:
            block2_outputs[key] = bool(values[key])


def get_setpoint(name, default=None):
    return setpoints.get(name, default)


# ── Snapshot ──────────────────────────────────────────────────────────────────

def snapshot():
    _rebuild_effective_temps()
    return {
        'ts': time.time(),
        'temps': dict(temps),
        'temp_sources': dict(temp_sources),
        'inputs': dict(inputs),
        'c1_wilo_duty_pct': c1_wilo_duty_pct,
        'c1_active': c1_active,
        'c2_on': c2_on,
        'cr_on': cr_on,
        'p4_on': p4_on,
        'p5_on': p5_on,
        'valve_on': valve_on,
        'piscina_pump_on': piscina_pump_on,
        'heat_pump_on': heat_pump_on,
        'gas_enable_on': gas_enable_on,
        'pdc_cmd_start_acr_on': pdc_cmd_start_acr_on,
        'relays': dict(relay_states),
        'relay_available': dict(relay_available),
        'manual_mode': manual_mode,
        'manual_relays': dict(manual_relays),
        'manual_c1_wilo_duty_pct': manual_c1_wilo_duty_pct,
        'pool_just_filled': pool_just_filled,
        'setpoints': dict(setpoints),
        'setpoint_meta': dict(setpoint_meta),
        'alarms': dict(alarms),
        'c1_latch': c1_latched_hard_stop,
        'c2_fb_expected': c2_fb_expected,
        'c2_fb_last_change_ts': c2_fb_last_change_ts,
        'c2_fb_alarm': c2_fb_alarm,
        'cr_emerg': cr_emerg_mode,
        'block2_outputs': dict(block2_outputs),
        'antileg_ok': antileg_ok,
        'antileg_ok_ts': antileg_ok_ts,
        'antileg_request': antileg_request,
        'temp_remote_received_at': temp_remote_received_at,
        'temp_remote_payload_ts': temp_remote_payload_ts,
        'temp_remote_topic': temp_remote_topic,
    }
