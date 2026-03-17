# devices/pca9685.py
# Driver PCA9685 minimale, riscritto da peripheral-pca9685.c

class PCA9685:
    REG_MODE1    = 0x00
    REG_MODE2    = 0x01
    REG_PRESCALE = 0xFE

    MODE1_SLEEP   = 0x10
    MODE1_AI      = 0x20

    def __init__(self, i2c, addr):
        self.i2c = i2c
        self.addr = addr
        self._initialized = False

    def _rd1(self, reg):
        return self.i2c.readfrom_mem(self.addr, reg, 1)[0]

    def _wr1(self, reg, val):
        self.i2c.writeto_mem(self.addr, reg, bytes([val & 0xFF]))

    def _set_led(self, index, on_l, on_h, off_l, off_h):
        base = 0x06 + (index * 4)
        # Con AI attivo potresti fare write multiplo, ma così è più robusto anche in debug
        self._wr1(base + 0, on_l)
        self._wr1(base + 1, on_h)
        self._wr1(base + 2, off_l)
        self._wr1(base + 3, off_h)

    def init_like_industrialshields(self):
        # Replica exact-ish di peripheral-pca9685.c
        self._wr1(self.REG_MODE1, self.MODE1_SLEEP | self.MODE1_AI)
        self._wr1(self.REG_PRESCALE, 11)  # ~500 Hz
        self._wr1(self.REG_MODE1, self.MODE1_AI)
        self._initialized = True

    def ensure_init(self):
        if not self._initialized:
            self.init_like_industrialshields()

    def set_out_on(self, index):
        self.ensure_init()
        self._set_led(index, 0x00, 0x10, 0x00, 0x00)

    def set_out_off(self, index):
        self.ensure_init()
        self._set_led(index, 0x00, 0x00, 0x00, 0x10)

    def set_out_pwm(self, index, value):
        self.ensure_init()
        if value < 0:
            value = 0
        elif value > 4095:
            value = 4095
        self._set_led(index, 0x00, 0x00, value & 0xFF, (value >> 8) & 0x0F)

    def dump_channel(self, index):
        base = 0x06 + (index * 4)
        return [
            self._rd1(base + 0),
            self._rd1(base + 1),
            self._rd1(base + 2),
            self._rd1(base + 3),
        ]
