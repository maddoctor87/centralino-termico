# --- main.py ---
# Boot skeleton:
# - Ethernet
# - I2C / DS2482
# - attuatori
# - task controllo placeholder

import uasyncio as asyncio
import time
import sys
from machine import I2C, Pin

import config
import state
from actuators import ActuatorManager
from inputs import DigitalInputManager, input_task
from sensors import SensorManager, sensor_task
from control_panels import control_panels_task
from control_c2 import control_c2_task
from control_recirc import control_recirc_task
from control_block2_pool_heat_pdc import control_block2_task
from comms_mqtt import mqtt_task

_actuator_mgr = None
_plc_io = None
_eth_spi = None
_eth_cs = None
_eth_int = None
_eth_lan = None


async def eth_connect():
    global _eth_spi, _eth_cs, _eth_int, _eth_lan

    if not config.ETH_ENABLED:
        print('[eth] disabilitata da config')
        return False

    from machine import SPI
    import network

    _eth_spi = SPI(
        config.ETH_SPI_ID,
        baudrate=config.ETH_SPI_BAUD,
        sck=Pin(config.ETH_SCK),
        mosi=Pin(config.ETH_MOSI),
        miso=Pin(config.ETH_MISO),
    )
    _eth_cs = Pin(config.ETH_CS, Pin.OUT, value=1)
    _eth_int = Pin(config.ETH_INT, Pin.IN)

    try:
        _eth_lan = network.LAN(
            0,
            spi=_eth_spi,
            cs=_eth_cs,
            int=_eth_int,
            phy_type=network.PHY_W5500,
            phy_addr=0,
        )
        _eth_lan.active(True)

        if not config.ETH_USE_DHCP:
            _eth_lan.ifconfig((
                config.ETH_STATIC_IP,
                config.ETH_NETMASK,
                config.ETH_GATEWAY,
                config.ETH_DNS,
            ))
            print('[eth] static ip {}'.format(config.ETH_STATIC_IP))

        print('[eth] init W5500 ok, INT={}'.format(_eth_int.value()))

        deadline = time.ticks_add(time.ticks_ms(), config.ETH_TIMEOUT_S * 1000)
        while not _eth_lan.isconnected():
            if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                print('[eth] timeout link, continuo senza rete')
                return False
            await asyncio.sleep_ms(100)

        print('[eth] connesso:', _eth_lan.ifconfig()[0])
        return True
    except Exception as e:
        print('[eth] errore:', e)
        _eth_lan = None
        return False


async def main():
    global _actuator_mgr, _plc_io

    print('[boot] {} skeleton'.format(config.BOARD_NAME))
    await eth_connect()
    state.load_settings()

    i2c = I2C(
        config.I2C_ID,
        sda=Pin(config.I2C_SDA),
        scl=Pin(config.I2C_SCL),
        freq=config.I2C_FREQ,
    )
    print('[boot] I2C scan:', [hex(addr) for addr in i2c.scan()])

    if '/pin_lib' not in sys.path:
        sys.path.append('/pin_lib')
    from plc21_io import PLC21IO

    _plc_io = PLC21IO(i2c=i2c)
    print('[boot] HAL:', _plc_io.model_name())

    _actuator_mgr = ActuatorManager(i2c=i2c, plc_io=_plc_io)
    input_mgr = DigitalInputManager(i2c=i2c, plc_io=_plc_io)
    sensor_mgr = SensorManager(i2c)

    if config.AUTO_SCAN_ROM_ON_BOOT:
        sensor_mgr.scan()

    tasks = [
        asyncio.create_task(input_task(input_mgr)),
        asyncio.create_task(sensor_task(sensor_mgr)),
        asyncio.create_task(control_panels_task(sensor_mgr, _actuator_mgr)),
        asyncio.create_task(control_c2_task(sensor_mgr, _actuator_mgr, input_mgr)),
        asyncio.create_task(control_recirc_task(sensor_mgr, _actuator_mgr)),
        asyncio.create_task(control_block2_task(_actuator_mgr, input_mgr)),
    ]
    if config.MQTT_ENABLED:
        tasks.append(asyncio.create_task(mqtt_task()))

    print('[boot] skeleton avviato con {} task'.format(len(tasks)))
    while True:
        await asyncio.sleep(60)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    print('[boot] stop manuale')
except Exception as e:
    import sys
    sys.print_exception(e)
finally:
    if _actuator_mgr is not None:
        _actuator_mgr.all_off()
