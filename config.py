# config.py — ESP32 PLC 21 (Industrial Shields)
# Pin mapping verificato fin qui:
# - I2C su GPIO21/22
# - PCA9685 @ 0x40 per uscite
# - MCP23008 @ 0x21 per parte degli ingressi
# - I0.5 = GPIO27, I0.6 = GPIO26
#
# ATTENZIONE:
# pin_lib contiene il reverse engineering del dispatch I/O reale del PLC.
# Questo firmware usa PLC21IO come HAL condivisa.

BOARD_NAME = 'Industrial Shields ESP32 PLC 21'
USE_PLC21_IO = True

# ── Ethernet W5500 (SPI interno, pin fissi) ──────────────────────────────────
ETH_ENABLED = True
ETH_SPI_ID = 2
ETH_SPI_BAUD = 10_000_000
ETH_SCK = 18
ETH_MOSI = 23
ETH_MISO = 19
ETH_CS = 15
ETH_INT = 4
ETH_TIMEOUT_S = 10

ETH_USE_DHCP = False
ETH_STATIC_IP = '192.168.10.210'
ETH_NETMASK = '255.255.255.0'
ETH_GATEWAY = '192.168.10.1'
ETH_DNS = '192.168.10.1'

# ── MQTT ──────────────────────────────────────────────────────────────────────
MQTT_ENABLED = True
MQTT_BROKER = '192.168.10.20'
MQTT_PORT = 1883
MQTT_CLIENT_ID = 'esp32_centralina_acs'
MQTT_USER = 'relay_sensor'
MQTT_PASS = 'RelaySensor!2024'
MQTT_TOPIC_STATE = 'centralina/state'
MQTT_TOPIC_CMD = 'centralina/cmd'
MQTT_KEEPALIVE = 60
MQTT_QOS = 1

# ── I2C ───────────────────────────────────────────────────────────────────────
I2C_ID = 0
I2C_SDA = 21
I2C_SCL = 22
I2C_FREQ = 100_000

# ── PCA9685 @ 0x40 — uscite ──────────────────────────────────────────────────
# Mapping verificato:
#   Q0.0 → ch 11
#   Q0.1 → ch 10
#   Q0.2 → ch  9
#   Q0.3 → ch  8
#   Q0.4 → ch 12
#   A0.5 → ch 13 (PWM, B1 ON)
#   Q0.6 → ch  6
#   Q0.7 → ch  7
PCA9685_ADDR = 0x40
PCA9685_FREQ = 500

DO_C2_CH = 11
DO_CR_CH = 10
DO_P4_CH = 9
DO_P5_CH = 8
DO_VALVE_CH = 12

C1_PWM_OUTPUT = 'A0.5'
C1_PWM_CH = 13
C1_PWM_FREQ_HZ = 500

RELAY_OUTPUTS = {
    'C2': DO_C2_CH,
    'CR': DO_CR_CH,
    'P4': DO_P4_CH,
    'P5': DO_P5_CH,
    'VALVE': DO_VALVE_CH,
}

RELAY_PLC_OUTPUTS = {
    'C2': 'Q0.0',
    'CR': 'Q0.1',
    'P4': 'Q0.2',
    'P5': 'Q0.3',
    'VALVE': 'Q0.4',
}

# ── Ingressi applicativi del firmware ─────────────────────────────────────────
# Il dispatch hardware reale è demandato a pin_lib/plc21_io.py.
# Qui restano alias applicativi e compatibilità col firmware esistente.
MCP23008_ADDR = 0x21

MCP_INPUT_MAP = {
    'I0.0': 6,
    'I0.1': 4,
    'I0.2': 5,
    'I0.3': 3,
    'I0.4': 2,
}

DIRECT_INPUT_MAP = {
    'I0.5': 27,
    'I0.6': 26,
}

# Alias applicativi del firmware
INPUT_ALIASES = {
    'POOL_THERMOSTAT_CALL': 'I0.5',
    'HEAT_HELP_REQUEST': 'I0.6',
}

# Placeholder applicativi non fisici
UNMAPPED_INPUTS = (
    'FB_C2_NC',
)

INPUT_DEBOUNCE_MS = 50
INPUT_POLL_INTERVAL_S = 1

# Feedback relè C2 placeholder applicativo
C2_FB_NC_NAME = 'FB_C2_NC'
C2_FB_TIMEOUT_S = 1

# Alias retrocompatibilità per eventuali moduli vecchi
MCP_INPUT_ADDR = MCP23008_ADDR
PCA9685_OUTPUT_ADDR = PCA9685_ADDR

# ── DS2482 @ 0x18 — master 1-Wire ────────────────────────────────────────────
DS2482_ADDR = 0x18

# ── Sensori DS18B20 ───────────────────────────────────────────────────────────
SENSOR_LABELS = ('S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7')

ROM_MAP = {
    'S1': None,
    'S2': None,
    'S3': None,
    'S4': None,
    'S5': None,
    'S6': None,
    'S7': None,
}

AUTO_SCAN_ROM_ON_BOOT = False

# ── Logiche C1 ────────────────────────────────────────────────────────────────
# speed_pct = richiesta velocità umana 0..100
# wilo_duty_pct = duty finale PWM2 Wilo invertito
C1_DELTA_PWM_MIN = 2.0
C1_DELTA_PWM_MAX = 10.0
C1_SPEED_PCT_MIN = 10
C1_SPEED_PCT_MAX = 100
C1_THIGH_FULL = 70.0
C1_THIGH_STOP = 85.0
C1_STAGNATION_TEMP = 130.0
C1_STAGNATION_SPEED_PCT = 30
C1_STOP_HARD_TEMP = 85.0
C1_LATCH_RESET_TEMP = 70.0

# ── Logiche C2 ────────────────────────────────────────────────────────────────
C2_DELTA_ON = 5.0
C2_DELTA_OFF = 3.0
C2_HARD_STOP_TEMP = 85.0

# ── Logiche CR ────────────────────────────────────────────────────────────────
CR_TARGET_NORMAL = 45.0
CR_HYSTERESIS_NORMAL = 4.0
CR_TARGET_EMERG = 70.0
CR_HYSTERESIS_EMERG = 3.0
CR_EMERG_TEMP = 80.0

# ── Antilegionella ────────────────────────────────────────────────────────────
ANTILEGIONELLA_OK_SECONDS = 3600

# ── Setpoint configurabili ────────────────────────────────────────────────────
SETPOINTS_FILE = '/acs_setpoints.json'
SETPOINTS = {
    'solar_target_c': {
        'label': 'Target solare',
        'default': 55.0,
        'min': 20.0,
        'max': 95.0,
        'step': 0.5,
    },
    'pdc_target_c': {
        'label': 'Target boiler PDC',
        'default': 50.0,
        'min': 20.0,
        'max': 95.0,
        'step': 0.5,
    },
    'recirc_target_c': {
        'label': 'Target ricircolo',
        'default': 45.0,
        'min': 20.0,
        'max': 80.0,
        'step': 0.5,
    },
    'antileg_target_c': {
        'label': 'Target antilegionella',
        'default': 70.0,
        'min': 55.0,
        'max': 80.0,
        'step': 0.5,
    },
}

# ── Timing ────────────────────────────────────────────────────────────────────
SENSOR_INTERVAL_S = 5
CONTROL_INTERVAL_S = 5
SNAPSHOT_INTERVAL_S = 5

# ── Safe state ────────────────────────────────────────────────────────────────
SAFE_RELAY_STATE = False
SAFE_PWM_DUTY = 0

# ── Boot diagnostics ──────────────────────────────────────────────────────────
BOOT_TEST_OUTPUTS = False
BOOT_TEST_PWM = False