"""
Fake-TLS MTProxy соединение для Telethon.

MTProto прокси с ee-префиксом используют обфускацию «Fake-TLS»:
  1. Клиент отправляет фейковый TLS 1.0 ClientHello
     (session_id = HMAC-SHA256(secret, client_random))
  2. Сервер отвечает ServerHello + ChangeCipherSpec + ApplicationData
  3. Все последующие данные обёртываются в TLS ApplicationData записи
     (17 03 03 {len} {data}), внутри которых идёт обычный MTProxy-поток.
"""
import asyncio
import hashlib
import hmac as _hmac
import os
import struct
import time

from telethon.network.connection.tcpmtproxy import TcpMTProxy, MTProxyIO
from telethon.network.connection.tcpintermediate import RandomizedIntermediatePacketCodec

_TLS_HANDSHAKE = 0x16
_TLS_CCS = 0x14
_TLS_ALERT = 0x15
_TLS_APPDATA = 0x17
_MAX_RECORD = 4096
_DEFAULT_DOMAIN = "www.google.com"


# ------------------------------------------------------------------ #
#  Fake-TLS ClientHello                                               #
# ------------------------------------------------------------------ #

def _build_client_hello(secret: bytes, domain: str) -> bytes:
    """Строит фейковый TLS 1.0 ClientHello для MTProxy handshake."""
    client_random = struct.pack(">I", int(time.time())) + os.urandom(28)
    # session_id = HMAC-SHA256(key, client_random) — сервер по нему аутентифицирует нас
    session_id = _hmac.new(secret, client_random, hashlib.sha256).digest()  # 32 bytes

    cipher_suites = (
        b"\x13\x01\x13\x02\x13\x03"            # TLS 1.3 suites
        b"\xc0\x2b\xc0\x2f\xcc\xa9\xcc\xa8"    # ECDHE-ECDSA/RSA
        b"\xc0\x13\xc0\x14\x00\x9c\x00\x9d"    # RSA
        b"\x00\x2f\x00\x35\x00\xff"             # RSA CBC + SCSV
    )

    sni = domain.encode("ascii")
    sni_ext = (
        b"\x00\x00"
        + struct.pack(">H", len(sni) + 5)
        + struct.pack(">H", len(sni) + 3)
        + b"\x00"
        + struct.pack(">H", len(sni))
        + sni
    )
    ext = (
        sni_ext
        + b"\x00\x17\x00\x00"                  # extended master secret
        + b"\x00\x0a\x00\x08\x00\x06\x00\x1d\x00\x17\x00\x18"  # supported groups
        + b"\x00\x0b\x00\x02\x01\x00"          # EC point formats
        + b"\x00\x23\x00\x00"                   # session ticket
        + b"\x00\x2b\x00\x03\x02\x03\x04"      # supported versions: TLS 1.3
    )

    ch_body = (
        b"\x03\x03"                             # legacy_version TLS 1.2
        + client_random
        + b"\x20" + session_id                  # session_id_len=32 + session_id
        + struct.pack(">H", len(cipher_suites)) + cipher_suites
        + b"\x01\x00"                           # compression: null
        + struct.pack(">H", len(ext)) + ext
    )
    hs = b"\x01" + struct.pack(">I", len(ch_body))[1:] + ch_body  # type=ClientHello, 3-byte len
    return b"\x16\x03\x01" + struct.pack(">H", len(hs)) + hs     # TLS record


# ------------------------------------------------------------------ #
#  TLS record I/O                                                     #
# ------------------------------------------------------------------ #

async def _read_tls_record(reader, timeout: float = 10.0) -> tuple:
    """Читает одну TLS запись. Возвращает (content_type: int, data: bytes)."""
    hdr = await asyncio.wait_for(reader.readexactly(5), timeout=timeout)
    length = struct.unpack(">H", hdr[3:5])[0]
    data = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
    return hdr[0], data


async def _do_fake_tls_handshake(reader, writer, secret: bytes, domain: str) -> None:
    """
    Выполняет Fake-TLS хэндшейк:
    1. Отправляет ClientHello
    2. Читает ServerHello + ChangeCipherSpec + ApplicationData от сервера
    """
    writer.write(_build_client_hello(secret, domain))

    # Читаем ответ сервера до первого ApplicationData включительно
    while True:
        ct, _data = await _read_tls_record(reader, timeout=10.0)
        if ct == _TLS_ALERT:
            raise ConnectionError("Прокси отклонил fake-TLS соединение (TLS Alert)")
        if ct == _TLS_APPDATA:
            break   # handshake завершён


# ------------------------------------------------------------------ #
#  TLS ApplicationData обёртки для I/O                                #
# ------------------------------------------------------------------ #

class _TLSReader:
    """
    Читает данные из TLS ApplicationData записей.
    Накапливает внутренний буфер; все прочие типы TLS записей игнорирует.
    """
    def __init__(self, raw_reader):
        self._raw = raw_reader
        self._buf = bytearray()

    async def readexactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            ct, data = await _read_tls_record(self._raw, timeout=30.0)
            if ct == _TLS_ALERT:
                raise ConnectionError("TLS Alert от прокси")
            if ct == _TLS_APPDATA:
                self._buf.extend(data)
        result = bytes(self._buf[:n])
        del self._buf[:n]
        return result

    async def _wait_for_data(self, _name: str):
        """Telethon вызывает это для проверки что прокси не закрыл соединение."""
        ct, data = await _read_tls_record(self._raw, timeout=3.0)
        if ct == _TLS_APPDATA:
            self._buf.extend(data)

    def at_eof(self) -> bool:
        return False


class _TLSWriter:
    """
    Оборачивает данные в TLS ApplicationData записи перед отправкой.
    Разбивает большие буферы на куски по _MAX_RECORD байт.
    """
    def __init__(self, raw_writer):
        self._raw = raw_writer

    def write(self, data: bytes) -> None:
        off = 0
        while off < len(data):
            chunk = data[off: off + _MAX_RECORD]
            self._raw.write(b"\x17\x03\x03" + struct.pack(">H", len(chunk)) + chunk)
            off += len(chunk)


# ------------------------------------------------------------------ #
#  Telethon connection class                                          #
# ------------------------------------------------------------------ #

class ConnectionTcpMTProxyFakeTLS(TcpMTProxy):
    """
    MTProxy соединение с fake-TLS обфускацией (ee-prefix прокси).

    Порядок работы:
    1. TCP connect
    2. Fake-TLS handshake (ClientHello / ServerHello / CCS / AppData)
    3. Весь последующий трафик оборачивается в TLS ApplicationData
    4. Внутри потока — стандартный MTProxy Randomized Intermediate
    """
    packet_codec = RandomizedIntermediatePacketCodec

    def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
        super().__init__(ip, port, dc_id, loggers=loggers, proxy=proxy, local_addr=local_addr)
        self._tls_domain = _DEFAULT_DOMAIN  # используем дефолтный SNI

    async def _connect(self, timeout=None, ssl=None):
        # Шаг 1: чистое TCP соединение (минуя ObfuscatedConnection._connect)
        from telethon.network.connection.connection import Connection
        await Connection._connect(self, timeout=timeout, ssl=ssl)

        # Шаг 2: fake-TLS хэндшейк (использует self._secret из TcpMTProxy.__init__)
        await _do_fake_tls_handshake(
            self._reader, self._writer, self._secret, self._tls_domain
        )

        # Шаг 3: оборачиваем I/O в TLS ApplicationData
        tls_reader = _TLSReader(self._reader)
        tls_writer = _TLSWriter(self._writer)
        self._reader = tls_reader
        self._writer = tls_writer

        # Шаг 4: MTProxyIO читает/пишет через TLS-обёртки;
        # init_header отправляется как TLS AppData запись
        self._obfuscated_io = MTProxyIO(self)
        if self._obfuscated_io.header:
            tls_writer.write(self._obfuscated_io.header)

        self._reader = self._obfuscated_io
        self._writer = self._obfuscated_io
