# plc21_map.py
# Mapping I/O per Industrial Shields ESP32 PLC 21+

MODEL_NAME = "ESP32 PLC 21+"

# Tipi supportati:
# - mcp
# - gpio
# - ads
# - pca_digital
# - pca_pwm

INPUTS = {
    # Digital isolated inputs
    "I0.0": {"type": "mcp",  "addr": 0x21, "index": 6},
    "I0.1": {"type": "mcp",  "addr": 0x21, "index": 4},
    "I0.2": {"type": "mcp",  "addr": 0x21, "index": 5},
    "I0.3": {"type": "mcp",  "addr": 0x21, "index": 3},
    "I0.4": {"type": "mcp",  "addr": 0x21, "index": 2},
    "I0.5": {"type": "gpio", "pin": 27},
    "I0.6": {"type": "gpio", "pin": 26},

    # Analog inputs
    "I0.7":  {"type": "ads", "addr": 0x49, "index": 2},
    "I0.8":  {"type": "ads", "addr": 0x49, "index": 3},
    "I0.9":  {"type": "ads", "addr": 0x48, "index": 3},
    "I0.10": {"type": "ads", "addr": 0x48, "index": 2},
    "I0.11": {"type": "ads", "addr": 0x48, "index": 1},
    "I0.12": {"type": "ads", "addr": 0x48, "index": 0},
}

OUTPUTS = {
    # Digital isolated outputs
    "Q0.0": {"type": "pca_digital", "addr": 0x40, "index": 11},
    "Q0.1": {"type": "pca_digital", "addr": 0x40, "index": 10},
    "Q0.2": {"type": "pca_digital", "addr": 0x40, "index": 9},
    "Q0.3": {"type": "pca_digital", "addr": 0x40, "index": 8},
    "Q0.4": {"type": "pca_digital", "addr": 0x40, "index": 12},

    # Digital / analog outputs
    "A0.5": {"type": "pca_pwm", "addr": 0x40, "index": 13},
    "A0.6": {"type": "pca_pwm", "addr": 0x40, "index": 6},
    "A0.7": {"type": "pca_pwm", "addr": 0x40, "index": 7},
}

ADS_DIGITAL_THRESHOLD = 1023
PCA_DEFAULT_ADDRS = (0x40,)
MCP_DEFAULT_ADDRS = (0x20, 0x21, 0x23)
ADS_DEFAULT_ADDRS = (0x48, 0x49)
