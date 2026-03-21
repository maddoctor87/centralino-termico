# control_block2_pool_heat_pdc.py - Block 2: GAS, EVIE valve, avvio lavoro ACR

import time
import uasyncio as asyncio

import config
import state


class Block2Controller:
    """Controller for Block 2: GAS_ENABLE, VALVE, PDC_CMD_START_ACR, HEAT_PUMP, PISCINA_PUMP"""

    def __init__(self):
        self.gas_off_timer = 0
        self.valve_off_timer = 0
        self.pdc_cmd_hold_timer = 0
        self.c2_work_start = None

    def _pool_just_filled_active(self):
        getter = getattr(state, 'get_pool_just_filled', None)
        if callable(getter):
            return bool(getter())
        return bool(getattr(config, 'POOL_JUST_FILLED', False))

    def _required_inputs_present(self, inputs):
        required = (
            'PDC_WORK_ACS',
            'PDC_WORK_ACR',
            'PDC_HELP_REQUEST',
            'POOL_THERMOSTAT_CALL',
            'HEAT_HELP_REQUEST',
        )
        return all(name in inputs for name in required)

    def _publish_outputs(self, outputs):
        state.set_block2_outputs(outputs)

    def _solar_can_help_pdc(self):
        s2 = state.temps.get('S2')
        s3 = state.temps.get('S3')
        s5 = state.temps.get('S5')
        if s2 is None or s3 is None or s5 is None:
            return False
        tsolare = (s2 + s3) / 2.0
        return tsolare > s5

    def _prefer_c2_over_gas(self, inputs):
        return inputs.get('PDC_HELP_REQUEST', False) and self._solar_can_help_pdc()

    def _set_manual_outputs(self, actuator_mgr):
        outputs = {
            'gas_enable': bool(state.manual_relays.get('GAS_ENABLE', False)),
            'valve': bool(state.manual_relays.get('VALVE', False)),
            'pdc_cmd_start_acr': bool(state.manual_relays.get('PDC_CMD_START_ACR', False)),
            'heat_pump': bool(state.manual_relays.get('HEAT_PUMP', False)),
            'piscina_pump': bool(state.manual_relays.get('PISCINA_PUMP', False)),
        }
        actuator_mgr.set_relay('GAS_ENABLE', outputs['gas_enable'])
        actuator_mgr.set_relay('VALVE', outputs['valve'])
        actuator_mgr.set_relay('PDC_CMD_START_ACR', outputs['pdc_cmd_start_acr'])
        actuator_mgr.set_relay('HEAT_PUMP', outputs['heat_pump'])
        actuator_mgr.set_relay('PISCINA_PUMP', outputs['piscina_pump'])
        self._publish_outputs(outputs)

    def _should_activate_gas(self, inputs):
        if inputs.get('PDC_HELP_REQUEST', False):
            if self._prefer_c2_over_gas(inputs):
                return False
            return True

        if inputs.get('PDC_WORK_ACS', False) and (
            inputs.get('POOL_THERMOSTAT_CALL', False) or
            inputs.get('HEAT_HELP_REQUEST', False)
        ):
            return True

        if (inputs.get('POOL_THERMOSTAT_CALL', False) and
            inputs.get('PDC_WORK_ACR', False)):
            if self.c2_work_start is None:
                self.c2_work_start = time.time()
            elif (time.time() - self.c2_work_start) >= config.POOL_C2_GAS_BOOST_AFTER_S:
                return True
        else:
            self.c2_work_start = None

        if self._pool_just_filled_active():
            return True

        return False

    def _should_activate_valve(self, inputs):
        if (inputs.get('POOL_THERMOSTAT_CALL', False) or
            inputs.get('HEAT_HELP_REQUEST', False)):
            return True

        if self._pool_just_filled_active():
            return True

        return False

    def _should_cmd_pdc_c2(self, inputs):
        if inputs.get('PDC_WORK_ACS', False):
            return False

        if not (
            inputs.get('POOL_THERMOSTAT_CALL', False) or
            inputs.get('HEAT_HELP_REQUEST', False) or
            self._pool_just_filled_active()
        ):
            return False

        return True

    def _should_activate_heat_pump(self, inputs):
        return inputs.get('HEAT_HELP_REQUEST', False)

    def _should_activate_piscina_pump(self, inputs):
        return inputs.get('POOL_THERMOSTAT_CALL', False) or self._pool_just_filled_active()

    def run_once(self, actuator_mgr, inputs):
        if state.manual_mode:
            self._set_manual_outputs(actuator_mgr)
            return

        now = time.time()
        prefer_c2_help = self._prefer_c2_over_gas(inputs)

        gas_on = self._should_activate_gas(inputs)
        valve_on = self._should_activate_valve(inputs)
        pdc_cmd_on = self._should_cmd_pdc_c2(inputs)
        heat_pump_on = self._should_activate_heat_pump(inputs)
        piscina_pump_on = self._should_activate_piscina_pump(inputs)

        if prefer_c2_help:
            self.gas_off_timer = 0
            gas_on = False
        elif gas_on:
            self.gas_off_timer = now + config.GAS_OFF_DELAY_S
        elif now < self.gas_off_timer:
            gas_on = True

        if valve_on:
            self.valve_off_timer = now + config.VALVE_OFF_DELAY_S
        elif now < self.valve_off_timer:
            valve_on = True

        if pdc_cmd_on:
            self.pdc_cmd_hold_timer = now + config.PDC_C2_CMD_HOLD_S
        elif now < self.pdc_cmd_hold_timer:
            pdc_cmd_on = True

        if not self._required_inputs_present(inputs):
            gas_on = False
            valve_on = False
            pdc_cmd_on = False
            heat_pump_on = False
            piscina_pump_on = False
            print('[Block2] ingressi mancanti, safety off')

        actuator_mgr.set_relay('GAS_ENABLE', gas_on)
        actuator_mgr.set_relay('VALVE', valve_on)
        actuator_mgr.set_relay('PDC_CMD_START_ACR', pdc_cmd_on)
        actuator_mgr.set_relay('HEAT_PUMP', heat_pump_on)
        actuator_mgr.set_relay('PISCINA_PUMP', piscina_pump_on)

        self._publish_outputs({
            'gas_enable': gas_on,
            'valve': valve_on,
            'pdc_cmd_start_acr': pdc_cmd_on,
            'heat_pump': heat_pump_on,
            'piscina_pump': piscina_pump_on,
        })


block2_controller = Block2Controller()


def _get_input_snapshot(input_mgr):
    if input_mgr is None:
        return {}
    try:
        return input_mgr.snapshot()
    except Exception:
        return {}


async def control_block2_task(actuator_mgr, input_mgr=None):
    print('[Block2] logica piscina/riscaldamento attiva')
    while True:
        try:
            block2_controller.run_once(actuator_mgr, _get_input_snapshot(input_mgr))
        except Exception as e:
            print('[Block2] exception:', e)
            for name in ('GAS_ENABLE', 'VALVE', 'PDC_CMD_START_ACR', 'HEAT_PUMP', 'PISCINA_PUMP'):
                actuator_mgr.set_relay(name, config.SAFE_RELAY_STATE)
            state.set_block2_outputs({
                'gas_enable': False,
                'valve': False,
                'pdc_cmd_start_acr': False,
                'heat_pump': False,
                'piscina_pump': False,
            })
        await asyncio.sleep(config.CONTROL_INTERVAL_S)
