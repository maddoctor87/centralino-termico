DEVICE_ID = "esp32_sonde_temp_1"
MQTT_CLIENT_ID = DEVICE_ID

WIFI_SSID = "ITINERIS-Resort"
WIFI_PASSWORD = ""

# Allineato al broker MQTT del progetto su sstit / rete centrale termica.
MQTT_BROKER = "192.168.10.20"
MQTT_PORT = 1883
MQTT_USER = "relay_sensor"
MQTT_PASS = "RelaySensor!2024"
MQTT_PASSWORD = MQTT_PASS
MQTT_KEEPALIVE = 60
MQTT_QOS = 1

MQTT_TOPIC_STATE = "centralina/sonde/{}/state".format(DEVICE_ID)
MQTT_TOPIC_STATUS = "centralina/sonde/{}/status".format(DEVICE_ID)

# Alias legacy: il firmware vecchio usava "TEMP", qui lo riallineiamo a "STATE".
MQTT_TOPIC_TEMP = MQTT_TOPIC_STATE

ONEWIRE_GPIO = 4
READ_INTERVAL_SEC = 15
CONVERSION_WAIT_MS = 750
WIFI_CONNECT_TIMEOUT_S = 20
WIFI_RETRY_INTERVAL_S = 30

# Etichette logiche del progetto centrale termica.
SENSOR_LABELS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
SENSOR_DESCRIPTIONS = {
    "S1": "pannelli solari",
    "S2": "centro boiler solare",
    "S3": "alto boiler solare",
    "S4": "alto boiler PDC",
    "S5": "basso boiler PDC",
    "S6": "collettore ricircolo ingresso",
    "S7": "collettore ricircolo fine",
}

# Popolare dopo il primo scan MQTT: chiave ROM hex lowercase -> label S1..S7.
ROM_LABELS = {
    # "28ff641d7216035c": "S1",
    "280333bb00000088":"S1",
    "28f88cbc000000a4":"S3",
    "284affba000000e7":"S2",
    "286ceebe0000005c":"S4",
    "28b470bb000000ad":"S5",
    "28fbebbc000000bd":"S6",
    "28a28abf00000051":"S7",
}
