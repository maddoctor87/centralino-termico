# devices/mcp23008.py
# Driver minimale MCP23008 per MicroPython

class MCP23008:
    REG_IODIR = 0x00
    REG_GPIO  = 0x09
    REG_GPPU  = 0x06
    REG_IOCON = 0x05

    IOCON_SEQOP = 0x20
    IOCON_ODR   = 0x04

    def __init__(self, i2c, addr):
        self.i2c = i2c
        self.addr = addr

    def _rd1(self, reg):
        return self.i2c.readfrom_mem(self.addr, reg, 1)[0]

    def _wr1(self, reg, val):
        self.i2c.writeto_mem(self.addr, reg, bytes([val & 0xFF]))

    def init_like_industrialshields(self):
        # Come peripheral-mcp23008.c:
        # IODIR = 0xFF, IOCON = SEQOP|ODR, GPPU = 0x00
        self._wr1(self.REG_IODIR, 0xFF)
        self._wr1(self.REG_IOCON, self.IOCON_SEQOP | self.IOCON_ODR)
        self._wr1(self.REG_GPPU, 0x00)

    def read_gpio(self):
        return self._rd1(self.REG_GPIO)

    def read_bit(self, index):
        if not 0 <= index <= 7:
            raise ValueError("MCP23008 index must be 0..7")
        return (self.read_gpio() >> index) & 1
