# inputs.py - Digital input management with debounce
# Handles MCP23008 and direct GPIO for reading digital inputs with debouncing

import time
import config
import state
from machine import Pin

class MCP23008:
    """MCP23008 8-bit I/O expander for digital inputs."""

    def __init__(self, i2c, addr=config.MCP23008_ADDR):
        self.i2c = i2c
        self.addr = addr
        self._buf1 = bytearray(1)
        self._buf2 = bytearray(2)
        # Configure all pins as inputs (IODIR = 0xFF)
        self._write_reg(0x00, 0xFF)  # IODIR
        # Enable pull-ups (GPPU = 0xFF)
        self._write_reg(0x06, 0xFF)  # GPPU

    def _write_reg(self, reg, val):
        self._buf2[0] = reg
        self._buf2[1] = val
        self.i2c.writeto(self.addr, self._buf2)

    def _read_reg(self, reg):
        self._buf1[0] = reg
        self.i2c.writeto(self.addr, self._buf1)
        self.i2c.readfrom_into(self.addr, self._buf1)
        return self._buf1[0]

    def read_pin(self, pin):
        """Read single pin (0-7). Returns True if high."""
        gpio = self._read_reg(0x09)  # GPIO
        return bool(gpio & (1 << pin))

    def read_all(self):
        """Read all pins as bitmask."""
        return self._read_reg(0x09)  # GPIO


class DigitalInputManager:
    """Manages digital inputs with debouncing."""

    def __init__(self, i2c):
        self.mcp = None
        # Direct GPIO pins for I0.5 and I0.6
        self.gpio27 = Pin(27, Pin.IN, Pin.PULL_UP)  # I0.5
        self.gpio26 = Pin(26, Pin.IN, Pin.PULL_UP)  # I0.6
        self.values = {name: False for name in config.INPUT_PINS}
        self._last_values = {name: False for name in config.INPUT_PINS}
        self._debounce_times = {name: 0 for name in config.INPUT_PINS}

        try:
            self.mcp = MCP23008(i2c, config.MCP23008_ADDR)
            print('[inputs] MCP23008 ok @ 0x{:02X}'.format(config.MCP23008_ADDR))
        except Exception as e:
            print('[inputs] MCP23008 non disponibile:', e)

    def read_all(self):
        """Read all inputs with debouncing."""
        if self.mcp is None:
            return dict(self.values)

        current_time = time.ticks_ms()

        for name, pin in config.INPUT_PINS.items():
            if pin is not None:
                try:
                    if name == "POOL_THERMOSTAT_CALL":
                        raw_value = self.gpio27.value() == 1  # Assuming active high
                    elif name == "HEAT_HELP_REQUEST":
                        raw_value = self.gpio26.value() == 1  # Assuming active high
                    else:
                        raw_value = self.mcp.read_pin(pin)
                except Exception as e:
                    print('[inputs] read {} error: {}'.format(name, e))
                    raw_value = False

                # Debounce logic
                if raw_value != self._last_values[name]:
                    self._debounce_times[name] = current_time
                    self._last_values[name] = raw_value
                elif time.ticks_diff(current_time, self._debounce_times[name]) >= config.INPUT_DEBOUNCE_MS:
                    if raw_value != self.values[name]:
                        self.values[name] = raw_value

        # Update global state
        return dict(self.values)

    def snapshot(self):
        return dict(self.values)