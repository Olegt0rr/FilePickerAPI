"""Вызовы WinAPI, которых нет в pywin32, через ctypes.

Модуль импортируется на любой ОС: библиотеки загружаются при первом
вызове, а разбор таблицы TCP — чистая функция над байтами, поэтому
тесты подменяют вызовы и на Linux.
"""

import ctypes
import functools
import ipaddress
import struct
from typing import NamedTuple

# Флаги RegisterApplicationRestart: перезапуск только после закрытия
# программой установки (Restart Manager), не после сбоя, зависания или
# перезагрузки для обновления Windows
RESTART_NO_CRASH = 0x1
RESTART_NO_HANG = 0x2
RESTART_NO_REBOOT = 0x8
RESTART_FLAGS = RESTART_NO_CRASH | RESTART_NO_HANG | RESTART_NO_REBOOT

AF_INET = 2
AF_INET6 = 23
# TCP_TABLE_CLASS.TCP_TABLE_OWNER_PID_LISTENER: только слушающие сокеты
_TCP_TABLE_OWNER_PID_LISTENER = 3
_NO_ERROR = 0
_ERROR_INSUFFICIENT_BUFFER = 122
# Таблица может вырасти между запросом размера и чтением; попыток
# немного, чтобы не зациклиться
_TCP_TABLE_ATTEMPTS = 3

# MIB_TCPTABLE_OWNER_PID и MIB_TCP6TABLE_OWNER_PID: DWORD
# dwNumEntries, затем строки. Порт — младшие два байта DWORD в сетевом
# порядке, адрес — байты в сетевом порядке
_TCP_TABLE_HEADER = struct.Struct("<I")
# MIB_TCPROW_OWNER_PID: состояние, локальные адрес и порт, удалённые
# адрес и порт, PID
_TCP_ROW = struct.Struct("<I4s2s2x4s2s2xI")
# MIB_TCP6ROW_OWNER_PID: локальные адрес, зона и порт, удалённые адрес,
# зона и порт, состояние, PID
_TCP6_ROW = struct.Struct("<16s4x2s2x16s4x2s2xII")


class TcpRow(NamedTuple):
    """Строка таблицы TCP с PID владельца сокета."""

    state: int
    local_address: str
    local_port: int
    pid: int


def _tcp_row(row: bytes) -> TcpRow:
    state, address, port, _remote, _remote_port, pid = _TCP_ROW.unpack(row)
    return TcpRow(
        state, str(ipaddress.IPv4Address(address)), int.from_bytes(port, "big"), pid
    )


def _tcp6_row(row: bytes) -> TcpRow:
    address, port, _remote, _remote_port, state, pid = _TCP6_ROW.unpack(row)
    return TcpRow(
        state, str(ipaddress.IPv6Address(address)), int.from_bytes(port, "big"), pid
    )


_ROW_PARSERS = {
    AF_INET: (_TCP_ROW.size, _tcp_row),
    AF_INET6: (_TCP6_ROW.size, _tcp6_row),
}


def parse_tcp_table(data: bytes, family: int = AF_INET) -> list[TcpRow]:
    """Разобрать таблицу слушающих сокетов TCP с PID владельцев.

    MIB_TCPTABLE_OWNER_PID (IPv4) и MIB_TCP6TABLE_OWNER_PID (IPv6)
    отличаются только строками.

    Args:
        data: Буфер, заполненный GetExtendedTcpTable
        family: AF_INET или AF_INET6 — семейство адресов таблицы

    Returns:
        Строки таблицы в её порядке

    Raises:
        ValueError: Если буфер короче, чем объявлено в заголовке

    """
    if len(data) < _TCP_TABLE_HEADER.size:
        msg = "TCP table is shorter than its header"
        raise ValueError(msg)
    (count,) = _TCP_TABLE_HEADER.unpack_from(data)
    row_size, parse_row = _ROW_PARSERS[family]
    if len(data) < _TCP_TABLE_HEADER.size + count * row_size:
        msg = f"TCP table is shorter than {count} rows"
        raise ValueError(msg)
    offsets = range(
        _TCP_TABLE_HEADER.size, _TCP_TABLE_HEADER.size + count * row_size, row_size
    )
    return [parse_row(data[offset : offset + row_size]) for offset in offsets]


@functools.cache
def _iphlpapi() -> ctypes.CDLL:
    """Загрузить iphlpapi и описать сигнатуру GetExtendedTcpTable."""
    iphlpapi = ctypes.WinDLL("iphlpapi")
    iphlpapi.GetExtendedTcpTable.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint32,
    )
    iphlpapi.GetExtendedTcpTable.restype = ctypes.c_uint32
    return iphlpapi


@functools.cache
def _kernel32() -> ctypes.CDLL:
    """Загрузить kernel32, описать сигнатуру RegisterApplicationRestart.

    Отдельный объект библиотеки, а не _kernel32() из
    app/handlers/files.py: сигнатуры — состояние объекта, и модули не
    зависят друг от друга.
    """
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.RegisterApplicationRestart.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
    kernel32.RegisterApplicationRestart.restype = ctypes.c_long
    return kernel32


def tcp_listeners() -> list[TcpRow]:
    """Вернуть слушающие сокеты TCP IPv4 и IPv6 с PID владельцев.

    PID берётся из таблицы TCP, а не из процесса: права на чужой
    процесс для этого не нужны. IPv6 нужен потому, что слушатель на
    [::] без IPV6_V6ONLY занимает порт и для 0.0.0.0.

    Returns:
        Строки таблиц GetExtendedTcpTable: сначала IPv4, затем IPv6

    Raises:
        OSError: Если таблицу получить не удалось

    """
    return _tcp_table(AF_INET) + _tcp_table(AF_INET6)


def _tcp_table(family: int) -> list[TcpRow]:
    """Прочитать таблицу слушающих сокетов одного семейства адресов.

    Raises:
        OSError: Если таблицу получить не удалось

    """
    get_table = _iphlpapi().GetExtendedTcpTable
    size = ctypes.c_uint32(0)
    buffer = None
    for _ in range(_TCP_TABLE_ATTEMPTS):
        result = get_table(
            buffer,
            ctypes.byref(size),
            False,  # noqa: FBT003 - BOOL bOrder: без сортировки
            family,
            _TCP_TABLE_OWNER_PID_LISTENER,
            0,
        )
        if result == _NO_ERROR and buffer is not None:
            return parse_tcp_table(buffer.raw, family)
        if result not in {_NO_ERROR, _ERROR_INSUFFICIENT_BUFFER}:
            break
        # Первый вызов без буфера сообщает нужный размер; если таблица
        # выросла, размер обновлён, и буфер выделяется заново
        buffer = ctypes.create_string_buffer(size.value)
    msg = "GetExtendedTcpTable failed"
    raise OSError(None, msg, None, result)


def port_owner_pid(port: int, address: str = "127.0.0.1") -> int | None:
    """Вернуть PID процесса, который примет соединение на address:port.

    Соединение получает слушатель на самом адресе, а без него —
    слушатель на адресе «любой» своего семейства. К адресу IPv4
    подходит и [::]: режим IPV6_V6ONLY по таблице не узнать, а без него
    такой слушатель принимает и IPv4. Слушатель на другом адресе с тем
    же портом соединение не получит.

    Args:
        port: Номер порта
        address: Адрес, к которому подключается клиент

    Returns:
        PID слушателя или None, если соединение никто не примет

    Raises:
        OSError: Если таблицу TCP получить не удалось

    """
    target = ipaddress.ip_address(address)
    wildcards = ("::",)
    if isinstance(target, ipaddress.IPv4Address):
        wildcards = ("0.0.0.0", *wildcards)
    pids: dict[str, int] = {}
    for row in tcp_listeners():
        if row.local_port == port:
            pids.setdefault(row.local_address, row.pid)
    return next(
        (
            pids[candidate]
            for candidate in (str(target), *wildcards)
            if candidate in pids
        ),
        None,
    )


def register_application_restart() -> None:
    """Попросить Restart Manager перезапустить сервис после установки.

    Командная строка пустая (сервис принимает только запуск без
    аргументов), флаги RESTART_FLAGS: только после закрытия программой
    установки.

    Raises:
        OSError: Если регистрация не удалась (HRESULT в сообщении)

    """
    result = _kernel32().RegisterApplicationRestart(None, RESTART_FLAGS)
    if result:
        msg = f"RegisterApplicationRestart failed: HRESULT 0x{result & 0xFFFFFFFF:08X}"
        raise OSError(None, msg)
