# --- comms_mqtt.py ---
# MQTT: publish snapshot periodico + ricezione comandi da portale.

import uasyncio as asyncio
import ujson
import time

import config
import state

_client = None
_connect_fail_count = 0
_connect_fail_since = None


def _startup_delay_s():
    try:
        return max(0, int(getattr(config, 'MQTT_STARTUP_DELAY_S', 0)))
    except Exception:
        return 0


def _reconnect_delay_s():
    try:
        return max(1, int(getattr(config, 'MQTT_RECONNECT_DELAY_S', 5)))
    except Exception:
        return 5


def _reset_min_failures():
    try:
        return max(1, int(getattr(config, 'MQTT_RESET_MIN_FAILURES', 6)))
    except Exception:
        return 6


def _reset_after_s():
    try:
        return max(10, int(getattr(config, 'MQTT_RESET_AFTER_S', 180)))
    except Exception:
        return 180


def _socket_test_timeout_s():
    try:
        return max(1, int(getattr(config, 'MQTT_SOCKET_TEST_TIMEOUT_S', 2)))
    except Exception:
        return 2


def _clear_failures():
    global _connect_fail_count, _connect_fail_since
    _connect_fail_count = 0
    _connect_fail_since = None


def _disconnect_client():
    global _client
    client = _client
    _client = None
    if client is None:
        return
    try:
        client.disconnect()
    except Exception:
        pass


def _broker_reachable():
    try:
        import usocket as socket
    except ImportError:
        import socket

    sock = None
    try:
        addr = socket.getaddrinfo(config.MQTT_BROKER, config.MQTT_PORT)[0][-1]
        sock = socket.socket()
        sock.settimeout(_socket_test_timeout_s())
        sock.connect(addr)
        return True
    except Exception:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _handle_failure(label, err):
    global _connect_fail_count, _connect_fail_since

    print('[mqtt] {}: {}'.format(label, err))
    _disconnect_client()

    now = time.time()
    if _connect_fail_since is None:
        _connect_fail_since = now
    _connect_fail_count += 1

    if _connect_fail_count < _reset_min_failures():
        return
    if (now - _connect_fail_since) < _reset_after_s():
        return
    if not _broker_reachable():
        print('[mqtt] broker irraggiungibile, salto reset automatico')
        return

    print('[mqtt] reset automatico: mqtt bloccato con broker raggiungibile')
    try:
        from machine import reset
        time.sleep(1)
        reset()
    except Exception as reset_err:
        print('[mqtt] reset automatico fallito:', reset_err)


def _topic_text(topic):
    if isinstance(topic, bytes):
        return topic.decode('utf-8')
    return str(topic)


def _remote_temp_topics():
    if not getattr(config, 'MQTT_TEMP_FALLBACK_ENABLED', False):
        return ()
    topics = getattr(config, 'MQTT_TEMP_FALLBACK_TOPICS', ())
    if isinstance(topics, (bytes, str)):
        topics = (topics,)
    return tuple(str(topic) for topic in topics if topic)


def _connect():
    global _client
    from umqtt.simple import MQTTClient
    c = MQTTClient(
        config.MQTT_CLIENT_ID,
        config.MQTT_BROKER,
        port=config.MQTT_PORT,
        user=config.MQTT_USER,
        password=config.MQTT_PASS,
        keepalive=config.MQTT_KEEPALIVE,
    )
    c.set_callback(_on_cmd)
    c.connect()
    c.subscribe(config.MQTT_TOPIC_CMD)
    for topic in _remote_temp_topics():
        c.subscribe(topic)
    _client = c
    _clear_failures()
    state.last_snapshot_ts = 0
    print('[mqtt] connesso a {}:{}'.format(config.MQTT_BROKER, config.MQTT_PORT))


def _decode_msg(msg):
    if isinstance(msg, bytes):
        msg = msg.decode('utf-8')
    return ujson.loads(msg)


def _on_remote_temps(topic, msg):
    try:
        d = _decode_msg(msg)
        if not isinstance(d, dict):
            return

        temps = d.get('temps')
        if not isinstance(temps, dict):
            return

        state.set_all_temps(
            temps,
            source='remote',
            payload_ts=d.get('ts'),
            topic=topic,
        )
    except Exception as e:
        print('[mqtt] remote temps error:', e)


def _on_cmd(topic, msg):
    topic = _topic_text(topic)
    if topic != config.MQTT_TOPIC_CMD:
        if topic in _remote_temp_topics():
            _on_remote_temps(topic, msg)
        return

    try:
        d = _decode_msg(msg)
        if not isinstance(d, dict):
            return

        if 'antileg_request' in d:
            requested = bool(d['antileg_request'])
            if requested and not state.antileg_request:
                state.antileg_ok = False
                state.antileg_hold_start = None
                state.antileg_hold_elapsed_s = 0
                state.antileg_phase = 'heat_boiler'
            if not requested:
                state.antileg_hold_start = None
                state.antileg_hold_elapsed_s = 0
                state.antileg_phase = 'idle'
            state.antileg_request = requested
            print('[mqtt] antileg_request =', state.antileg_request)

        if 'manual_mode' in d:
            state.set_manual_mode(d['manual_mode'])
            print('[mqtt] manual_mode =', state.manual_mode)

        if 'pool_just_filled' in d:
            state.set_pool_just_filled(d['pool_just_filled'])
            print('[mqtt] pool_just_filled =', state.get_pool_just_filled())

        relay_cmd = d.get('relay')
        if isinstance(relay_cmd, dict):
            name  = relay_cmd.get('name')
            value = relay_cmd.get('state')
            if name in config.RELAY_OUTPUTS:
                state.set_manual_relay(name, value)
                print('[mqtt] relay {} -> {}'.format(name, state.manual_relays[name]))

        pwm_cmd = d.get('pwm')
        if isinstance(pwm_cmd, dict) and 'duty' in pwm_cmd:
            state.set_manual_c1_wilo_duty_pct(pwm_cmd['duty'])
            print('[mqtt] c1 wilo duty cmd -> {}'.format(state.manual_c1_wilo_duty_pct))

        setpoint_cmd = d.get('setpoint')
        if isinstance(setpoint_cmd, dict):
            key   = setpoint_cmd.get('key')
            value = setpoint_cmd.get('value')
            if key in config.SETPOINTS and value is not None:
                try:
                    state.setpoints[key] = state._normalize_setpoint(key, value)
                    state.save_settings()
                    print('[mqtt] setpoint {} -> {}'.format(key, state.setpoints[key]))
                except Exception as e:
                    print('[mqtt] setpoint error:', e)

        tuning = d.get('tuning')
        if isinstance(tuning, dict):
            mapping = {
                'delta_pwm_min': 'C1_DELTA_PWM_MIN',
                'delta_pwm_max': 'C1_DELTA_PWM_MAX',
                'speed_pct_min': 'C1_SPEED_PCT_MIN',
                'speed_pct_max': 'C1_SPEED_PCT_MAX',
                'pwm_min':       'C1_SPEED_PCT_MIN',
                'pwm_max':       'C1_SPEED_PCT_MAX',
            }
            for key, cfg_name in mapping.items():
                if key in tuning:
                    try:
                        setattr(config, cfg_name, float(tuning[key]))
                        print('[mqtt] tuning {} -> {}'.format(cfg_name, getattr(config, cfg_name)))
                    except Exception:
                        pass

        state.last_snapshot_ts = 0  # forza publish immediato
    except Exception as e:
        print('[mqtt] on_cmd error:', e)


def publish_snapshot():
    if _client is None:
        return False
    try:
        _client.publish(config.MQTT_TOPIC_STATE, ujson.dumps(state.snapshot()))
        return True
    except Exception as e:
        _handle_failure('publish error', e)
        return False


async def mqtt_task():
    startup_delay = _startup_delay_s()
    if startup_delay:
        await asyncio.sleep(startup_delay)

    while True:
        if _client is None:
            try:
                _connect()
            except Exception as e:
                _handle_failure('connect error', e)
                await asyncio.sleep(_reconnect_delay_s())
                continue

        try:
            _client.check_msg()
        except Exception as e:
            _handle_failure('check_msg error', e)

        now = time.time()
        if now - state.last_snapshot_ts >= config.SNAPSHOT_INTERVAL_S:
            if publish_snapshot():
                state.last_snapshot_ts = now

        await asyncio.sleep(1)
