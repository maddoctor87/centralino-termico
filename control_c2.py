# --- control_c2.py ---
# Controllo Pompa 2 / C2: trasferimento solare -> PDC.

import uasyncio as asyncio

import config
import state


def run_once(sensor_mgr, actuator_mgr):
    if state.manual_mode:
        actuator_mgr.set_relay('C2', state.manual_relays.get('C2', False))
        return

    # Subito in sicurezza se mancano sensori importanti
    for label in ('S1', 'S2', 'S3', 'S4', 'S5'):
        if state.temps.get(label) is None:
            actuator_mgr.set_relay('C2', config.SAFE_RELAY_STATE)
            return

    s1 = state.temps['S1']
    s2 = state.temps['S2']
    s3 = state.temps['S3']
    s4 = state.temps['S4']
    s5 = state.temps['S5']

    # Hard stop PDC
    if s4 >= config.C1_HARD_STOP_TEMP:
        actuator_mgr.set_relay('C2', config.SAFE_RELAY_STATE)
        return

    # Calcolo del delta solare vs PDC
    delta_solare = ((s2 + s3) / 2.0) - s1
    delta_pdc = s4 - s5

    on_thresh = delta_pdc + config.C2_DELTA_SOLAR_MIN
    off_thresh = delta_pdc + config.C2_DELTA_SOLAR_MAX

    # Hysteresis
    if state.c2_on_state:
        new_state = delta_solare > off_thresh
    else:
        new_state = delta_solare > on_thresh

    actuator_mgr.set_relay('C2', new_state)


async def control_c2_task(sensor_mgr, actuator_mgr):
    print('[C2] controllo trasferimento solare avviato')
    while True:
        try:
            run_once(sensor_mgr, actuator_mgr)
        except Exception as e:
            print('[C2] exception:', e)
            actuator_mgr.set_relay('C2', config.SAFE_RELAY_STATE)
        await asyncio.sleep(config.CONTROL_INTERVAL_MS / 1000)
