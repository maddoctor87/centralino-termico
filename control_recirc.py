# --- control_recirc.py ---
# Controllo ricircolo CR + ciclo antilegionella.

import uasyncio as asyncio
import time

import config
import state


def _hysteresis(current, value, on_thresh, off_thresh):
    if current:
        return value < off_thresh   # resta acceso finche' non raggiunge il target
    return value <= on_thresh       # accendi quando scende sotto la soglia bassa


def _average_defined(values):
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def _pdc_enable_ok(current, tpdc):
    if tpdc is None:
        return False

    on_thresh = config.CR_ENABLE_MIN_PDC_TEMP
    off_thresh = config.CR_ENABLE_MIN_PDC_TEMP - config.CR_ENABLE_HYSTERESIS_PDC

    if current:
        return tpdc >= off_thresh
    return tpdc >= on_thresh


def _get_antileg_target_c():
    getter = getattr(state, "get_setpoint", None)
    if callable(getter):
        value = getter("antileg_target_c", None)
        if value is not None:
            return float(value)
    return float(config.SETPOINTS["antileg_target_c"]["default"])


def _get_recirc_target_c():
    getter = getattr(state, "get_setpoint", None)
    if callable(getter):
        value = getter("recirc_target_c", None)
        if value is not None:
            return float(value)
    return float(config.SETPOINTS["recirc_target_c"]["default"])


def _solar_boiler_high_temp():
    s2 = state.temps.get('S2')
    s3 = state.temps.get('S3')
    valid = [temp for temp in (s2, s3) if temp is not None]
    if not valid:
        return None
    return max(valid)


def _pdc_boiler_ready_for_antileg_start(target_c):
    s4 = state.temps.get('S4')
    s5 = state.temps.get('S5')
    if s4 is None or s5 is None:
        return False
    return s4 >= target_c and s5 >= config.ANTILEGIONELLA_BOTTOM_READY_C


def _pdc_boiler_ready_for_antileg_hold(target_c):
    s4 = state.temps.get('S4')
    s5 = state.temps.get('S5')
    if s4 is None or s5 is None:
        return False
    return s4 >= target_c and s5 >= config.ANTILEGIONELLA_BOTTOM_READY_C


def _pause_antileg_timer():
    if state.antileg_hold_start is not None:
        state.antileg_hold_elapsed_s += max(0, time.time() - state.antileg_hold_start)
        state.antileg_hold_start = None


def _resume_antileg_timer():
    if state.antileg_hold_start is None:
        state.antileg_hold_start = time.time()


def _reset_antileg_timer():
    state.antileg_hold_start = None
    state.antileg_hold_elapsed_s = 0


def run_once(sensor_mgr, actuator_mgr):
    if state.manual_mode:
        actuator_mgr.set_relay('CR', state.manual_relays.get('CR', False))
        return

    s6 = state.temps.get('S6')
    s7 = state.temps.get('S7')
    valid = [t for t in (s6, s7) if t is not None]
    tcol = min(valid) if valid else None
    s4 = state.temps.get('S4')
    s5 = state.temps.get('S5')
    tpdc = _average_defined((s4, s5))

    antileg_mode = bool(state.antileg_request)

    solar_boiler_high = _solar_boiler_high_temp()

    # Emergenza: boiler solare alto o richiesta antilegionella via MQTT
    emerg = (solar_boiler_high is not None and solar_boiler_high >= config.CR_EMERG_TEMP) or antileg_mode
    state.cr_emerg_mode = emerg

    # Gestione timer antilegionella:
    # 1) porta il boiler PDC alla soglia antileg: S4>=target e S5>=68 C
    # 2) attiva CR
    # 3) mantiene il boiler PDC sopra la stessa soglia per la finestra richiesta
    if antileg_mode:
        target_c = _get_antileg_target_c()
        start_ready = _pdc_boiler_ready_for_antileg_start(target_c)
        hold_ready = _pdc_boiler_ready_for_antileg_hold(target_c)

        if start_ready and state.antileg_phase in ('heat_boiler', 'pause_recirc'):
            state.antileg_phase = 'hold_recirc'
            _resume_antileg_timer()

        if state.antileg_phase == 'hold_recirc' and not hold_ready:
            _pause_antileg_timer()
            state.antileg_phase = 'pause_recirc'

        if state.antileg_phase == 'hold_recirc':
            _resume_antileg_timer()
            elapsed = state.antileg_hold_elapsed_s + max(0, time.time() - state.antileg_hold_start)
            if elapsed >= config.ANTILEGIONELLA_OK_SECONDS:
                if not state.antileg_ok:
                    print('[CR] antilegionella OK ({}s)'.format(int(elapsed)))
                state.antileg_ok = True
                state.antileg_ok_ts = time.time()
                state.antileg_request = False
                _reset_antileg_timer()
                state.antileg_phase = 'idle'
                antileg_mode = False
                emerg = solar_boiler_high is not None and solar_boiler_high >= config.CR_EMERG_TEMP
                state.cr_emerg_mode = emerg
            else:
                state.antileg_ok = False
        else:
            if state.antileg_phase not in ('pause_recirc', 'hold_recirc'):
                state.antileg_phase = 'heat_boiler'
            state.antileg_ok = False
    else:
        _reset_antileg_timer()
        state.antileg_phase = 'idle'

    if antileg_mode:
        if state.antileg_phase in ('heat_boiler', 'pause_recirc'):
            state.cr_on_state = False
            actuator_mgr.set_relay('CR', False)
            return

        state.cr_on_state = True
        actuator_mgr.set_relay('CR', True)
        return

    if not valid:
        actuator_mgr.set_relay('CR', config.SAFE_RELAY_STATE)
        return

    # Soglie isteresi in base alla modalità
    if emerg:
        target_c = _get_antileg_target_c() if antileg_mode else config.CR_TARGET_EMERG
        on_thresh = target_c - config.CR_HYSTERESIS_EMERG
        off_thresh = target_c
    else:
        if not _pdc_enable_ok(state.cr_on_state, tpdc):
            state.cr_on_state = False
            actuator_mgr.set_relay('CR', False)
            return
        target_c = _get_recirc_target_c()
        on_thresh  = target_c - config.CR_HYSTERESIS_NORMAL
        off_thresh = target_c

    new_state = _hysteresis(state.cr_on_state, tcol, on_thresh, off_thresh)
    state.cr_on_state = new_state
    actuator_mgr.set_relay('CR', new_state)


async def control_recirc_task(sensor_mgr, actuator_mgr):
    print('[CR] controllo ricircolo avviato')
    while True:
        try:
            run_once(sensor_mgr, actuator_mgr)
        except Exception as e:
            print('[CR] exception:', e)
            actuator_mgr.set_relay('CR', config.SAFE_RELAY_STATE)
        await asyncio.sleep(config.CONTROL_INTERVAL_S)
