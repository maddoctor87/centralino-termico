# --- actuators.py ---
# Attuatori PLC 21:
# - 7 uscite digitali Q0.0-Q0.4, Q0.6, Q0.7 via PCA9685 @ 0x40
# - 1 uscita PWM su A0.5 del PLC 21 per pompa C1 Wilo PWM2
# Le logiche di comando restano nei moduli control_*.

from micropython import const

import config
import state


# ── PCA9685 driver (I2C) ──────────────────────────────────────────────────────
# Registri per canale n: base = 0x06 + 4*n
#   [base+0] LED_ON_L, [base+1] LED_ON_H, [base+2] LED_OFF_L, [base+3] LED_OFF_H
# Full ON:  ON_H bit4=1, OFF=0
# Full OFF: ON=0, OFF_H bit4=1
# PWM 12-bit: ON=0, OFF = 0..4095

_PCA_MODE1 = const(0x00)
_PCA_PRE_SCALE = const(0xFE)
_PCA_LED0_BASE = const(0x06)
_PCA_FULL_ON = const(0x10)   # bit4 in ON_H
_PCA_FULL_OFF = const(0x10)  # bit4 in OFF_H
_PCA_SLEEP = const(0x10)     # bit4 in MODE1


class PCA9685:
    def __init__(self, i2c, addr, freq_hz=1000):
        self._i2c = i2c
        self._addr = addr
        # wake up (clear SLEEP bit)
        self._write_reg(_PCA_MODE1, 0x00)
        self._set_freq(freq_hz)

    def _write_reg(self, reg, val):
        self._i2c.writeto_mem(self._addr, reg, bytes([val & 0xFF]))

    def _read_reg(self, reg):
        return self._i2c.readfrom_mem(self._addr, reg, 1)[0]

    def _write4(self, base, b0, b1, b2, b3):
        # Scrittura robusta verificata sul banco:
        # il bulk writeto(addr, [reg, ...]) sul tuo stack non è affidabile.
        self._i2c.writeto_mem(self._addr, base + 0, bytes([b0 & 0xFF]))
        self._i2c.writeto_mem(self._addr, base + 1, bytes([b1 & 0xFF]))
        self._i2c.writeto_mem(self._addr, base + 2, bytes([b2 & 0xFF]))
        self._i2c.writeto_mem(self._addr, base + 3, bytes([b3 & 0xFF]))

    def _set_freq(self, freq_hz):
        # prescale = round(25 MHz / (4096 * freq)) - 1
        prescale = max(3, min(255, round(25_000_000 / (4096 * freq_hz)) - 1))
        mode1 = self._read_reg(_PCA_MODE1)
        self._write_reg(_PCA_MODE1, (mode1 & 0x7F) | _PCA_SLEEP)
        self._write_reg(_PCA_PRE_SCALE, prescale)
        self._write_reg(_PCA_MODE1, mode1 & 0x7F)

    def set_digital(self, ch, value):
        """Uscita digitale: full ON o full OFF."""
        base = _PCA_LED0_BASE + 4 * int(ch)
        if value:
            # FULL ON
            self._write4(base, 0x00, _PCA_FULL_ON, 0x00, 0x00)
        else:
            # FULL OFF
            self._write4(base, 0x00, 0x00, 0x00, _PCA_FULL_OFF)

    def set_pwm(self, ch, duty_4096):
        """Uscita PWM 0-4095 (12-bit)."""
        duty_4096 = max(0, min(4095, int(duty_4096)))
        base = _PCA_LED0_BASE + 4 * int(ch)
        self._write4(
            base,
            0x00,  # ON_L
            0x00,  # ON_H
            duty_4096 & 0xFF,
            (duty_4096 >> 8) & 0x0F,
        )

    def all_off(self):
        for ch in range(16):
            self.set_digital(ch, False)

    def dump_channel(self, ch):
        base = _PCA_LED0_BASE + 4 * int(ch)
        return [self._read_reg(base + i) for i in range(4)]


# ── RelayOutput ───────────────────────────────────────────────────────────────

class RelayOutput:
    def __init__(self, name, pca, ch, plc_io=None, plc_name=None):
        self.name = name
        self._pca = pca
        self._ch = ch
        self._plc_io = plc_io
        self._plc_name = plc_name
        self.state = False
        self._warned_unavailable = False

    @property
    def available(self):
        return self._plc_io is not None or (self._pca is not None and self._ch is not None)

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
        if self._plc_io is not None and self._plc_name is not None:
            self._plc_io.write_output(self._plc_name, 1 if value else 0)
        else:
            self._pca.set_digital(self._ch, value)
        state.set_relay_output(self.name, value)
        print('[relay] {}={}'.format(self.name, 'ON' if value else 'OFF'))


# ── Wilo PWM2 output (C1) ────────────────────────────────────────────────────

class WiloPWM2Output:
    def __init__(self, pca, ch, plc_io=None, plc_name=None, name='C1'):
        self._pca = pca
        self._ch = ch
        self._plc_io = plc_io
        self._plc_name = plc_name
        self._name = name
        self._wilo_duty_pct = config.C1_WILO_STANDBY_DUTY_PCT
        self._warned_unavailable = False
        self._has_written = False
        state.set_c1_wilo_duty_pct(self._wilo_duty_pct)

    @property
    def available(self):
        return self._plc_io is not None or (self._pca is not None and self._ch is not None)

    @property
    def wilo_duty_pct(self):
        return self._wilo_duty_pct

    def _normalize_wilo_duty_pct(self, wilo_duty_pct):
        try:
            wilo_duty_pct = int(wilo_duty_pct)
        except Exception:
            return config.C1_WILO_STANDBY_DUTY_PCT

        if wilo_duty_pct <= 0:
            return config.C1_WILO_STANDBY_DUTY_PCT
        if wilo_duty_pct >= config.C1_WILO_STANDBY_DUTY_PCT:
            return config.C1_WILO_STANDBY_DUTY_PCT
        return max(
            config.C1_WILO_MAX_SPEED_DUTY_PCT,
            min(config.C1_WILO_MIN_WORK_DUTY_PCT, wilo_duty_pct),
        )

    def _raw_pwm_12bit(self, wilo_duty_pct):
        return int(wilo_duty_pct) * 4095 // 100

    def _write_wilo_duty_pct(self, wilo_duty_pct):
        raw_pwm = self._raw_pwm_12bit(wilo_duty_pct)
        if self._plc_io is not None and self._plc_name is not None:
            self._plc_io.write_output(self._plc_name, raw_pwm)
            return raw_pwm
        if self._pca is not None and self._ch is not None:
            self._pca.set_pwm(self._ch, raw_pwm)
            return raw_pwm
        raise OSError('Wilo PWM2 output non disponibile')

    def set_wilo_duty(self, wilo_duty_pct):
        requested_wilo_duty_pct = self._normalize_wilo_duty_pct(wilo_duty_pct)
        standby_wilo_duty_pct = config.C1_WILO_STANDBY_DUTY_PCT

        if not self.available:
            if not self._warned_unavailable or requested_wilo_duty_pct != standby_wilo_duty_pct:
                print(
                    '[wilo_pwm] {} output non disponibile, requested_wilo_duty_pct={}% -> standby={}%' .format(
                        self._name, requested_wilo_duty_pct, standby_wilo_duty_pct
                    )
                )
                self._warned_unavailable = True
            self._wilo_duty_pct = standby_wilo_duty_pct
            self._has_written = False
            state.set_c1_wilo_duty_pct(self._wilo_duty_pct)
            return self._wilo_duty_pct

        self._warned_unavailable = False

        if requested_wilo_duty_pct == self._wilo_duty_pct and self._has_written:
            return self._wilo_duty_pct

        try:
            raw_pwm = self._write_wilo_duty_pct(requested_wilo_duty_pct)
            self._wilo_duty_pct = requested_wilo_duty_pct
            self._has_written = True
            state.set_c1_wilo_duty_pct(self._wilo_duty_pct)
            print(
                '[wilo_pwm] {} wilo_duty_pct={}% raw_pwm={}/4095'.format(
                    self._name, self._wilo_duty_pct, raw_pwm
                )
            )
            return self._wilo_duty_pct
        except Exception as e:
            print(
                '[wilo_pwm] {} write error: {} requested_wilo_duty_pct={}% -> standby={}%' .format(
                    self._name, e, requested_wilo_duty_pct, standby_wilo_duty_pct
                )
            )
            try:
                raw_pwm = self._write_wilo_duty_pct(standby_wilo_duty_pct)
                self._has_written = True
                print(
                    '[wilo_pwm] {} standby wilo_duty_pct={}% raw_pwm={}/4095'.format(
                        self._name, standby_wilo_duty_pct, raw_pwm
                    )
                )
            except Exception as standby_error:
                self._has_written = False
                print('[wilo_pwm] {} standby write error: {}'.format(self._name, standby_error))
            self._wilo_duty_pct = standby_wilo_duty_pct
            state.set_c1_wilo_duty_pct(self._wilo_duty_pct)
            return self._wilo_duty_pct

    def standby(self):
        return self.set_wilo_duty(config.C1_WILO_STANDBY_DUTY_PCT)

    def off(self):
        return self.standby()


PWMOutput = WiloPWM2Output


# ── ActuatorManager ───────────────────────────────────────────────────────────

class ActuatorManager:
    def __init__(self, i2c, plc_io=None):
        self._pca = None
        self._plc_io = plc_io
        self.c1_wilo_output = None
        self.c1_wilo_pwm = None
        self.relays = {}

        if self._plc_io is None:
            try:
                self._pca = PCA9685(i2c, config.PCA9685_ADDR, config.PCA9685_FREQ)
                print('[actuators] PCA9685 ok @ 0x{:02X}'.format(config.PCA9685_ADDR))
            except Exception as e:
                print('[actuators] PCA9685 non disponibile:', e)
        else:
            print('[actuators] usando plc_io come HAL uscite')

        self.c1_wilo_output = WiloPWM2Output(
            self._pca,
            config.C1_PWM_CH,
            plc_io=self._plc_io,
            plc_name=config.C1_PWM_OUTPUT,
        )
        self.c1_wilo_pwm = self.c1_wilo_output

        for name, ch in config.RELAY_OUTPUTS.items():
            self.relays[name] = RelayOutput(name, self._pca, ch, plc_io=self._plc_io, plc_name=config.RELAY_PLC_OUTPUTS.get(name))
            state.set_relay_available(name, self.relays[name].available)

        self.all_off()

    def set_relay(self, name, value):
        relay = self.relays.get(name)
        if relay is None:
            raise ValueError('relay {} non definito'.format(name))
        relay.set(value)

    def set_c1_wilo_duty(self, wilo_duty_pct):
        self.c1_wilo_output.set_wilo_duty(wilo_duty_pct)

    def set_c1_pwm(self, duty_pct):
        # Compatibilità con vecchi moduli che chiamano ancora il nome pre-Wilo.
        self.set_c1_wilo_duty(duty_pct)

    def all_off(self):
        self.c1_wilo_output.standby()
        for relay in self.relays.values():
            relay.set(config.SAFE_RELAY_STATE)

    def snapshot(self):
        return {
            'c1_wilo_duty_pct': self.c1_wilo_output.wilo_duty_pct,
            'relays': {name: relay.state for name, relay in self.relays.items()},
        }
