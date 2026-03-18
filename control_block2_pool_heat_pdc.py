# control_block2_pool_heat_pdc.py - Block 2: GAS, Valve, PDC C2 logic
# TODO: Update parameters when server implements them. Currently using placeholders.

import time
import config
import state


class Block2Controller:
    """Controller for Block 2: GAS_ENABLE, VALVE_RELAY, PDC_CMD_START_C2, HEAT_PUMP, PISCINA_PUMP"""

    def __init__(self):
        self.gas_off_timer = 0
        self.valve_off_timer = 0
        self.pdc_cmd_hold_timer = 0
        self.c2_work_start = None  # For boost timer

    def _should_activate_gas(self, inputs):
        """Determine if GAS_ENABLE should be ON"""
        # PDC_HELP_REQUEST always enables GAS
        if inputs.get('PDC_HELP_REQUEST', False):
            return True

        # Pool or heat request with PDC C1 ON
        if inputs.get('PDC_WORK_ACS', False) and (
            inputs.get('POOL_THERMOSTAT_CALL', False) or
            inputs.get('HEAT_HELP_REQUEST', False)
        ):
            return True

        # Boost after C2 working continuously
        if (inputs.get('POOL_THERMOSTAT_CALL', False) and
            inputs.get('PDC_WORK_acr', False)):
            if self.c2_work_start is None:
                self.c2_work_start = time.time()
            elif (time.time() - self.c2_work_start) >= config.POOL_C2_GAS_BOOST_AFTER_S:
                return True
        else:
            self.c2_work_start = None

        # Pool just filled
        if config.POOL_JUST_FILLED:
            return True

        return False

    def _should_activate_valve(self, inputs):
        """Determine if VALVE_RELAY should be ON"""
        # Pool or heat request
        if (inputs.get('POOL_THERMOSTAT_CALL', False) or
            inputs.get('HEAT_HELP_REQUEST', False)):
            return True

        # Pool just filled
        if config.POOL_JUST_FILLED:
            return True

        return False

    def _should_cmd_pdc_c2(self, inputs):
        """Determine if PDC_CMD_START_C2 should be ON"""
        # Only when PDC C1 is OFF
        if inputs.get('PDC_WORK_ACS', False):
            return False

        # Pool or heat request
        if not (inputs.get('POOL_THERMOSTAT_CALL', False) or
                inputs.get('HEAT_HELP_REQUEST', False)):
            return False

        # Pool just filled always commands PDC C2
        if config.POOL_JUST_FILLED:
            return True

        # Normal case: command PDC C2
        return True

    def _should_activate_heat_pump(self, inputs):
        """Determine if HEAT_PUMP should be ON"""
        # Activate when heating requests help
        return inputs.get('HEAT_HELP_REQUEST', False)

    def _should_activate_piscina_pump(self, inputs):
        """Determine if PISCINA_PUMP should be ON"""
        # Activate when pool thermostat calls for heat
        return inputs.get('POOL_THERMOSTAT_CALL', False)

    def run_once(self, actuator_mgr, inputs):
        """Update Block 2 outputs based on inputs"""
        now = time.time()

        # Get current states
        gas_on = self._should_activate_gas(inputs)
        valve_on = self._should_activate_valve(inputs)
        pdc_cmd_on = self._should_cmd_pdc_c2(inputs)
        heat_pump_on = self._should_activate_heat_pump(inputs)
        piscina_pump_on = self._should_activate_piscina_pump(inputs)

        # Apply delays/holds
        if gas_on:
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

        # Safety: if inputs invalid, turn off
        input_valid = all(
            inputs.get(name, False) is not None
            for name in ['PDC_WORK_ACS', 'PDC_WORK_acr', 'PDC_HELP_REQUEST',
                        'POOL_THERMOSTAT_CALL', 'HEAT_HELP_REQUEST']
        )
        if not input_valid:
            gas_on = False
            valve_on = False
            pdc_cmd_on = False
            heat_pump_on = False
            piscina_pump_on = False
            # Log alarm
            print('[Block2] Input invalid, safety off')

        # Set outputs
        actuator_mgr.set_relay('GAS_ENABLE', gas_on)
        actuator_mgr.set_relay('VALVE_RELAY', valve_on)
        actuator_mgr.set_relay('PDC_CMD_START_C2', pdc_cmd_on)
        actuator_mgr.set_relay('HEAT_PUMP', heat_pump_on)
        actuator_mgr.set_relay('PISCINA_PUMP', piscina_pump_on)

        # Update state
        state.set_block2_outputs({
            'gas_enable': gas_on,
            'valve_relay': valve_on,
            'pdc_cmd_start_c2': pdc_cmd_on,
            'heat_pump': heat_pump_on,
            'piscina_pump': piscina_pump_on
        })


# Global instance
block2_controller = Block2Controller()