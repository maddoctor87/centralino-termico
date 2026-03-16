# --- comms_mqtt.py ---
# MQTT: publish snapshot periodico + ricezione comando antilegionella.
# umqtt.simple è incluso nel firmware MicroPython ESP32 standard.

import uasyncio as asyncio
import ujson
import time
import config
import state

_client = None


def _connect():
    global _client
    from umqtt.simple import MQTTClient
    c = MQTTClient(
        config.MQTT_CLIENT_ID,
        config.MQTT_HOST,
        port=config.MQTT_PORT,
        user=config.MQTT_USER,
        password=config.MQTT_PASS,
        keepalive=config.MQTT_KEEPALIVE,
    )
    c.set_callback(_on_cmd)
    c.connect()
    c.subscribe(config.MQTT_TOPIC_CMD)
    _client = c
    print('[mqtt] connesso a {}:{}'.format(config.MQTT_BROKER, config.MQTT_PORT))


def _decode_msg(msg):
    if isinstance(msg, bytes):
        msg = msg.decode('utf-8')
    return ujson.loads(msg)


def _on_cmd(topic, msg):
    """Callback messaggi in ingresso da portale/server."""
    try:
        d = _decode_msg(msg)
        if not isinstance(d, dict):
            return

        if 'antileg_request' in d:
            state.antileg_request = bool(d['antileg_request'])
            print('[mqtt] antileg_request =', state.antileg_request)

        if 'manual_mode' in d:
            state.set_manual_mode(d['manual_mode'])
            print('[mqtt] manual_mode =', state.manual_mode)

        relay_cmd = d.get('relay')
        if isinstance(relay_cmd, dict):
            name = relay_cmd.get('name')
            value = relay_cmd.get('state')
            if name in config.RELAY_OUTPUTS:
                state.set_manual_relay(name, value)
                print('[mqtt] relay {} -> {}'.format(name, state.manual_relays[name]))

        pwm_cmd = d.get('pwm')
        if isinstance(pwm_cmd, dict) and 'duty' in pwm_cmd:
            state.set_manual_pwm_duty(pwm_cmd['duty'])
            print('[mqtt] pwm duty -> {}'.format(state.manual_pwm_duty))

        setpoint_cmd = d.get('setpoint')
        if isinstance(setpoint_cmd, dict):
            key = setpoint_cmd.get('key')
            value = setpoint_cmd.get('value')
            if key in config.SETPOINTS and value is not None:
                saved = state.set_setpoint(key, value)
                print('[mqtt] setpoint {} -> {}'.format(key, saved))

        tuning = d.get('tuning')
        if isinstance(tuning, dict):
            # Parametri di tuning (ad es. per C1 PWM)
            mapping = {
                'delta_pwm_min': 'C1_DELTA_PWM_MIN',
                'delta_pwm_max': 'C1_DELTA_PWM_MAX',
                'pwm_min': 'C1_PWM_MIN',
                'pwm_max': 'C1_PWM_MAX',
            }
            for key, cfg_name in mapping.items():
                if key in tuning:
                    try:
                        val = float(tuning[key])
                        setattr(config, cfg_name, val)
                        print('[mqtt] tuning {} -> {}'.format(cfg_name, val))
                    except Exception:
                        pass

        state.last_snapshot_ts = 0
    except Exception as e:
        print('[mqtt] on_cmd error:', e)


def publish_snapshot():
    global _client
    if _client is None:
        return
    try:
        payload = ujson.dumps(state.snapshot())
        _client.publish(config.MQTT_TOPIC_STATE, payload)
    except Exception as e:
        print('[mqtt] publish error:', e)
        _client = None  # forza riconnessione


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
            _client.check_msg()  # non bloccante
        except Exception as e:
            print('[mqtt] check_msg error:', e)
            _client = None

        now = time.time()
        if now - state.last_snapshot_ts >= config.MQTT_PUBLISH_INTERVAL_MS / 1000:
            publish_snapshot()
            state.last_snapshot_ts = now

        await asyncio.sleep(5)
