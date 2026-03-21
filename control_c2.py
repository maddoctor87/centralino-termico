# --- control_c2.py ---
# Controllo Pompa 2 / C2: trasferimento solare -> PDC.
#
# Logica:
# - Tsolare = media boiler solare = ((S2 + S3) / 2)
# - Tpdc    = media boiler PDC    = ((S4 + S5) / 2)
# - Delta_trasferimento = Tsolare - Tpdc
# - ON  se Delta_trasferimento > C2_DELTA_ON
# - OFF se Delta_trasferimento < C2_DELTA_OFF
# - Override aiuto PDC: se PDC_HELP_REQUEST e Tsolare > S5, forza C2 ON
# - Stop aggiuntivo se la PDC sta lavorando in ACS e S1 < Tsolare
#   solo fuori dall'override aiuto PDC
# - Hard stop se S4 >= C2_HARD_STOP_TEMP
#
# Feedback NC opzionale:
# - relè C2 OFF -> contatto NC chiuso  -> feedback atteso = True
# - relè C2 ON  -> contatto NC aperto  -> feedback atteso = False

import uasyncio as asyncio
import time

import config
import state


def _get_input_snapshot(input_mgr):
    if input_mgr is None:
        return {}
    try:
        return input_mgr.snapshot()
    except Exception:
        return {}


def _expected_fb_nc_from_c2_command(c2_command_on: bool) -> bool:
    """
    Feedback NC:
    - C2 OFF -> contatto NC chiuso  -> True
    - C2 ON  -> contatto NC aperto  -> False
    """
    return not bool(c2_command_on)


def _check_c2_feedback(input_mgr, c2_command_on: bool):
    fb_name = getattr(config, "C2_FB_NC_NAME", None)
    if not fb_name:
        return

    inputs = _get_input_snapshot(input_mgr)
    if fb_name not in inputs:
        return

    fb_actual = bool(inputs.get(fb_name, False))
    fb_expected = _expected_fb_nc_from_c2_command(c2_command_on)

    prev_expected = state.get_c2_fb_expected()
    now_s = time.time()

    # se il comando atteso cambia, ricomincia la finestra di verifica
    if prev_expected is None or bool(prev_expected) != bool(fb_expected):
        state.set_c2_fb_expected(fb_expected)
        state.set_c2_fb_last_change_ts(now_s)
        state.set_c2_fb_alarm(False)
        return

    elapsed = now_s - state.get_c2_fb_last_change_ts()
    timeout_s = getattr(config, "C2_FB_TIMEOUT_S", 1)

    if elapsed >= timeout_s:
        mismatch = (fb_actual != fb_expected)
        state.set_c2_fb_alarm(mismatch)

        fn = getattr(state, "set_alarm", None)
        if callable(fn):
            fn("ALARM_C2_FB_MISMATCH", mismatch)

        if mismatch:
            print(
                "[C2] feedback mismatch: cmd_on={} expected_nc={} actual_nc={}".format(
                    c2_command_on, fb_expected, fb_actual
                )
            )


def run_once(sensor_mgr, actuator_mgr, input_mgr=None):
    inputs = _get_input_snapshot(input_mgr)

    if state.manual_mode:
        manual_value = state.manual_relays.get('C2', False)
        actuator_mgr.set_relay('C2', manual_value)
        state.c2_on_state = bool(manual_value)
        _check_c2_feedback(input_mgr, manual_value)
        return

    # Sensori richiesti
    required = ('S1', 'S2', 'S3', 'S4', 'S5')
    missing = [label for label in required if state.temps.get(label) is None]
    if missing:
        actuator_mgr.set_relay('C2', config.SAFE_RELAY_STATE)
        state.c2_on_state = False

        fn = getattr(state, "set_alarm", None)
        if callable(fn):
            fn("ALARM_SENSORS_C2", True)

        _check_c2_feedback(input_mgr, False)
        return

    # Sensori validi
    fn = getattr(state, "set_alarm", None)
    if callable(fn):
        fn("ALARM_SENSORS_C2", False)

    s1 = state.temps['S1']
    s2 = state.temps['S2']
    s3 = state.temps['S3']
    s4 = state.temps['S4']
    s5 = state.temps['S5']

    # Hard stop lato boiler PDC alto
    if s4 >= config.C2_HARD_STOP_TEMP:
        actuator_mgr.set_relay('C2', config.SAFE_RELAY_STATE)
        state.c2_on_state = False
        _check_c2_feedback(input_mgr, False)
        return

    # Trasferimento diretto boiler solare -> boiler PDC.
    # La logica di emergenza resta invariata: sensori validi richiesti e hard
    # stop su S4 alto.
    tsolare = (s2 + s3) / 2.0
    tpdc = (s4 + s5) / 2.0
    delta_transfer = tsolare - tpdc
    help_delta = tsolare - s5

    # Se la PDC chiede aiuto e il boiler solare e' gia' piu' caldo del fondo
    # boiler PDC, usa subito C2 come prima scelta invece del gas.
    if inputs.get('PDC_HELP_REQUEST', False) and help_delta > 0:
        actuator_mgr.set_relay('C2', True)
        state.c2_on_state = True
        _check_c2_feedback(input_mgr, True)
        print(
            "[C2] aiuto PDC da boiler solare: Tsolare={:.1f} S5={:.1f} delta_help={:.1f} state=True".format(
                tsolare, s5, help_delta
            )
        )
        return

    # Se ACS e' attiva e i pannelli sono piu freddi del boiler solare,
    # evita di trasferire ulteriore energia verso il boiler PDC.
    if inputs.get('PDC_WORK_ACS', False) and s1 < tsolare:
        actuator_mgr.set_relay('C2', config.SAFE_RELAY_STATE)
        state.c2_on_state = False
        _check_c2_feedback(input_mgr, False)
        print(
            "[C2] stop ACS: S1={:.1f} < Tsolare={:.1f} con PDC_WORK_ACS attivo".format(
                s1, tsolare
            )
        )
        return

    on_thresh = config.C2_DELTA_ON
    off_thresh = config.C2_DELTA_OFF

    # Hysteresis
    if state.c2_on_state:
        new_state = delta_transfer > off_thresh
    else:
        new_state = delta_transfer > on_thresh

    actuator_mgr.set_relay('C2', new_state)
    state.c2_on_state = bool(new_state)

    _check_c2_feedback(input_mgr, new_state)

    print(
        "[C2] S1={:.1f} S2={:.1f} S3={:.1f} S4={:.1f} S5={:.1f} "
        "tsolare={:.1f} tpdc={:.1f} delta_transfer={:.1f} delta_help={:.1f} on_thr={:.1f} off_thr={:.1f} state={}".format(
            s1, s2, s3, s4, s5,
            tsolare, tpdc, delta_transfer, help_delta, on_thresh, off_thresh, new_state
        )
    )


async def control_c2_task(sensor_mgr, actuator_mgr, input_mgr=None):
    print('[C2] controllo trasferimento solare avviato')
    while True:
        try:
            run_once(sensor_mgr, actuator_mgr, input_mgr)
        except Exception as e:
            print('[C2] exception:', e)
            actuator_mgr.set_relay('C2', config.SAFE_RELAY_STATE)
            state.c2_on_state = False
            try:
                _check_c2_feedback(input_mgr, False)
            except Exception:
                pass
        await asyncio.sleep(config.CONTROL_INTERVAL_S)
