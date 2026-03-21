# --- comms_mqtt.py ---
# MQTT: publish snapshot periodico + ricezione comandi da portale.

import uasyncio as asyncio
import ujson
import time

import config
import state

_client = None


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
            if not requested:
                state.antileg_hold_start = None
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
    global _client
    if _client is None:
        return
    try:
        _client.publish(config.MQTT_TOPIC_STATE, ujson.dumps(state.snapshot()))
    except Exception as e:
        print('[mqtt] publish error:', e)
        _client = None


async def mqtt_task():
    global _client
    while True:
        if _client is None:
            try:
                _connect()
            except Exception as e:
                print('[mqtt] connect error:', e)
                await asyncio.sleep(30)
                continue

        try:
            _client.check_msg()
        except Exception as e:
            print('[mqtt] check_msg error:', e)
            _client = None

        now = time.time()
        if now - state.last_snapshot_ts >= config.SNAPSHOT_INTERVAL_S:
            publish_snapshot()
            state.last_snapshot_ts = now

        await asyncio.sleep(1)
