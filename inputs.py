# inputs.py - Digital input management with debounce
# HAL hardware delegata a pin_lib/plc21_io.py
# Questo modulo mantiene debounce, alias applicativi e pubblicazione stato.

import uasyncio as asyncio
import time

import config
import state


class _DebouncedInput:
    def __init__(self, name, read_cb, debounce_ms):
        self.name = name
        self.read_cb = read_cb
        self.debounce_ms = int(debounce_ms)
        self.stable_value = bool(self.read_cb())
        self.raw_value = self.stable_value
        self.last_change_ms = time.ticks_ms()

    def poll(self):
        raw = bool(self.read_cb())
        now = time.ticks_ms()

        if raw != self.raw_value:
            self.raw_value = raw
            self.last_change_ms = now
            return False

        if raw != self.stable_value and time.ticks_diff(now, self.last_change_ms) >= self.debounce_ms:
            self.stable_value = raw
            return True

        return False


class DigitalInputManager:
    def __init__(self, i2c, plc_io=None):
        self.i2c = i2c
        self.plc_io = plc_io
        self.inputs = {}

        for input_name in self._discover_hw_inputs():
            self.inputs[input_name] = _DebouncedInput(
                input_name,
                lambda n=input_name: self._read_hw_input(n),
                config.INPUT_DEBOUNCE_MS,
            )

        for alias_name, source_name in config.INPUT_ALIASES.items():
            if source_name in self.inputs:
                self.inputs[alias_name] = _DebouncedInput(
                    alias_name,
                    lambda a=alias_name, s=source_name: self._alias_value(a, self.inputs[s].stable_value),
                    config.INPUT_DEBOUNCE_MS,
                )

        for unmapped_name in getattr(config, 'UNMAPPED_INPUTS', ()):
            if unmapped_name not in self.inputs:
                self.inputs[unmapped_name] = _DebouncedInput(
                    unmapped_name,
                    lambda: False,
                    config.INPUT_DEBOUNCE_MS,
                )

        self._publish_all_initial()

    def _discover_hw_inputs(self):
        if self.plc_io is not None:
            try:
                data = self.plc_io.read_all_inputs()
                return sorted(data.keys())
            except Exception as e:
                print('[inputs] plc_io discovery fallback:', e)

        return sorted(set(config.MCP_INPUT_MAP.keys()) | set(config.DIRECT_INPUT_MAP.keys()))

    def _read_hw_input(self, input_name):
        if self.plc_io is not None:
            try:
                return bool(self.plc_io.read_input(input_name))
            except Exception as e:
                print('[inputs] read error {}: {}'.format(input_name, e))
                return False

        return False

    def _publish_all_initial(self):
        for name, inp in self.inputs.items():
            state.set_input(name, inp.stable_value)

    def _alias_value(self, alias_name, source_value):
        value = bool(source_value)
        if alias_name in getattr(config, 'INPUT_INVERTED', ()):
            return not value
        return value

    def poll(self):
        changed = {}
        for name, inp in self.inputs.items():
            if name in config.INPUT_ALIASES:
                source_name = config.INPUT_ALIASES[name]
                current = self._alias_value(name, self.inputs[source_name].stable_value)
                if current != inp.stable_value:
                    inp.stable_value = current
                    inp.raw_value = current
                    changed[name] = current
                    state.set_input(name, current)
                continue

            if inp.poll():
                changed[name] = inp.stable_value
                state.set_input(name, inp.stable_value)

        return changed

    def snapshot(self):
        return {name: inp.stable_value for name, inp in self.inputs.items()}


async def input_task(input_mgr):
    interval_ms = int(getattr(config, 'INPUT_POLL_INTERVAL_S', 1) * 1000)
    if interval_ms < 20:
        interval_ms = 20

    while True:
        changes = input_mgr.poll()
        for name, value in changes.items():
            print('[input] {}={}'.format(name, int(bool(value))))
        await asyncio.sleep_ms(interval_ms)
