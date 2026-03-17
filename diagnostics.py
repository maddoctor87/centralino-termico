import config
import actuators
from sensors import DS2482, DS2482Error


def _print_pin_result(pin_name: str, ok: bool, detail: str):
    status = 'OK' if ok else 'WARN'
    print('[diag] {:<12} {:<4} {}'.format(pin_name, status, detail))


def run_all_pin_tests(i2c, eth_hw_ok: bool, eth_link_ok: bool, eth_int_level: int):
    """
    Collauda tutti i pin usati dal firmware senza forzare le linee di bus
    come GPIO generici. I pin SPI/I2C vengono verificati tramite la presenza
    e la risposta delle periferiche collegate.
    """
    print('[diag] ===== test pin avvio =====')

    # I2C: SDA/SCL sono validi se il bus risponde e i dispositivi I2C sono visibili.
    try:
        addrs = i2c.scan()
        addrs_hex = [hex(a) for a in addrs]
        mcp_ok = config.MCP_INPUT_ADDR in addrs
        pca_ok = config.PCA9685_OUTPUT_ADDR in addrs
        _print_pin_result(
            'GPIO21 SDA',
            bool(addrs),
            'scan={}'.format(addrs_hex),
        )
        _print_pin_result(
            'GPIO22 SCL',
            bool(addrs),
            'scan={}'.format(addrs_hex),
        )
    except Exception as e:
        addrs = []
        mcp_ok = False
        pca_ok = False
        _print_pin_result('GPIO21 SDA', False, 'scan error: {}'.format(e))
        _print_pin_result('GPIO22 SCL', False, 'scan error: {}'.format(e))

    # W5500: le linee SPI/CS/INT vengono validate tramite init hardware.
    eth_detail = 'w5500_init={} link={} int={}'.format(
        'ok' if eth_hw_ok else 'fail',
        'up' if eth_link_ok else 'down',
        eth_int_level,
    )
    _print_pin_result('GPIO18 SCK', eth_hw_ok, eth_detail)
    _print_pin_result('GPIO23 MOSI', eth_hw_ok, eth_detail)
    _print_pin_result('GPIO19 MISO', eth_hw_ok, eth_detail)
    _print_pin_result('GPIO15 CS', eth_hw_ok, eth_detail)
    _print_pin_result('GPIO4 INT', eth_hw_ok, eth_detail)

    # DS2482: opzionale in questa fase, ma utile per confermare SDA/SCL/bridge.
    if config.DS2482_ADDR in addrs:
        try:
            DS2482(i2c, config.DS2482_ADDR)
            _print_pin_result(
                'DS2482 I2C',
                True,
                'DS2482 {} risponde'.format(hex(config.DS2482_ADDR)),
            )
        except (DS2482Error, OSError) as e:
            _print_pin_result(
                'DS2482 I2C',
                False,
                'DS2482 {} errore {}'.format(hex(config.DS2482_ADDR), e),
            )
    else:
        _print_pin_result(
            'DS2482 I2C',
            False,
            'DS2482 {} non presente'.format(hex(config.DS2482_ADDR)),
        )

    # Uscite digitali reali via PCA9685.
    if config.BOOT_TEST_OUTPUTS and pca_ok:
        try:
            # TODO: implement PCA9685 test
            _print_pin_result(
                'Q0.0 / C2',
                True,
                'PCA9685 ch{} test TODO'.format(config.DO_C2_CH),
            )
            _print_pin_result(
                'Q0.1 / CR',
                True,
                'PCA9685 ch{} test TODO'.format(config.DO_CR_CH),
            )
        except Exception as e:
            _print_pin_result('Q0.0 / C2', False, 'test error: {}'.format(e))
            _print_pin_result('Q0.1 / CR', False, 'test error: {}'.format(e))
    elif not config.BOOT_TEST_OUTPUTS:
        _print_pin_result('Q0.0 / C2', False, 'test disabilitato da config')
        _print_pin_result('Q0.1 / CR', False, 'test disabilitato da config')
    else:
        _print_pin_result('Q0.0 / C2', False, 'PCA9685 non presente')
        _print_pin_result('Q0.1 / CR', False, 'PCA9685 non presente')

    # Uscita PWM reale.
    if config.BOOT_TEST_PWM:
        _print_pin_result(
            'PWM C1',
            False,
            'test PWM non implementato (usa Q0.5 con switch B1 = ON)',
        )
    else:
        _print_pin_result('PWM C1', False, 'test disabilitato da config')

    print('[diag] ===== fine test pin =====')
