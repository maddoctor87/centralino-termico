# --- control_panels.py ---
# Controllo C1 (pompa pannelli solari PWM) secondo regole termiche.

import uasyncio as asyncio
import time

import config
import state


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _map_linear(x, in_min, in_max, out_min, out_max):
    if in_min == in_max:
        return out_min
    t = (x - in_min) / (in_max - in_min)
    return out_min + t * (out_max - out_min)


def _compute_c1_duty(temps):
    # Se mancano sensori critici, spegni la pompa per sicurezza.
    for label in ('S1', 'S2', 'S3', 'S4'):
        if temps.get(label) is None:
            return 0, False

    s1 = temps['S1']
    s2 = temps['S2']
    s3 = temps['S3']
    s4 = temps['S4']

    thigh = max(s2, s3)
    tavg = (s2 + s3) / 2.0
    delta = s1 - tavg

    # Hard stop PDC
    if s4 >= config.C1_HARD_STOP_TEMP:
        return 0, True

    # Clear latch if condizioni consentono
    if state.c1_latched_hard_stop and thigh <= config.C1_LATCH_RESET_TEMP:
        state.c1_latched_hard_stop = False

    if state.c1_latched_hard_stop:
        return 0, True

    # Base duty mapping
    duty = _map_linear(delta, config.C1_DELTA_PWM_MIN, config.C1_DELTA_PWM_MAX, config.C1_PWM_MIN, config.C1_PWM_MAX)

    # Freno in funzione della temperatura massima solare
    if thigh <= config.C1_FRICTION_FACTOR_START:
        factor = 1.0
    elif thigh >= config.C1_FRICTION_FACTOR_END:
        factor = 0.0
    else:
        factor = (config.C1_FRICTION_FACTOR_END - thigh) / (config.C1_FRICTION_FACTOR_END - config.C1_FRICTION_FACTOR_START)

    duty *= factor

    # Override anti-stagnazione
    if s1 >= config.C1_OVERRIDE_TEMP and thigh >= config.C1_FRICTION_FACTOR_END:
        duty = max(duty, config.C1_STAGNATION_DUTY)

    duty = int(_clamp(duty, 0, 100))
    return duty, False


def run_once(sensor_mgr, actuator_mgr):
    if state.manual_mode:
        actuator_mgr.set_c1_pwm(state.manual_pwm_duty)
        return

    duty, latched = _compute_c1_duty(state.temps)
    state.c1_latched_hard_stop = latched
    actuator_mgr.set_c1_pwm(duty)
    state.c1_on_state = duty > 0


async def control_panels_task(sensor_mgr, actuator_mgr):
    print('[C1] controllo pannelli avviato')
    while True:
        try:
            run_once(sensor_mgr, actuator_mgr)
        except Exception as e:
            print('[C1] exception:', e)
            actuator_mgr.set_c1_pwm(config.SAFE_PWM_DUTY)
        await asyncio.sleep(config.CONTROL_INTERVAL_MS / 1000)
