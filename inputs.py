# inputs.py - Digital input management with debounce
# Supporto verificato:
# - MCP23008 @ 0x21 per I0.0-I0.4
# - GPIO27 -> I0.5
# - GPIO26 -> I0.6
#
# NON implementa ancora I0.7-I0.12 finché il reverse engineering non è concluso.

import uasyncio as asyncio
import time
from machine import Pin

import config
import state


class MCP23008:
    """MCP23008 8-bit I/O expander for digital inputs."""

    REG_IODIR = 0x00
    REG_GPPU = 0x06
    REG_GPIO = 0x09

    def __init__(self, i2c, addr):
        self.i2c = i2c
        self.addr = addr
        # tutti input
        self._write_reg(self.REG_IODIR, 0xFF)
        # pull-up interni attivi
        self._write_reg(self.REG_GPPU, 0xFF)

    def _write_reg(self, reg, val):
        self.i2c.writeto_mem(self.addr, reg, bytes([val & 0xFF]))

    def _read_reg(self, reg):
        return self.i2c.readfrom_mem(self.addr, reg, 1)[0]

    def read_all(self):
        """Ritorna il byte GPIO completo."""
        return self._read_reg(self.REG_GPIO)

    def read_pin(self, pin):
        """Ritorna True/False logico del pin MCP indicato."""
        if pin < 0 or pin > 7:
            raise ValueError("MCP23008 pin fuori range: {}".format(pin))
        gpio = self.read_all()
        return bool((gpio >> pin) & 0x01)


class _DebouncedInput:
    def __init__(self, name, read_cb, debounce_ms):
        self.name = name
        self._read_cb = read_cb
        self.debounce_ms = debounce_ms

        now = time.ticks_ms()
        self.raw_value = bool(self._read_cb())
        self.stable_value = self.raw_value
        self.last_raw_value = self.raw_value
        self.last_change_ms = now

    def poll(self):
        now = time.ticks_ms()
        raw = bool(self._read_cb())

        if raw != self.last_raw_value:
            self.last_raw_value = raw
            self.last_change_ms = now

        if raw != self.stable_value:
            if time.ticks_diff(now, self.last_change_ms) >= self.debounce_ms:
                self.raw_value = raw
                self.stable_value = raw
                return True

        self.raw_value = raw
        return False


class DigitalInputManager:
    def __init__(self, i2c):
        self.i2c = i2c
        self.mcp = None
        self.inputs = {}
        self._pin_objs = {}

        # GPIO diretti verificati
        for input_name, gpio_num in config.DIRECT_INPUT_MAP.items():
            self._pin_objs[input_name] = Pin(gpio_num, Pin.IN, Pin.PULL_UP)

        # MCP verificato per I0.0-I0.4
        try:
            self.mcp = MCP23008(i2c, config.MCP23008_ADDR)
            print('[inputs] MCP23008 ok @ 0x{:02X}'.format(config.MCP23008_ADDR))
        except Exception as e:
            self.mcp = None
            print('[inputs] MCP23008 non disponibile:', e)

        # Ingressi MCP verificati
        for input_name, mcp_pin in config.MCP_INPUT_MAP.items():
            self.inputs[input_name] = _DebouncedInput(
                input_name,
                lambda p=mcp_pin: self._read_mcp_pin(p),
                config.INPUT_DEBOUNCE_MS,
            )

        # Ingressi diretti verificati
        for input_name in config.DIRECT_INPUT_MAP:
            self.inputs[input_name] = _DebouncedInput(
                input_name,
                lambda n=input_name: self._read_direct_input(n),
                config.INPUT_DEBOUNCE_MS,
            )

        # Alias applicativi sicuri
        for alias_name, source_name in config.INPUT_ALIASES.items():
            if source_name in self.inputs:
                self.inputs[alias_name] = _DebouncedInput(
                    alias_name,
                    lambda s=source_name: self.inputs[s].stable_value,
                    config.INPUT_DEBOUNCE_MS,
                )

        # Ingressi non ancora mappati: presenti nello stato ma sempre False
        for unmapped_name in getattr(config, 'UNMAPPED_INPUTS', ()):
            if unmapped_name not in self.inputs:
                self.inputs[unmapped_name] = _DebouncedInput(
                    unmapped_name,
                    lambda: False,
                    config.INPUT_DEBOUNCE_MS,
                )

        # Pubblica stato iniziale
        self._publish_all_initial()

    def _read_mcp_pin(self, pin):
        if self.mcp is None:
            return False
        # ingressi isolati spesso risultano attivi-bassi con pull-up
        return not self.mcp.read_pin(pin)

    def _read_direct_input(self, input_name):
        pin = self._pin_objs[input_name]
        # anche i diretti li trattiamo attivi-bassi finché il cablaggio reale non dice il contrario
        return not bool(pin.value())

    def _publish_all_initial(self):
        for name, inp in self.inputs.items():
            state.set_input(name, inp.stable_value)

    def poll(self):
        changed = {}
        for name, inp in self.inputs.items():
            if name in config.INPUT_ALIASES:
                # gli alias seguono la sorgente, non leggono direttamente
                source_name = config.INPUT_ALIASES[name]
                current = self.inputs[source_name].stable_value
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