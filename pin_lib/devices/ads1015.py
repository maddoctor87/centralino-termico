# devices/ads1015.py
# Driver ADS1015 minimale compatibile con il comportamento osservato nel core Industrial Shields

import time

class ADS1015:
    REG_CONV = 0x00
    REG_CFG  = 0x01

    def __init__(self, i2c, addr):
        self.i2c = i2c
        self.addr = addr

    def read_raw(self, ch):
        # MUX single-ended come già validato
        mux_map = {
            0: 0x40,
            1: 0x50,
            2: 0x60,
            3: 0x70,
        }
        if ch not in mux_map:
            raise ValueError("ADS1015 channel must be 0..3")

        # Replica pratica già testata
        cfg_hi = 0x80 | 0x02 | mux_map[ch]
        cfg_lo = 0x80 | 0x03

        self.i2c.writeto_mem(self.addr, self.REG_CFG, bytes([cfg_hi, cfg_lo]))
        time.sleep_ms(2)

        data = self.i2c.readfrom_mem(self.addr, self.REG_CONV, 2)
        val = ((data[0] << 8) | data[1]) >> 4

        # Coerente con i test già fatti
        if val > 0x07FF:
            val = 0

        return val

    def read_digital(self, ch, threshold=1023):
        return 1 if self.read_raw(ch) > threshold else 0
