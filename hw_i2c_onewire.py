# hw_i2c_onewire.py - DS2482 1-Wire master driver for MicroPython
# Handles I2C communication with DS2482 and 1-Wire bus operations

import time
import machine
from micropython import const

# DS2482 I2C Registers
DS2482_STATUS_REG = const(0xF0)
DS2482_READ_DATA_REG = const(0xE1)
DS2482_CONFIG_REG = const(0xC3)

# DS2482 Commands
DS2482_CMD_RESET = const(0xF0)
DS2482_CMD_SET_READ_PTR = const(0xE1)
DS2482_CMD_WRITE_CONFIG = const(0xD2)
DS2482_CMD_1WIRE_RESET = const(0xB4)
DS2482_CMD_1WIRE_SINGLE_BIT = const(0x87)
DS2482_CMD_1WIRE_WRITE_BYTE = const(0xA5)
DS2482_CMD_1WIRE_READ_BYTE = const(0x96)
DS2482_CMD_1WIRE_TRIPLET = const(0x78)

# Status register bits
STATUS_1WB = const(0x01)  # 1-Wire Busy
STATUS_PPD = const(0x02)  # Presence Pulse Detect
STATUS_SD = const(0x04)   # Short Detected
STATUS_LL = const(0x08)   # Logic Level
STATUS_RST = const(0x10)  # Device Reset
STATUS_SBR = const(0x20)  # Single Bit Result
STATUS_TSB = const(0x40)  # Triplet Second Bit
STATUS_DIR = const(0x80)  # Branch Direction Taken

# Configuration register
CONFIG_APU = const(0x01)  # Active Pullup
CONFIG_PPM = const(0x02)  # Presence Pulse Masking
CONFIG_SPU = const(0x04)  # Strong Pullup
CONFIG_1WS = const(0x08)  # 1-Wire Speed

class DS2482:
    """DS2482 1-Wire master driver."""

    def __init__(self, i2c, addr=0x18):
        self.i2c = i2c
        self.addr = addr
        self._reset()

    def _reset(self):
        """Reset the DS2482 device."""
        try:
            self.i2c.writeto(self.addr, bytes([DS2482_CMD_RESET]))
            time.sleep_ms(1)
            # Configure: APU=1, PPM=0, SPU=0, 1WS=0 (standard speed)
            config = CONFIG_APU
            self.i2c.writeto(self.addr, bytes([DS2482_CMD_WRITE_CONFIG, config | (~config << 4)]))
            time.sleep_ms(1)
        except Exception as e:
            print(f"DS2482 reset error: {e}")

    def _wait_idle(self, timeout_ms=100):
        """Wait for 1-Wire bus to be idle."""
        start = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
            status = self._read_status()
            if not (status & STATUS_1WB):
                return True
            time.sleep_ms(1)
        return False

    def _read_status(self):
        """Read status register."""
        self.i2c.writeto(self.addr, bytes([DS2482_CMD_SET_READ_PTR, DS2482_STATUS_REG]))
        data = self.i2c.readfrom(self.addr, 1)
        return data[0]

    def _read_data(self):
        """Read data register."""
        self.i2c.writeto(self.addr, bytes([DS2482_CMD_SET_READ_PTR, DS2482_READ_DATA_REG]))
        data = self.i2c.readfrom(self.addr, 1)
        return data[0]

    def onewire_reset(self):
        """Perform 1-Wire reset and check for presence."""
        if not self._wait_idle():
            return False
        self.i2c.writeto(self.addr, bytes([DS2482_CMD_1WIRE_RESET]))
        if not self._wait_idle():
            return False
        status = self._read_status()
        return bool(status & STATUS_PPD)

    def onewire_write_byte(self, byte):
        """Write a byte to 1-Wire bus."""
        if not self._wait_idle():
            return False
        self.i2c.writeto(self.addr, bytes([DS2482_CMD_1WIRE_WRITE_BYTE, byte]))
        return self._wait_idle()

    def onewire_read_byte(self):
        """Read a byte from 1-Wire bus."""
        if not self._wait_idle():
            return None
        self.i2c.writeto(self.addr, bytes([DS2482_CMD_1WIRE_READ_BYTE]))
        if not self._wait_idle():
            return None
        return self._read_data()

    def onewire_write_bit(self, bit):
        """Write a single bit to 1-Wire bus."""
        if not self._wait_idle():
            return False
        cmd = DS2482_CMD_1WIRE_SINGLE_BIT | (bit << 7)
        self.i2c.writeto(self.addr, bytes([cmd]))
        return self._wait_idle()

    def onewire_read_bit(self):
        """Read a single bit from 1-Wire bus."""
        if not self._wait_idle():
            return None
        self.i2c.writeto(self.addr, bytes([DS2482_CMD_1WIRE_SINGLE_BIT | 0x80]))
        if not self._wait_idle():
            return None
        status = self._read_status()
        return bool(status & STATUS_SBR)

class OneWireBus:
    """1-Wire bus interface using DS2482."""

    def __init__(self, ds2482):
        self.ds2482 = ds2482

    def reset(self):
        """Reset 1-Wire bus."""
        return self.ds2482.onewire_reset()

    def write_byte(self, byte):
        """Write byte to bus."""
        return self.ds2482.onewire_write_byte(byte)

    def read_byte(self):
        """Read byte from bus."""
        return self.ds2482.onewire_read_byte()

    def write_bit(self, bit):
        """Write bit to bus."""
        return self.ds2482.onewire_write_bit(bit)

    def read_bit(self):
        """Read bit from bus."""
        return self.ds2482.onewire_read_bit()

    def select_rom(self, rom):
        """Select a specific device ROM."""
        self.write_byte(0x55)  # MATCH ROM
        for byte in rom:
            self.write_byte(byte)

    def skip_rom(self):
        """Skip ROM (for single device bus)."""
        self.write_byte(0xCC)  # SKIP ROM

    def search_rom(self):
        """Search for devices on bus (basic implementation)."""
        # TODO: Implement full search algorithm
        return []

# DS18B20 Commands
DS18B20_CONVERT_T = const(0x44)
DS18B20_READ_SCRATCHPAD = const(0xBE)
DS18B20_WRITE_SCRATCHPAD = const(0x4E)
DS18B20_COPY_SCRATCHPAD = const(0x48)
DS18B20_RECALL_E2 = const(0xB8)
DS18B20_READ_POWER_SUPPLY = const(0xB4)

class DS18B20:
    """DS18B20 temperature sensor driver."""

    def __init__(self, onewire, rom):
        self.onewire = onewire
        self.rom = rom

    def convert_temp(self):
        """Start temperature conversion."""
        if not self.onewire.reset():
            return False
        self.onewire.select_rom(self.rom)
        self.onewire.write_byte(DS18B20_CONVERT_T)
        return True

    def read_temp(self):
        """Read temperature from scratchpad."""
        if not self.onewire.reset():
            return None
        self.onewire.select_rom(self.rom)
        self.onewire.write_byte(DS18B20_READ_SCRATCHPAD)

        data = []
        for _ in range(9):
            byte = self.onewire.read_byte()
            if byte is None:
                return None
            data.append(byte)

        # Convert to temperature
        temp_raw = (data[1] << 8) | data[0]
        if temp_raw & 0x8000:  # Negative
            temp_raw = -((temp_raw ^ 0xFFFF) + 1)
        temp_c = temp_raw / 16.0
        return temp_c