# config.py — ESP32 PLC 21 (Industrial Shields)
# Pin mapping verificato su manuale Rev.9 (Nov 2022)

BOARD_NAME = 'Industrial Shields ESP32 PLC 21'

# ── Ethernet W5500 (SPI interno, pin fissi dal manuale) ───────────────────────
ETH_ENABLED   = True
ETH_SPI_ID    = 2
ETH_SPI_BAUD  = 10_000_000
ETH_SCK       = 18    # GPIO18 - confermato manuale
ETH_MOSI      = 23    # GPIO23 - confermato manuale
ETH_MISO      = 19    # GPIO19 - confermato manuale
ETH_CS        = 15    # GPIO15 - CS W5500
ETH_INT       = 4     # GPIO4  - INT W5500
ETH_TIMEOUT_S = 10
ETH_USE_DHCP  = False
ETH_STATIC_IP = '192.168.10.210'
ETH_NETMASK   = '255.255.255.0'
ETH_GATEWAY   = '192.168.10.1'
ETH_DNS       = '192.168.10.1'

# ── MQTT ──────────────────────────────────────────────────────────────────────
MQTT_ENABLED        = True
MQTT_BROKER         = '192.168.10.20'
MQTT_PORT           = 1883
MQTT_CLIENT_ID      = 'esp32_centralina_acs'
MQTT_USER           = 'relay_sensor'
MQTT_PASS           = 'RelaySensor!2024'
MQTT_TOPIC_STATE    = 'centralina/state'
MQTT_TOPIC_CMD      = 'centralina/cmd'
MQTT_KEEPALIVE      = 60
MQTT_QOS            = 1

# ── I2C (pin fissi ESP32 PLC 21, confermati manuale) ─────────────────────────
I2C_ID   = 0
I2C_SDA  = 21    # GPIO21 - SDA
I2C_SCL  = 22    # GPIO22 - SCL
I2C_FREQ = 100_000

# Mappatura canali verificata su manuale Rev.9, tabella equivalenze I/O:
#   Q0.0 → ch 11   Q0.1 → ch 10   Q0.2 → ch 9    Q0.3 → ch 8
#   Q0.4 → ch 12   Q0.5 → ch 13 (PWM, richiede switch B1 ON)
#   Q0.6/A0.6 → ch 6   Q0.7/A0.7 → ch 7
PCA9685_ADDR = 0x40
PCA9685_FREQ = 1000   # Hz — relay digitali

DO_C2_CH   = 11   # Q0.0 → Pompa trasferimento solare -> PDC
DO_CR_CH   = 10   # Q0.1 → Pompa ricircolo
DO_P4_CH   =  9   # Q0.2 → Pompa 4 (piscina)
DO_P5_CH   =  8   # Q0.3 → spare
DO_VALVE_CH= 12   # Q0.4 → Valvola motorizzata fail-safe

# Uscita PWM per pompa Wilo C1 su Q0.5
# PLC 21 - zona B:
#   B1 ON  -> Q0.5 (digitale / PWM)
#   B1 OFF -> A0.5 (analogica 0-10V)
# Per questo progetto usare Q0.5, quindi B1 deve essere ON.
C1_PWM_OUTPUT = 'Q0.5'
C1_PWM_CH = 13   # dettaglio interno implementativo, non naming hardware
C1_PWM_FREQ_HZ = 1000

# ── Relay outputs — dizionario unico, usato da actuators.py e state.py ────────
RELAY_OUTPUTS = {
    'C2':    DO_C2_CH,    # Q0.0 - Pompa solare->PDC
    'CR':    DO_CR_CH,    # Q0.1 - Ricircolo
    'P4':    DO_P4_CH,    # Q0.2 - Piscina
    'P5':    DO_P5_CH,    # Q0.3 - spare
    'VALVE': DO_VALVE_CH, # Q0.4 - Valvola
}

# ── MCP23008 @ 0x21 — ingressi digitali isolati I0.0-I0.4 ────────────────────
# Pin MCP23008 per i0.x (da manuale, encoding: addr=0x21, pin=N)
#   I0.0 → pin 6   I0.1 → pin 4   I0.2 → pin 5
#   I0.3 → pin 3   I0.4 → pin 2
# I0.5 = GPIO27 (diretto), I0.6 = GPIO26 (diretto) — non isolati
MCP23008_ADDR = 0x21

INPUT_PINS = {
    'POOL_THERMOSTAT_CALL': 27,   # GPIO27 - ingresso diretto
    'HEAT_HELP_REQUEST': 26,      # GPIO26 - ingresso diretto
}
INPUT_DEBOUNCE_MS = 50

# ── DS2482 @ 0x18 — master 1-Wire per DS18B20 ────────────────────────────────
DS2482_ADDR = 0x18

# ── Sensori DS18B20 — ROM da sostituire dopo scan reale ──────────────────────
SENSOR_LABELS = ('S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7')

ROM_MAP = {
    'S1': None,   # Pannelli solari
    'S2': None,   # Boiler solare centro
    'S3': None,   # Boiler solare alto
    'S4': None,   # Boiler PDC alto
    'S5': None,   # Boiler PDC basso
    'S6': None,   # Collettore ricircolo in
    'S7': None,   # Collettore ricircolo out
}

AUTO_SCAN_ROM_ON_BOOT = False

# ── Logiche C1 (pompa Wilo PWM su Q0.5) ──────────────────────────────────────
C1_ON_DELTA       = 4.0    # ON se S1 >= Tavg_solare + 4
C1_OFF_DELTA      = 2.0    # OFF se S1 <= Tavg_solare + 2
C1_DELTA_PWM_MIN  = 2.0    # delta minimo per avviare (°C)
C1_DELTA_PWM_MAX  = 10.0   # delta per 100% duty (°C)
C1_PWM_MIN        = 10     # duty minimo % quando accesa
C1_PWM_MAX        = 100    # duty massimo %
C1_THIGH_FULL     = 70.0   # S4: sotto questa temp, nessuna riduzione potenza
C1_THIGH_STOP     = 85.0   # S4: sopra questa temp, duty scalato a 0
C1_STAGNATION_TEMP= 130.0  # S1: anti-stagnazione pannelli
C1_STAGNATION_SPEED_PCT = 30   # richiesta velocità % in override anti-stagnazione
C1_STOP_HARD_TEMP = 85.0   # S4 >= questo -> C1 si ferma (hard stop)
C1_LATCH_RESET_TEMP= 70.0  # S4 <= questo -> reset latch hard stop

# ── Logiche C2 (pompa trasferimento solare -> PDC) ────────────────────────────
C2_DELTA_ON       = 5.0    # delta solare > PDC per accendere (°C)
C2_DELTA_OFF      = 3.0    # delta solare > PDC per restare accesa (°C)
C2_HARD_STOP_TEMP = 85.0   # S4 >= questo -> C2 si ferma

# ── Logiche CR (ricircolo ACS) ────────────────────────────────────────────────
CR_TARGET_NORMAL  = 45.0   # setpoint normale (°C)
CR_HYSTERESIS_NORMAL = 4.0 # isteresi normale (°C)
CR_TARGET_EMERG   = 70.0   # setpoint emergenza/antilegionella (°C)
CR_HYSTERESIS_EMERG = 3.0  # isteresi emergenza (°C)
CR_EMERG_TEMP     = 80.0   # S4 >= questo -> modalità emergenza

# ── Antilegionella ────────────────────────────────────────────────────────────
ANTILEGIONELLA_OK_SECONDS = 3600

# ── Setpoint configurabili da portale ────────────────────────────────────────
SETPOINTS_FILE = '/acs_setpoints.json'
SETPOINTS = {
    'solar_target_c': {
        'label': 'Target solare', 'default': 55.0,
        'min': 20.0, 'max': 95.0, 'step': 0.5,
    },
    'pdc_target_c': {
        'label': 'Target boiler PDC', 'default': 50.0,
        'min': 20.0, 'max': 95.0, 'step': 0.5,
    },
    'recirc_target_c': {
        'label': 'Target ricircolo', 'default': 45.0,
        'min': 20.0, 'max': 80.0, 'step': 0.5,
    },
    'antileg_target_c': {
        'label': 'Target antilegionella', 'default': 70.0,
        'min': 55.0, 'max': 80.0, 'step': 0.5,
    },
}

# ── Timing ────────────────────────────────────────────────────────────────────
SENSOR_INTERVAL_S    = 5
CONTROL_INTERVAL_S   = 5
SNAPSHOT_INTERVAL_S  = 5
INPUT_POLL_INTERVAL_S= 1

# ── Safe state ────────────────────────────────────────────────────────────────
SAFE_RELAY_STATE = False
SAFE_PWM_DUTY    = 0

# ── Boot diagnostics ──────────────────────────────────────────────────────────
BOOT_TEST_OUTPUTS = False