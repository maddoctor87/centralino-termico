# --- ota_update.py ---
# OTA firmware nativo ESP32 / MicroPython su partizioni app.

import gc
import time

import uasyncio as asyncio

import config
import state

try:
    import esp32
except ImportError:
    esp32 = None

try:
    import machine
except ImportError:
    machine = None

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib

try:
    import usocket as socket
except ImportError:
    import socket


def _push_snapshot():
    try:
        import comms_mqtt

        state.last_snapshot_ts = 0
        comms_mqtt.publish_snapshot()
    except Exception:
        pass


def _ota_supported():
    if esp32 is None or not hasattr(esp32, 'Partition'):
        return False, 'esp32.Partition non disponibile'
    try:
        running = esp32.Partition(esp32.Partition.RUNNING)
        update = running.get_next_update()
        return True, (running, update)
    except Exception as e:
        return False, str(e)


def _partition_label(part):
    try:
        info = part.info()
        if len(info) >= 5:
            label = info[4]
            if isinstance(label, bytes):
                label = label.decode('utf-8')
            return str(label or '')
    except Exception:
        pass
    return ''


def _partition_size(part):
    try:
        info = part.info()
        if len(info) >= 4:
            return int(info[3])
    except Exception:
        pass
    return 0


def _oserr_code(err):
    args = getattr(err, 'args', ())
    if args:
        try:
            return int(args[0])
        except Exception:
            pass
    return None


def _parse_url(url):
    text = str(url or '').strip()
    scheme, sep, rest = text.partition('://')
    if not sep or scheme.lower() != 'http':
        raise ValueError('OTA supporta solo URL http://')
    host_port, slash, tail = rest.partition('/')
    if not host_port:
        raise ValueError('host OTA non valido')
    host, colon, port_text = host_port.partition(':')
    port = int(port_text) if colon and port_text else 80
    path = '/' + tail if slash else '/'
    return host, port, path


def _recv_exact(sock, size):
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise OSError('body OTA troncato')
        buf.extend(chunk)
    return bytes(buf)


def _recv_line(sock):
    buf = bytearray()
    while True:
        chunk = sock.recv(1)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) >= 2 and buf[-2:] == b'\r\n':
            break
    return bytes(buf)


def _http_open(url, headers=None, redirects=1):
    host, port, path = _parse_url(url)
    addr = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][-1]
    sock = socket.socket()
    sock.settimeout(getattr(config, 'OTA_HTTP_TIMEOUT_S', 15))
    sock.connect(addr)

    host_header = host if port == 80 else '{}:{}'.format(host, port)
    lines = [
        'GET {} HTTP/1.0'.format(path),
        'Host: {}'.format(host_header),
        'Connection: close',
    ]
    for key, value in (headers or {}).items():
        lines.append('{}: {}'.format(str(key), str(value)))
    request = '\r\n'.join(lines) + '\r\n\r\n'
    sock.send(request.encode('utf-8'))

    status_line = _recv_line(sock)
    if not status_line:
        sock.close()
        raise OSError('risposta OTA assente')
    parts = status_line.decode('utf-8').strip().split()
    if len(parts) < 2:
        sock.close()
        raise OSError('status OTA non valido')
    status = int(parts[1])

    response_headers = {}
    while True:
        line = _recv_line(sock)
        if line in (b'', b'\r\n'):
            break
        text = line.decode('utf-8').strip()
        if ':' not in text:
            continue
        key, value = text.split(':', 1)
        response_headers[key.strip().lower()] = value.strip()

    if status in (301, 302, 303, 307, 308) and redirects > 0:
        location = response_headers.get('location')
        sock.close()
        if not location:
            raise OSError('redirect OTA senza location')
        return _http_open(location, headers=headers, redirects=redirects - 1)

    return sock, status, response_headers


def _iter_http_body(sock, response_headers, chunk_size):
    transfer_encoding = str(response_headers.get('transfer-encoding') or '').lower()
    if 'chunked' in transfer_encoding:
        while True:
            line = _recv_line(sock)
            if not line:
                raise OSError('chunk OTA mancante')
            size_text = line.decode('utf-8').split(';', 1)[0].strip()
            chunk_len = int(size_text or '0', 16)
            if chunk_len <= 0:
                while True:
                    trailer = _recv_line(sock)
                    if trailer in (b'', b'\r\n'):
                        return
                return
            payload = _recv_exact(sock, chunk_len)
            crlf = _recv_exact(sock, 2)
            if crlf != b'\r\n':
                raise OSError('chunk OTA non valido')
            yield payload
        return

    remaining = response_headers.get('content-length')
    if remaining is not None:
        remaining = int(remaining)
        while remaining > 0:
            payload = sock.recv(min(chunk_size, remaining))
            if not payload:
                raise OSError('body OTA troncato')
            remaining -= len(payload)
            yield payload
        return

    while True:
        payload = sock.recv(chunk_size)
        if not payload:
            return
        yield payload


def _download_json(url, headers=None):
    sock = None
    try:
        sock, status, response_headers = _http_open(url, headers=headers)
        if status != 200:
            raise OSError('HTTP {}'.format(status))
        chunks = []
        for chunk in _iter_http_body(
            sock,
            response_headers,
            getattr(config, 'OTA_DOWNLOAD_CHUNK_SIZE', 1024),
        ):
            chunks.append(chunk)
        payload = b''.join(chunks)
        try:
            import ujson as json
        except ImportError:
            import json
        return json.loads(payload.decode('utf-8'))
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def _load_manifest(request):
    if isinstance(request, dict) and isinstance(request.get('manifest'), dict):
        return dict(request.get('manifest'))

    manifest_url = request.get('manifest_url')
    if not manifest_url:
        raise ValueError('manifest_url mancante')
    headers = request.get('headers') if isinstance(request.get('headers'), dict) else {}
    manifest = _download_json(manifest_url, headers=headers)
    if not isinstance(manifest, dict):
        raise ValueError('manifest OTA non valido')
    if 'version' not in manifest and request.get('target_version'):
        manifest['version'] = request.get('target_version')
    return manifest


def _validate_manifest(manifest):
    firmware_url = str(manifest.get('firmware') or '').strip()
    if not firmware_url:
        raise ValueError('manifest OTA senza firmware')

    length = manifest.get('length')
    if length not in (None, ''):
        length = int(length)
        if length <= 0:
            raise ValueError('length OTA non valida')
    else:
        length = None

    sha1 = manifest.get('sha1')
    if sha1 not in (None, ''):
        sha1 = str(sha1).strip().lower()
        if len(sha1) != 40:
            raise ValueError('sha1 OTA non valido')
    else:
        sha1 = None

    return {
        'version': str(manifest.get('version') or 'ota-manual'),
        'firmware': firmware_url,
        'length': length,
        'sha1': sha1,
    }


class _PartitionWriter:
    def __init__(self, part, *, block_size, max_size):
        self.part = part
        self.block_size = int(block_size)
        self.max_size = int(max_size)
        self.block_num = 0
        self.total = 0
        self.buffer_len = 0
        self.buffer = bytearray(self.block_size)

    def write(self, data):
        if not data:
            return
        if self.total + len(data) > self.max_size:
            raise OSError('firmware troppo grande per la partizione OTA')

        mv = memoryview(data)
        while len(mv):
            take = min(self.block_size - self.buffer_len, len(mv))
            self.buffer[self.buffer_len:self.buffer_len + take] = mv[:take]
            self.buffer_len += take
            self.total += take
            mv = mv[take:]
            if self.buffer_len == self.block_size:
                self.part.writeblocks(self.block_num, self.buffer)
                self.block_num += 1
                self.buffer_len = 0

    def finalize(self):
        if self.buffer_len <= 0:
            return
        self.buffer[self.buffer_len:] = b'\xff' * (self.block_size - self.buffer_len)
        self.part.writeblocks(self.block_num, self.buffer)
        self.block_num += 1
        self.buffer_len = 0


async def _stream_firmware_to_partition(url, headers, part, *, expected_sha1=None, expected_length=None, target_label=''):
    sock = None
    try:
        sock, status, response_headers = _http_open(url, headers=headers)
        if status != 200:
            raise OSError('HTTP {}'.format(status))

        header_length = response_headers.get('content-length')
        total_bytes = int(header_length) if header_length else None
        if expected_length is not None and total_bytes is not None and total_bytes != expected_length:
            raise OSError('size OTA mismatch')
        if total_bytes is None:
            total_bytes = expected_length

        part_size = _partition_size(part)
        if total_bytes is not None and total_bytes > part_size:
            raise OSError('firmware troppo grande per {}'.format(target_label or 'partizione OTA'))

        writer = _PartitionWriter(
            part,
            block_size=max(512, int(getattr(config, 'OTA_PARTITION_BLOCK_SIZE', 4096))),
            max_size=part_size,
        )
        sha_ctx = hashlib.sha1()
        chunk_size = max(256, int(getattr(config, 'OTA_DOWNLOAD_CHUNK_SIZE', 1024)))
        downloaded = 0
        last_push_ms = time.ticks_ms()

        for chunk in _iter_http_body(sock, response_headers, chunk_size):
            writer.write(chunk)
            sha_ctx.update(chunk)
            downloaded += len(chunk)
            now_ms = time.ticks_ms()
            if (
                time.ticks_diff(now_ms, last_push_ms) >= 1000
                or (total_bytes is not None and downloaded == total_bytes)
            ):
                state.set_ota_status(
                    'writing',
                    message='Scrivo firmware su {}'.format(target_label or 'partizione OTA'),
                    bytes_written=downloaded,
                    total_bytes=total_bytes or downloaded,
                )
                _push_snapshot()
                last_push_ms = now_ms
            if downloaded and (downloaded % (chunk_size * 8)) == 0:
                gc.collect()
            await asyncio.sleep_ms(0)

        writer.finalize()
        if expected_length is not None and downloaded != expected_length:
            raise OSError('size OTA incompleta')

        if expected_sha1:
            actual_sha1 = ''.join('{:02x}'.format(byte) for byte in sha_ctx.digest())
            if actual_sha1 != expected_sha1:
                raise OSError('sha1 OTA mismatch')

        state.set_ota_status(
            'writing',
            bytes_written=downloaded,
            total_bytes=total_bytes or downloaded,
        )
        _push_snapshot()
        return downloaded
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def ota_boot_confirm():
    state.refresh_ota_runtime_info()
    supported, info = _ota_supported()
    if not supported:
        state.set_ota_status(
            'idle',
            enabled=False,
            message='OTA nativo non disponibile: {}'.format(info),
            manifest_url=None,
            firmware_url=None,
            bytes_written=0,
            total_bytes=0,
            started_at=None,
        )
        return False

    running_part, _update_part = info
    running_label = _partition_label(running_part)
    current_version = state.ota.get('current_version')
    pending_reboot = state.ota.get('last_result') == 'pending_reboot'
    target_partition = state.ota.get('target_partition')
    target_version = state.ota.get('target_version')

    if pending_reboot:
        boot_ok = bool(
            (target_partition and running_label == target_partition)
            or (not target_partition and target_version and target_version == current_version)
        )
        if boot_ok:
            state.set_ota_status(
                'idle',
                persist=True,
                enabled=True,
                message='Firmware OTA avviato correttamente',
                last_error=None,
                last_result='success',
                last_success_version=target_version or current_version,
                last_success_partition=running_label or target_partition,
                finished_at=time.time(),
                manifest_url=None,
                firmware_url=None,
                bytes_written=0,
                total_bytes=0,
                started_at=None,
            )
        else:
            state.set_ota_status(
                'idle',
                persist=True,
                enabled=True,
                message='Riavvio completato ma la partizione OTA non e attiva',
                last_error='partizione attiva {}'.format(running_label or '?'),
                last_result='rollback',
                finished_at=time.time(),
                manifest_url=None,
                firmware_url=None,
                bytes_written=0,
                total_bytes=0,
                started_at=None,
            )
    else:
        state.set_ota_status(
            'idle',
            enabled=True,
            manifest_url=None,
            firmware_url=None,
            bytes_written=0,
            total_bytes=0,
            started_at=None,
        )

    try:
        esp32.Partition.mark_app_valid_cancel_rollback()
    except OSError as e:
        if _oserr_code(e) != -261:
            print('[ota] boot confirm error:', e)
    except Exception as e:
        print('[ota] boot confirm error:', e)
    return True


async def _perform_ota(request):
    if not getattr(config, 'OTA_ENABLED', False):
        raise RuntimeError('OTA disabilitato in config')

    supported, info = _ota_supported()
    if not supported:
        raise RuntimeError('OTA nativo non disponibile: {}'.format(info))

    request = dict(request or {})
    manifest_url = request.get('manifest_url')
    state.set_ota_status(
        'downloading',
        enabled=True,
        message='Scarico manifest OTA',
        manifest_url=manifest_url,
        firmware_url=None,
        target_partition=None,
        bytes_written=0,
        total_bytes=0,
        started_at=time.time(),
        finished_at=None,
        last_error=None,
        last_result=None,
    )
    _push_snapshot()

    manifest = _validate_manifest(_load_manifest(request))
    target_version = str(manifest.get('version') or 'ota-manual')
    current_version = str(state.ota.get('current_version') or '')
    force = bool(request.get('force'))

    if not force and target_version and target_version == current_version:
        state.set_ota_status(
            'idle',
            persist=True,
            message='Firmware già aggiornato',
            target_version=target_version,
            target_partition=state.ota.get('current_partition'),
            firmware_url=manifest.get('firmware'),
            last_error=None,
            last_result='skipped',
            finished_at=time.time(),
            bytes_written=0,
            total_bytes=manifest.get('length') or 0,
        )
        _push_snapshot()
        return

    _running_part, update_part = info
    target_label = _partition_label(update_part)
    firmware_url = manifest['firmware']
    headers = request.get('headers') if isinstance(request.get('headers'), dict) else {}

    state.set_ota_status(
        'downloading',
        message='Scarico firmware {}'.format(target_version),
        target_version=target_version,
        firmware_url=firmware_url,
        target_partition=target_label,
        bytes_written=0,
        total_bytes=manifest.get('length') or 0,
    )
    _push_snapshot()

    try:
        written = await _stream_firmware_to_partition(
            firmware_url,
            headers,
            update_part,
            expected_sha1=manifest.get('sha1'),
            expected_length=manifest.get('length'),
            target_label=target_label,
        )

        state.set_ota_status(
            'applying',
            message='Attivo partizione {}'.format(target_label or 'OTA'),
            bytes_written=written,
            total_bytes=manifest.get('length') or written,
        )
        _push_snapshot()
        update_part.set_boot()

        state.set_ota_status(
            'restarting',
            persist=True,
            message='Firmware scritto in {}, riavvio PLC'.format(target_label or 'partizione OTA'),
            current_partition=state.ota.get('current_partition'),
            target_version=target_version,
            target_partition=target_label,
            firmware_url=firmware_url,
            last_result='pending_reboot',
            last_error=None,
            finished_at=time.time(),
            bytes_written=written,
            total_bytes=manifest.get('length') or written,
        )
        _push_snapshot()

        time.sleep(max(1, int(getattr(config, 'OTA_RESET_DELAY_S', 2))))
        if machine is None or not hasattr(machine, 'reset'):
            raise RuntimeError('machine.reset non disponibile')
        machine.reset()
    except Exception as e:
        state.set_ota_status(
            'error',
            persist=True,
            message='OTA fallito',
            last_error=str(e),
            last_result='error',
            finished_at=time.time(),
        )
        _push_snapshot()
        raise


async def ota_task():
    print('[ota] task OTA avviato')
    while True:
        request = state.pop_ota_request()
        if request is not None:
            try:
                await _perform_ota(request)
            except Exception as e:
                print('[ota] error:', e)
        await asyncio.sleep(1)
