# --- control_recirc.py ---
# Controllo ricircolo / CR + ciclo antilegionella.

import uasyncio as asyncio
import time

import config
import state


def _hysteresis_state(current, value, on_thresh, off_thresh):
    if current:
        return value >= off_thresh
    return value > on_thresh


def run_once(sensor_mgr, actuator_mgr):
    if state.manual_mode:
        actuator_mgr.set_relay('CR', state.manual_relays.get('CR', False))
        return

    # Se mancano sensori, spegni per sicurezza.
    if state.temps.get('S6') is None and state.temps.get('S7') is None:
        actuator_mgr.set_relay('CR', config.SAFE_RELAY_STATE)
        return

    # Antilegionella: priorità, basata su S4 o richiesta via MQTT.
    s4 = state.temps.get('S4')
    tcol = min(filter(lambda x: x is not None, (state.temps.get('S6'), state.temps.get('S7'))))

    emerg = False
    if s4 is not None and s4 >= config.CR_EMERG_ON_TEMP:
        emerg = True

    if state.antileg_request:
        emerg = True

    # Gestione timer antilegionella
    if state.antileg_request:
        if tcol >= config.CR_EMERG_OFF_TEMP:
            if state.antileg_hold_start is None:
                state.antileg_hold_start = time.time()
            elapsed = time.time() - state.antileg_hold_start
            if elapsed >= config.CR_ANTILEG_DURATION_S:
                if not state.antileg_ok:
                    print('[CR] antilegionella OK ({}s)'.format(elapsed))
                state.antileg_ok = True
                state.antileg_ok_ts = time.time()
            else:
                state.antileg_ok = False
        else:
            state.antileg_hold_start = None
            state.antileg_ok = False
    else:
        state.antileg_hold_start = None
        state.antileg_ok = False

    # Determina target e hysteresis
    if emerg:
        target = config.CR_EMERG_OFF_TEMP
        hyst = config.CR_HYSTERESIS_OFF
        state.cr_emerg_mode = True
    else:
        target = config.CR_NORMAL_OFF_TEMP
        hyst = config.CR_HYSTERESIS_OFF
        state.cr_emerg_mode = False

    on_thresh = target - hyst
    off_thresh = target

    new_state = _hysteresis_state(state.cr_on_state, tcol, on_thresh, off_thresh)
    actuator_mgr.set_relay('CR', new_state)


async def control_recirc_task(sensor_mgr, actuator_mgr):
    print('[CR] controllo ricircolo avviato')
    while True:
        try:
            run_once(sensor_mgr, actuator_mgr)
        except Exception as e:
            print('[CR] exception:', e)
            actuator_mgr.set_relay('CR', config.SAFE_RELAY_STATE)
        await asyncio.sleep(config.CONTROL_INTERVAL_MS / 1000)
