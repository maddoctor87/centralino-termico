# --- control_c2.py ---
# Controllo Pompa C2: trasferimento solare -> PDC.

import uasyncio as asyncio

import config
import state


def run_once(sensor_mgr, actuator_mgr):
    if state.manual_mode:
        actuator_mgr.set_relay('C2', state.manual_relays.get('C2', False))
        return

    # Sicurezza: sensori mancanti
    for label in ('S1', 'S2', 'S3', 'S4', 'S5'):
        if state.temps.get(label) is None:
            actuator_mgr.set_relay('C2', config.SAFE_RELAY_STATE)
            return

    s1 = state.temps['S1']
    s2 = state.temps['S2']
    s3 = state.temps['S3']
    s4 = state.temps['S4']
    s5 = state.temps['S5']

    # Hard stop: boiler PDC troppo caldo
    if s4 >= config.C2_HARD_STOP_TEMP:
        state.c2_on_state = False
        actuator_mgr.set_relay('C2', False)
        return

    # Delta solare = media pannelli - boiler PDC alto
    delta = ((s2 + s3) / 2.0) - s1

    # Isteresi
    if state.c2_on_state:
        new_state = delta > config.C2_DELTA_OFF
    else:
        new_state = delta > config.C2_DELTA_ON

    state.c2_on_state = new_state
    actuator_mgr.set_relay('C2', new_state)


async def control_c2_task(sensor_mgr, actuator_mgr):
    print('[C2] controllo trasferimento solare avviato')
    while True:
        try:
            run_once(sensor_mgr, actuator_mgr)
        except Exception as e:
            print('[C2] exception:', e)
            actuator_mgr.set_relay('C2', config.SAFE_RELAY_STATE)
        await asyncio.sleep(config.CONTROL_INTERVAL_S)