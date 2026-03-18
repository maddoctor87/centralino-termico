# --- control_aux.py ---
# Controllo uscite ausiliarie non gestite da logiche dedicate.
# Logica automatica TODO: per ora solo modalità manuale o safe state.

import uasyncio as asyncio

import config
import state

_AUX_RELAYS = ('PISCINA_PUMP', 'HEAT_PUMP', 'VALVE', 'GAS_ENABLE', 'PDC_CMD_START_ACR')


def run_once(actuator_mgr):
    for name in _AUX_RELAYS:
        value = state.manual_relays.get(name, False) if state.manual_mode else config.SAFE_RELAY_STATE
        actuator_mgr.set_relay(name, value)


async def control_aux_task(actuator_mgr):
    print('[AUX] skeleton attivo, logica automatica TODO')
    while True:
        try:
            run_once(actuator_mgr)
        except Exception as e:
            print('[AUX] exception:', e)
            for name in _AUX_RELAYS:
                actuator_mgr.set_relay(name, config.SAFE_RELAY_STATE)
        await asyncio.sleep(config.CONTROL_INTERVAL_S)
