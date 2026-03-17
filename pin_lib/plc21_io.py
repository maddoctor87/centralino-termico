# plc21_io.py
# Layer alto livello per Industrial Shields ESP32 PLC 21+ su MicroPython

from machine import I2C, Pin

from plc21_map import (
    MODEL_NAME,
    INPUTS,
    OUTPUTS,
    ADS_DIGITAL_THRESHOLD,
    MCP_DEFAULT_ADDRS,
    ADS_DEFAULT_ADDRS,
    PCA_DEFAULT_ADDRS,
)

from devices.mcp23008 import MCP23008
from devices.ads1015 import ADS1015
from devices.pca9685 import PCA9685


class PLC21IO:
    def __init__(self, i2c=None, scl_pin=22, sda_pin=21, freq=100000):
        if i2c is None:
            i2c = I2C(0, scl=Pin(scl_pin), sda=Pin(sda_pin), freq=freq)

        self.i2c = i2c

        self._mcps = {}
        self._ads = {}
        self._pcas = {}
        self._gpio_inputs = {}

        self._init_devices()

    def _init_devices(self):
        # MCP
        for addr in MCP_DEFAULT_ADDRS:
            try:
                dev = MCP23008(self.i2c, addr)
                dev.init_like_industrialshields()
                self._mcps[addr] = dev
            except Exception:
                pass

        # ADS
        for addr in ADS_DEFAULT_ADDRS:
            try:
                self._ads[addr] = ADS1015(self.i2c, addr)
            except Exception:
                pass

        # PCA
        for addr in PCA_DEFAULT_ADDRS:
            try:
                dev = PCA9685(self.i2c, addr)
                dev.init_like_industrialshields()
                self._pcas[addr] = dev
            except Exception:
                pass

        # GPIO inputs
        gpio_pins = set()
        for info in INPUTS.values():
            if info["type"] == "gpio":
                gpio_pins.add(info["pin"])

        for pin_num in gpio_pins:
            self._gpio_inputs[pin_num] = Pin(pin_num, Pin.IN)

    def scan(self):
        return self.i2c.scan()

    def model_name(self):
        return MODEL_NAME

    def read_input_raw(self, name):
        if name not in INPUTS:
            raise KeyError("Unknown input: {}".format(name))

        info = INPUTS[name]
        typ = info["type"]

        if typ == "mcp":
            addr = info["addr"]
            idx = info["index"]
            dev = self._mcps.get(addr)
            if dev is None:
                raise OSError("MCP23008 not initialized at {}".format(hex(addr)))
            return dev.read_bit(idx)

        if typ == "gpio":
            pin_num = info["pin"]
            pin = self._gpio_inputs[pin_num]
            return pin.value()

        if typ == "ads":
            addr = info["addr"]
            idx = info["index"]
            dev = self._ads.get(addr)
            if dev is None:
                raise OSError("ADS1015 not available at {}".format(hex(addr)))
            return dev.read_raw(idx)

        raise ValueError("Unsupported input type: {}".format(typ))

    def read_input(self, name, threshold=ADS_DIGITAL_THRESHOLD):
        if name not in INPUTS:
            raise KeyError("Unknown input: {}".format(name))

        info = INPUTS[name]
        if info["type"] == "ads":
            raw = self.read_input_raw(name)
            return 1 if raw > threshold else 0

        return self.read_input_raw(name)

    def write_output(self, name, value):
        if name not in OUTPUTS:
            raise KeyError("Unknown output: {}".format(name))

        info = OUTPUTS[name]
        typ = info["type"]
        addr = info["addr"]
        idx = info["index"]

        dev = self._pcas.get(addr)
        if dev is None:
            raise OSError("PCA9685 not initialized at {}".format(hex(addr)))

        if typ == "pca_digital":
            if value:
                dev.set_out_on(idx)
            else:
                dev.set_out_off(idx)
            return

        if typ == "pca_pwm":
            # se arriva 0/1 lo tratto come digitale
            if value in (0, 1):
                if value:
                    dev.set_out_on(idx)
                else:
                    dev.set_out_off(idx)
            else:
                dev.set_out_pwm(idx, int(value))
            return

        raise ValueError("Unsupported output type: {}".format(typ))

    def output_on(self, name):
        self.write_output(name, 1)

    def output_off(self, name):
        self.write_output(name, 0)

    def read_all_inputs(self, threshold=ADS_DIGITAL_THRESHOLD, raw_ads=False):
        out = {}
        for name, info in INPUTS.items():
            if info["type"] == "ads" and raw_ads:
                out[name] = self.read_input_raw(name)
            else:
                out[name] = self.read_input(name, threshold=threshold)
        return out

    def dump_output_channel(self, name):
        if name not in OUTPUTS:
            raise KeyError("Unknown output: {}".format(name))

        info = OUTPUTS[name]
        addr = info["addr"]
        idx = info["index"]

        dev = self._pcas.get(addr)
        if dev is None:
            raise OSError("PCA9685 not initialized at {}".format(hex(addr)))

        return dev.dump_channel(idx)
