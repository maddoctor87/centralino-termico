# --- actuators.py ---
# Attuatori PLC 21:
# - 5 uscite digitali Q0.0-Q0.4 via driver I2C interno al progetto
# - 1 uscita PWM su Q0.5 del PLC 21 per pompa C1 Wilo
#   (richiede switch B1 = ON)
# Le logiche di comando restano nei moduli control_*.

import config
import state


# ── PCA9685 driver (I2C) ──────────────────────────────────────────────────────
# Registri per canale n: base = 0x06 + 4*n
#   [base+0] LED_ON_L, [base+1] LED_ON_H, [base+2] LED_OFF_L, [base+3] LED_OFF_H
# Full ON:  ON_H bit4=1, OFF=0
# Full OFF: ON=0, OFF_H bit4=1
# PWM 12-bit: ON=0, OFF = 0..4095

_PCA_MODE1    = const(0x00)
_PCA_PRE_SCALE = const(0xFE)
_PCA_LED0_BASE = const(0x06)
_PCA_FULL_ON  = const(0x10)   # bit4 in ON_H
_PCA_FULL_OFF = const(0x10)   # bit4 in OFF_H
_PCA_SLEEP    = const(0x10)   # bit4 in MODE1


class PCA9685:
    def __init__(self, i2c, addr, freq_hz=1000):
        self._i2c = i2c
        self._addr = addr
        self._buf = bytearray(5)
        # wake up (clear SLEEP bit)
        self._write_reg(_PCA_MODE1, 0x00)
        self._set_freq(freq_hz)

    def _write_reg(self, reg, val):
        self._i2c.writeto(self._addr, bytes([reg, val]))

    def _set_freq(self, freq_hz):
        # prescale = round(25 MHz / (4096 * freq)) - 1
        prescale = max(3, min(255, round(25_000_000 / (4096 * freq_hz)) - 1))
        mode1 = self._read_reg(_PCA_MODE1)
        self._write_reg(_PCA_MODE1, (mode1 & 0x7F) | _PCA_SLEEP)
        self._write_reg(_PCA_PRE_SCALE, prescale)
        self._write_reg(_PCA_MODE1, mode1 & 0x7F)

    def _read_reg(self, reg):
        self._i2c.writeto(self._addr, bytes([reg]))
        buf = bytearray(1)
        self._i2c.readfrom_into(self._addr, buf)
        return buf[0]

    def set_digital(self, ch, value):
        """Uscita digitale: full ON o full OFF."""
        base = _PCA_LED0_BASE + 4 * ch
        if value:
            data = bytes([base, 0x00, _PCA_FULL_ON, 0x00, 0x00])
        else:
            data = bytes([base, 0x00, 0x00, 0x00, _PCA_FULL_OFF])
        self._i2c.writeto(self._addr, data)

    def set_pwm(self, ch, duty_4096):
        """Uscita PWM 0-4095 (12-bit). 0 = 0 V, 4095 = 10 V."""
        duty_4096 = max(0, min(4095, int(duty_4096)))
        base = _PCA_LED0_BASE + 4 * ch
        data = bytes([base, 0x00, 0x00, duty_4096 & 0xFF, (duty_4096 >> 8) & 0x0F])
        self._i2c.writeto(self._addr, data)

    def all_off(self):
        for ch in range(16):
            self.set_digital(ch, False)


# ── RelayOutput ───────────────────────────────────────────────────────────────

class RelayOutput:
    def __init__(self, name, pca, ch):
        self.name = name
        self._pca = pca
        self._ch = ch
        self.state = False
        self._warned_unavailable = False

    @property
    def available(self):
        return self._pca is not None and self._ch is not None

    def set(self, value):
        value = bool(value)
        if not self.available:
            if not self._warned_unavailable or value:
                if self._ch is None:
                    print('[relay] {} mapping TODO'.format(self.name))
                else:
                    print('[relay] {} PCA9685 non disponibile'.format(self.name))
                self._warned_unavailable = True
            self.state = False
            state.set_relay_output(self.name, False)
            return

        self._warned_unavailable = False
        if value == self.state:
            return

        self.state = value
        self._pca.set_digital(self._ch, value)
        state.set_relay_output(self.name, value)
        print('[relay] {}={}'.format(self.name, 'ON' if value else 'OFF'))


# ── PWMOutput (C1) ───────────────────────────────────────────────────────────
# Uscita PWM C1 su Q0.5 del PLC 21.
# Richiede switch B1 = ON.
# Pilotaggio verso modulo HALJIA PC817 4ch per isolamento.
# Frequenza nominale pompa Wilo PWM2: 1000 Hz.

class PWMOutput:
    def __init__(self, pca, ch):
        self._pca = pca
        self._ch = ch
        self._duty = 0
        state.set_c1_duty(0)

    @property
    def duty(self):
        return self._duty

    def set_duty(self, duty_percent):
        duty_percent = max(0, min(100, int(duty_percent)))
        if duty_percent == self._duty:
            return
        self._duty = duty_percent

        if self._pca is not None and self._ch is not None:
            self._pca.set_pwm(self._ch, duty_percent * 4095 // 100)

        state.set_c1_duty(duty_percent)
        print('[pwm] C1={}%' .format(duty_percent))

    def off(self):
        self.set_duty(0)


# ── ActuatorManager ───────────────────────────────────────────────────────────

class ActuatorManager:
    def __init__(self, i2c):
        self._pca = None
        self.c1_pwm = None
        self.relays = {}

        # Inizializza PCA9685 (per le uscite digitali Q0.x).
        try:
            self._pca = PCA9685(i2c, config.PCA9685_ADDR, config.PCA9685_FREQ)
            print('[actuators] PCA9685 ok @ 0x{:02X}'.format(config.PCA9685_ADDR))
        except Exception as e:
            print('[actuators] PCA9685 non disponibile:', e)
        # Uscita PWM C1 su Q0.5 del PLC 21.
        # B1 ON -> Q0.5, B1 OFF -> A0.5.
        # In questo progetto si usa Q0.5, quindi B1 deve essere ON.
        self.c1_pwm = PWMOutput(self._pca, config.C1_PWM_CH)

        for name, ch in config.RELAY_OUTPUTS.items():
            self.relays[name] = RelayOutput(name, self._pca, ch)
            state.set_relay_available(name, self.relays[name].available)

        self.all_off()

    def set_relay(self, name, value):
        relay = self.relays.get(name)
        if relay is None:
            raise ValueError('relay {} non definito'.format(name))
        relay.set(value)

    def set_c1_pwm(self, duty_percent):
        self.c1_pwm.set_duty(duty_percent)

    def all_off(self):
        self.c1_pwm.off()
        for relay in self.relays.values():
            relay.set(config.SAFE_RELAY_STATE)

    def snapshot(self):
        return {
            'c1_pwm': self.c1_pwm.duty,
            'relays': {name: relay.state for name, relay in self.relays.items()},
        }
