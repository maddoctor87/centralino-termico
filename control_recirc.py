# --- control_recirc.py ---
# Controllo ricircolo CR + ciclo antilegionella.

import uasyncio as asyncio
import time

import config
import state


def _hysteresis(current, value, on_thresh, off_thresh):
    if current:
        return value < off_thresh   # spegni sotto off_thresh
    return value >= on_thresh       # accendi sopra on_thresh


def _average_defined(values):
    valid = [value for value in values if value is not None]
    if not valid:
        return None
    return sum(valid) / len(valid)


def run_once(sensor_mgr, actuator_mgr):
    if state.manual_mode:
        actuator_mgr.set_relay('CR', state.manual_relays.get('CR', False))
        return

    # Sensori collettore: almeno uno valido
    s6 = state.temps.get('S6')
    s7 = state.temps.get('S7')
    valid = [t for t in (s6, s7) if t is not None]
    if not valid:
        actuator_mgr.set_relay('CR', config.SAFE_RELAY_STATE)
        return

    tcol = min(valid)
    s4 = state.temps.get('S4')
    s5 = state.temps.get('S5')
    tpdc = _average_defined((s4, s5))

    # Emergenza: S4 alta o richiesta antilegionella via MQTT
    emerg = (s4 is not None and s4 >= config.CR_EMERG_TEMP) or state.antileg_request
    state.cr_emerg_mode = emerg

    # Gestione timer antilegionella
    if state.antileg_request:
        if tcol >= config.CR_TARGET_EMERG:
            if state.antileg_hold_start is None:
                state.antileg_hold_start = time.time()
            elapsed = time.time() - state.antileg_hold_start
            if elapsed >= config.ANTILEGIONELLA_OK_SECONDS:
                if not state.antileg_ok:
                    print('[CR] antilegionella OK ({}s)'.format(int(elapsed)))
                state.antileg_ok    = True
                state.antileg_ok_ts = time.time()
            else:
                state.antileg_ok = False
        else:
            state.antileg_hold_start = None
            state.antileg_ok         = False
    else:
        state.antileg_hold_start = None
        state.antileg_ok         = False

    # Soglie isteresi in base alla modalità
    if emerg:
        on_thresh  = config.CR_TARGET_EMERG  - config.CR_HYSTERESIS_EMERG
        off_thresh = config.CR_TARGET_EMERG
    else:
        if tpdc is None or tpdc < config.CR_ENABLE_MIN_PDC_TEMP:
            state.cr_on_state = False
            actuator_mgr.set_relay('CR', False)
            return
        on_thresh  = config.CR_TARGET_NORMAL - config.CR_HYSTERESIS_NORMAL
        off_thresh = config.CR_TARGET_NORMAL

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
