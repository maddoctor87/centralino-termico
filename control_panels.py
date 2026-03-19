# --- control_panels.py ---
# Controllo C1 (pompa pannelli) per Wilo Yonos PARA PWM2.
#
# Logica:
# - Tavg_solare = (S2 + S3) / 2
# - Thigh_solare = max(S2, S3)
# - Delta = S1 - Tavg_solare
# - ON  se Delta >= soglia_on
# - OFF se Delta <= soglia_off
# - Freno lineare su Thigh tra C1_THIGH_FULL e C1_THIGH_STOP
# - Stop hard se S4 >= C1_STOP_HARD_TEMP, con latch
# - Reset latch quando Thigh <= C1_LATCH_RESET_TEMP
# - Override anti-stagnazione se S1 >= C1_STAGNATION_TEMP e Thigh >= C1_THIGH_STOP
#
# PWM2 Wilo:
# - wilo_duty_pct basso => velocità alta
# - wilo_duty_pct alto  => velocità bassa / standby
# - qui usiamo:
#     5%   = max speed
#     85%  = min working speed
#     95%  = stop / standby

import uasyncio as asyncio
import config
import state


# ---------------------------------------------------------------------------
# Helpers state-safe
# ---------------------------------------------------------------------------

def _set_alarm(name: str, value: bool) -> None:
    fn = getattr(state, "set_alarm", None)
    if callable(fn):
        fn(name, bool(value))


def _set_c1_latch(value: bool) -> None:
    fn = getattr(state, "set_c1_latch", None)
    if callable(fn):
        fn(bool(value))
    else:
        # fallback se state.py non ha helper dedicato
        setattr(state, "c1_latch", bool(value))


def _get_c1_latch() -> bool:
    fn = getattr(state, "get_c1_latch", None)
    if callable(fn):
        return bool(fn())
    return bool(getattr(state, "c1_latch", False))


def _set_c1_active(value: bool) -> None:
    fn = getattr(state, "set_c1_active", None)
    if callable(fn):
        fn(bool(value))
    else:
        setattr(state, "c1_active", bool(value))
        setattr(state, "c1_on_state", bool(value))


def _get_c1_active() -> bool:
    fn = getattr(state, "get_c1_active", None)
    if callable(fn):
        return bool(fn())
    return bool(getattr(state, "c1_active", getattr(state, "c1_on_state", False)))


def _manual_mode_active() -> bool:
    fn = getattr(state, "get_manual_mode", None)
    if callable(fn):
        return bool(fn())
    return bool(getattr(state, "manual_mode", False))


def _get_setpoint(name: str, default):
    fn = getattr(state, "get_setpoint", None)
    if callable(fn):
        try:
            value = fn(name)
            return default if value is None else value
        except Exception:
            return default
    return default


# ---------------------------------------------------------------------------
# Helpers logica
# ---------------------------------------------------------------------------

def _is_valid_temp(value) -> bool:
    return value is not None and isinstance(value, (int, float))


def _map_linear(value, in_min, in_max, out_min, out_max):
    if in_max <= in_min:
        return out_min
    if value <= in_min:
        return out_min
    if value >= in_max:
        return out_max
    ratio = (value - in_min) / (in_max - in_min)
    return out_min + ratio * (out_max - out_min)


def _brake_factor(thigh: float) -> float:
    """
    Freno lineare:
    - <= C1_THIGH_FULL : 1.0
    - >= C1_THIGH_STOP : 0.0
    """
    if thigh <= config.C1_THIGH_FULL:
        return 1.0
    if thigh >= config.C1_THIGH_STOP:
        return 0.0
    return 1.0 - ((thigh - config.C1_THIGH_FULL) / (config.C1_THIGH_STOP - config.C1_THIGH_FULL))


def _speed_pct_from_delta(delta_c: float) -> int:
    """
    Mappa il delta termico in una "richiesta velocità" classica 0..100.
    Poi questa verrà convertita nel wilo_duty_pct PWM2 invertito.
    """
    speed_pct_min = _get_setpoint("c1_speed_pct_min", config.C1_SPEED_PCT_MIN)
    speed_pct_max = _get_setpoint("c1_speed_pct_max", config.C1_SPEED_PCT_MAX)
    delta_min = _get_setpoint("delta_pwm_min", config.C1_DELTA_PWM_MIN)
    delta_max = _get_setpoint("delta_pwm_max", config.C1_DELTA_PWM_MAX)

    speed_pct = _map_linear(delta_c, delta_min, delta_max, speed_pct_min, speed_pct_max)
    speed_pct = max(0, min(100, int(round(speed_pct))))
    return speed_pct


def speed_pct_to_wilo_duty_pct(speed_pct: int) -> int:
    """
    Converte una richiesta velocità "umana" 0..100 nel duty PWM2 Wilo.
    Regola usata:
    - 0%   -> 95%  (stop / standby)
    - 1-100% -> 85..5% (min..max)
    """
    if speed_pct <= 0:
        return config.C1_WILO_STANDBY_DUTY_PCT

    speed_pct = max(1, min(100, int(speed_pct)))
    wilo_duty_pct = _map_linear(
        speed_pct,
        1,
        100,
        config.C1_WILO_MIN_WORK_DUTY_PCT,
        config.C1_WILO_MAX_SPEED_DUTY_PCT,
    )
    return int(
        round(
            max(
                config.C1_WILO_MAX_SPEED_DUTY_PCT,
                min(config.C1_WILO_MIN_WORK_DUTY_PCT, wilo_duty_pct),
            )
        )
    )


def _speed_pct_to_wilo_duty_pct(speed_pct: int) -> int:
    return speed_pct_to_wilo_duty_pct(speed_pct)


def validate_c1_wilo_pwm2_mapping():
    samples = {
        0: speed_pct_to_wilo_duty_pct(0),
        1: speed_pct_to_wilo_duty_pct(1),
        50: speed_pct_to_wilo_duty_pct(50),
        100: speed_pct_to_wilo_duty_pct(100),
    }
    active_values = [speed_pct_to_wilo_duty_pct(speed_pct) for speed_pct in (1, 25, 50, 75, 100)]

    if samples[0] != config.C1_WILO_STANDBY_DUTY_PCT:
        raise AssertionError('0% must map to standby {}'.format(config.C1_WILO_STANDBY_DUTY_PCT))
    if samples[1] != config.C1_WILO_MIN_WORK_DUTY_PCT:
        raise AssertionError('1% must map to {}'.format(config.C1_WILO_MIN_WORK_DUTY_PCT))
    if samples[100] != config.C1_WILO_MAX_SPEED_DUTY_PCT:
        raise AssertionError('100% must map to {}'.format(config.C1_WILO_MAX_SPEED_DUTY_PCT))
    if any(
        value < config.C1_WILO_MAX_SPEED_DUTY_PCT or value > config.C1_WILO_MIN_WORK_DUTY_PCT
        for value in active_values
    ):
        raise AssertionError('active branch generated out-of-range Wilo duty values')
    if any(active_values[idx] < active_values[idx + 1] for idx in range(len(active_values) - 1)):
        raise AssertionError('active branch is not inverted')
    if speed_pct_to_wilo_duty_pct(-10) != config.C1_WILO_STANDBY_DUTY_PCT:
        raise AssertionError('negative speed must map to standby')

    return samples


def _compute_c1_wilo_duty_pct(s1: float, s2: float, s3: float, s4: float, active: bool) -> int:
    """
    Restituisce il wilo_duty_pct PWM2 finale per la pompa C1.
    95 = stop / standby
    5..85 = modulazione valida Wilo PWM2
    """
    tavg = (s2 + s3) / 2.0
    thigh = max(s2, s3)
    delta = s1 - tavg

    # Se non attiva per isteresi, pompa ferma
    if not active:
        return config.C1_WILO_STANDBY_DUTY_PCT

    # Override anti-stagnazione
    if s1 >= config.C1_STAGNATION_TEMP and thigh >= config.C1_THIGH_STOP:
        override_speed = max(1, min(100, int(config.C1_STAGNATION_SPEED_PCT)))
        return speed_pct_to_wilo_duty_pct(override_speed)

    # Richiesta base da delta termico
    speed_pct = _speed_pct_from_delta(delta)

    # Freno su Thigh
    factor = _brake_factor(thigh)
    speed_pct = int(round(speed_pct * factor))

    # Se il freno porta a zero, stop
    if speed_pct <= 0:
        return config.C1_WILO_STANDBY_DUTY_PCT

    return speed_pct_to_wilo_duty_pct(speed_pct)


# ---------------------------------------------------------------------------
# Core control
# ---------------------------------------------------------------------------

def run_once(sensor_mgr, actuator_mgr):
    """
    Esegue un ciclo di controllo C1.
    """
    if _manual_mode_active():
        manual_wilo_duty_pct = getattr(
            state,
            "manual_c1_wilo_duty_pct",
            config.C1_WILO_STANDBY_DUTY_PCT,
        )
        actuator_mgr.set_c1_wilo_duty(manual_wilo_duty_pct)
        _set_c1_active(0 < int(manual_wilo_duty_pct) < config.C1_WILO_STANDBY_DUTY_PCT)
        return

    temps = sensor_mgr.snapshot()
    s1 = temps.get("S1")
    s2 = temps.get("S2")
    s3 = temps.get("S3")
    s4 = temps.get("S4")

    panels_ok = all(_is_valid_temp(v) for v in (s1, s2, s3))
    s4_ok = _is_valid_temp(s4)

    _set_alarm("ALARM_SENSORS_PANELS", not panels_ok)
    _set_alarm("ALARM_S4_INVALID", not s4_ok)

    # Se sensori critici mancanti, fermo C1
    if not panels_ok or not s4_ok:
        actuator_mgr.set_c1_wilo_duty(config.C1_WILO_STANDBY_DUTY_PCT)
        _set_c1_active(False)
        return

    tavg = (s2 + s3) / 2.0
    thigh = max(s2, s3)
    delta = s1 - tavg

    # Hysteresis ON/OFF
    c1_on_delta = getattr(config, "C1_ON_DELTA", 4.0)
    c1_off_delta = getattr(config, "C1_OFF_DELTA", 2.0)

    active = _get_c1_active()

    # Hard stop latch su S4
    latched = _get_c1_latch()
    if s4 >= config.C1_STOP_HARD_TEMP:
        latched = True
        _set_c1_latch(True)

    if latched:
        if thigh <= config.C1_LATCH_RESET_TEMP:
            latched = False
            _set_c1_latch(False)
        else:
            actuator_mgr.set_c1_wilo_duty(config.C1_WILO_STANDBY_DUTY_PCT)
            _set_c1_active(False)
            return

    if not active and delta >= c1_on_delta:
        active = True
    elif active and delta <= c1_off_delta:
        active = False

    wilo_duty_pct = _compute_c1_wilo_duty_pct(s1, s2, s3, s4, active)

    actuator_mgr.set_c1_wilo_duty(wilo_duty_pct)
    _set_c1_active(active)

    # opzionale: debug leggibile
    print(
        "[panels] "
        "S1={:.1f} S2={:.1f} S3={:.1f} S4={:.1f} "
        "Tavg={:.1f} Thigh={:.1f} Delta={:.1f} "
        "active={} wilo_duty_pct={}%".format(
            s1, s2, s3, s4, tavg, thigh, delta, active, wilo_duty_pct
        )
    )


async def control_panels_task(sensor_mgr, actuator_mgr):
    while True:
        try:
            run_once(sensor_mgr, actuator_mgr)
        except Exception as e:
            print("[panels] error: {} -> forcing C1 standby".format(e))
            try:
                actuator_mgr.set_c1_wilo_duty(config.C1_WILO_STANDBY_DUTY_PCT)
                _set_c1_active(False)
            except Exception:
                pass
        await asyncio.sleep(config.CONTROL_INTERVAL_S)
