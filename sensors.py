# --- sensors.py ---
# Lettura sensori temperatura DS18B20 via DS2482 (1-Wire su I2C).

import uasyncio as asyncio
from micropython import const
import time

import config
import state

_CMD_DRST  = const(0xF0)
_CMD_SRP   = const(0xE1)
_CMD_WCFG  = const(0xD2)
_CMD_1WRS  = const(0xB4)
_CMD_1WWB  = const(0xA5)
_CMD_1WRB  = const(0x96)
_CMD_1WT   = const(0x78)

_PTR_STATUS = const(0xF0)
_PTR_DATA   = const(0xE1)

_ST_1WB = const(0x01)
_ST_PPD = const(0x02)
_ST_SD  = const(0x04)

_DS18B20_FAMILY = const(0x28)
_DS_CONVERT     = const(0x44)
_DS_READ_SP     = const(0xBE)
_DS_MATCH_ROM   = const(0x55)
_DS_SKIP_ROM    = const(0xCC)
_DS_SEARCH_ROM  = const(0xF0)

_BUSY_TIMEOUT_MS = const(50)
_CONVERT_WAIT_MS = const(820)


class DS2482Error(Exception):
    pass


def _crc8(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x01:
                crc = (crc >> 1) ^ 0x8C
            else:
                crc >>= 1
    return crc


class DS2482:
    def __init__(self, i2c, addr=config.DS2482_ADDR):
        self._i2c = i2c
        self._addr = addr
        self._buf1 = bytearray(1)
        self._buf2 = bytearray(2)
        self._reset_device()

    def _write1(self, cmd):
        self._buf1[0] = cmd
        self._i2c.writeto(self._addr, self._buf1)

    def _write2(self, cmd, data):
        self._buf2[0] = cmd
        self._buf2[1] = data
        self._i2c.writeto(self._addr, self._buf2)

    def _read1(self):
        self._i2c.readfrom_into(self._addr, self._buf1)
        return self._buf1[0]

    def _set_ptr(self, ptr):
        self._write2(_CMD_SRP, ptr)

    def _status(self):
        self._set_ptr(_PTR_STATUS)
        return self._read1()

    def _wait_idle(self):
        deadline = time.ticks_add(time.ticks_ms(), _BUSY_TIMEOUT_MS)
        while True:
            status = self._status()
            if not (status & _ST_1WB):
                return status
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                raise DS2482Error('1-Wire busy timeout')

    def _reset_device(self):
        self._write1(_CMD_DRST)
        status = self._read1()
        if not (status & 0x10):
            raise DS2482Error('DS2482 reset failed status=0x{:02X}'.format(status))
        self._write2(_CMD_WCFG, 0xE1)

    def ow_reset(self):
        self._write1(_CMD_1WRS)
        status = self._wait_idle()
        if status & _ST_SD:
            raise DS2482Error('1-Wire short detected')
        return bool(status & _ST_PPD)

    def ow_write_byte(self, value):
        self._write2(_CMD_1WWB, value)
        self._wait_idle()

    def ow_read_byte(self):
        self._write1(_CMD_1WRB)
        self._wait_idle()
        self._set_ptr(_PTR_DATA)
        return self._read1()

    def ow_triplet(self, direction):
        self._write2(_CMD_1WT, 0xFF if direction else 0x00)
        status = self._wait_idle()
        return bool(status & 0x20), bool(status & 0x40), bool(status & 0x80)

    def search_rom(self):
        roms = []
        last_discrepancy = 0
        last_device_flag = False
        rom = bytearray(8)

        while not last_device_flag:
            if not self.ow_reset():
                break
            self.ow_write_byte(_DS_SEARCH_ROM)
            last_zero = 0
            search_fail = False

            for bit_idx in range(64):
                byte_n   = bit_idx >> 3
                bit_mask = 1 << (bit_idx & 7)

                if bit_idx < last_discrepancy:
                    direction = 1 if (rom[byte_n] & bit_mask) else 0
                elif bit_idx == last_discrepancy:
                    direction = 1
                else:
                    direction = 0

                id_bit, comp_bit, taken = self.ow_triplet(direction)

                if id_bit and comp_bit:
                    search_fail = True
                    break

                if not id_bit and not comp_bit and not taken:
                    last_zero = bit_idx

                if taken:
                    rom[byte_n] |= bit_mask
                else:
                    rom[byte_n] &= ~bit_mask

            if not search_fail and _crc8(rom) == 0:
                roms.append(bytes(rom))
                last_discrepancy = last_zero
                if last_zero == 0:
                    last_device_flag = True
            else:
                break

        return roms


class SensorManager:
    def __init__(self, i2c):
        self._i2c = i2c
        self._bus  = None
        self.values = {label: None for label in config.SENSOR_LABELS}
        self._warned_connect_fail = False
        self._warned_missing_rom  = False
        self.connect()

    def connect(self):
        try:
            self._bus = DS2482(self._i2c, config.DS2482_ADDR)
            self._warned_connect_fail = False
            print('[sensors] DS2482 ok @ 0x{:02X}'.format(config.DS2482_ADDR))
            return True
        except Exception as e:
            self._bus = None
            if not self._warned_connect_fail:
                print('[sensors] DS2482 non disponibile:', e)
                self._warned_connect_fail = True
            return False

    def _read_scratchpad(self, rom):
        if not self._bus.ow_reset():
            return None
        self._bus.ow_write_byte(_DS_MATCH_ROM)
        for byte in rom:
            self._bus.ow_write_byte(byte)
        self._bus.ow_write_byte(_DS_READ_SP)
        scratchpad = bytearray(9)
        for idx in range(9):
            scratchpad[idx] = self._bus.ow_read_byte()
        if _crc8(scratchpad) != 0:
            return None
        raw = (scratchpad[1] << 8) | scratchpad[0]
        if raw & 0x8000:
            raw -= 0x10000
        return raw / 16.0

    def _trigger_convert(self):
        if not self._bus.ow_reset():
            raise DS2482Error('No 1-Wire presence at convert')
        self._bus.ow_write_byte(_DS_SKIP_ROM)
        self._bus.ow_write_byte(_DS_CONVERT)

    def scan(self):
        if self._bus is None and not self.connect():
            print('[sensors] scan impossibile: DS2482 assente')
            return []
        roms = self._bus.search_rom()
        print('[sensors] trovate {} sonde'.format(len(roms)))
        for rom in roms:
            family = 'DS18B20' if rom[0] == _DS18B20_FAMILY else 'family=0x{:02X}'.format(rom[0])
            print('  {} -> {}'.format(family, ':'.join('{:02X}'.format(b) for b in rom)))
        return roms

    async def read_all(self):
        if self._bus is None and not self.connect():
            for label in self.values:
                self.values[label] = None
            state.set_all_temps(self.values)
            return dict(self.values)

        # Usa ROM_MAP (nomi corretti da config)
        rom_items = [(label, rom) for label, rom in config.ROM_MAP.items() if rom is not None]
        if not rom_items:
            if not self._warned_missing_rom:
                print('[sensors] ROM_MAP vuota: popolare con ROM reali dopo scan')
                self._warned_missing_rom = True
            for label in self.values:
                self.values[label] = None
            state.set_all_temps(self.values)
            return dict(self.values)

        try:
            self._trigger_convert()
        except Exception as e:
            print('[sensors] convert error:', e)
            self._bus = None
            for label in self.values:
                self.values[label] = None
            state.set_all_temps(self.values)
            return dict(self.values)

        await asyncio.sleep_ms(_CONVERT_WAIT_MS)

        for label in self.values:
            self.values[label] = None

        for label, rom in rom_items:
            try:
                self.values[label] = self._read_scratchpad(bytes(rom))
            except Exception as e:
                print('[sensors] read {} error: {}'.format(label, e))
                self.values[label] = None

        state.set_all_temps(self.values)
        return dict(self.values)

    def snapshot(self):
        return dict(self.values)


async def sensor_task(sensor_mgr):
    while True:
        await sensor_mgr.read_all()
        await asyncio.sleep(config.SENSOR_INTERVAL_S)