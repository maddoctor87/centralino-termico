from micropython import const

import config
from sensors import DS2482, DS2482Error


_PCA_LED0_BASE = const(0x06)


def _print_pin_result(pin_name: str, ok: bool, detail: str):
    status = 'OK' if ok else 'WARN'
    print('[diag] {:<12} {:<4} {}'.format(pin_name, status, detail))


def _read_pca_ch_regs(i2c, addr, ch):
    base = _PCA_LED0_BASE + 4 * int(ch)
    return [i2c.readfrom_mem(addr, base + i, 1)[0] for i in range(4)]


def run_all_pin_tests(i2c, eth_hw_ok: bool, eth_link_ok: bool, eth_int_level: int):
    """
    Collauda i pin usati dal firmware senza forzare le linee di bus come GPIO generici.
    I pin SPI/I2C vengono verificati tramite la presenza e la risposta delle periferiche collegate.

    Nota:
    - non forza relè/uscite per default
    - quando BOOT_TEST_OUTPUTS=True fa solo una lettura registri PCA9685,
      senza commutare carichi
    """
    print('[diag] ===== test pin avvio =====')

    try:
        addrs = i2c.scan()
        addrs_hex = [hex(a) for a in addrs]
        mcp_ok = config.MCP23008_ADDR in addrs
        pca_ok = config.PCA9685_ADDR in addrs

        _print_pin_result('GPIO21 SDA', bool(addrs), 'scan={}'.format(addrs_hex))
        _print_pin_result('GPIO22 SCL', bool(addrs), 'scan={}'.format(addrs_hex))
        _print_pin_result('MCP23008', mcp_ok, 'addr={}'.format(hex(config.MCP23008_ADDR)))
        _print_pin_result('PCA9685', pca_ok, 'addr={}'.format(hex(config.PCA9685_ADDR)))

    except Exception as e:
        addrs = []
        mcp_ok = False
        pca_ok = False
        _print_pin_result('GPIO21 SDA', False, 'scan error: {}'.format(e))
        _print_pin_result('GPIO22 SCL', False, 'scan error: {}'.format(e))
        _print_pin_result('MCP23008', False, 'scan error')
        _print_pin_result('PCA9685', False, 'scan error')

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

    if config.BOOT_TEST_OUTPUTS and pca_ok:
        try:
            c2_regs = _read_pca_ch_regs(i2c, config.PCA9685_ADDR, config.DO_C2_CH)
            cr_regs = _read_pca_ch_regs(i2c, config.PCA9685_ADDR, config.DO_CR_CH)
            _print_pin_result('Q0.0 / C2', True, 'PCA9685 ch{} regs={}'.format(config.DO_C2_CH, [hex(x) for x in c2_regs]))
            _print_pin_result('Q0.1 / CR', True, 'PCA9685 ch{} regs={}'.format(config.DO_CR_CH, [hex(x) for x in cr_regs]))
        except Exception as e:
            _print_pin_result('Q0.0 / C2', False, 'test error: {}'.format(e))
            _print_pin_result('Q0.1 / CR', False, 'test error: {}'.format(e))
    elif not config.BOOT_TEST_OUTPUTS:
        _print_pin_result('Q0.0 / C2', False, 'test disabilitato da config')
        _print_pin_result('Q0.1 / CR', False, 'test disabilitato da config')
    else:
        _print_pin_result('Q0.0 / C2', False, 'PCA9685 non presente')
        _print_pin_result('Q0.1 / CR', False, 'PCA9685 non presente')

    if getattr(config, 'BOOT_TEST_PWM', False):
        _print_pin_result(
            'PWM C1',
            True if pca_ok else False,
            'usa PCA9685 ch{} (solo lettura/config)'.format(config.C1_PWM_CH),
        )
    else:
        _print_pin_result('PWM C1', False, 'test disabilitato da config')

    print('[diag] ===== fine test pin =====')