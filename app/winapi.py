"""Вызовы WinAPI, которых нет в pywin32, через ctypes.

Модуль импортируется на любой ОС: библиотеки загружаются при первом
вызове, а разбор таблицы TCP — чистая функция над байтами, поэтому
тесты подменяют вызовы и на Linux.
"""

import contextlib
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

# NtQueryVolumeInformationFile: класс FileFsDeviceInformation и тип
# устройства NUL (ntifs.h, devioctl.h)
_FILE_FS_DEVICE_INFORMATION = 4
FILE_DEVICE_NULL = 0x15

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


# Строка, класс адреса и номера полей состояния, адреса, порта и PID
# в распакованной строке
_ROW_FORMATS = {
    AF_INET: (_TCP_ROW, ipaddress.IPv4Address, (0, 1, 2, 5)),
    AF_INET6: (_TCP6_ROW, ipaddress.IPv6Address, (4, 0, 1, 5)),
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
    row, address_class, fields = _ROW_FORMATS[family]
    if len(data) < _TCP_TABLE_HEADER.size + count * row.size:
        msg = f"TCP table is shorter than {count} rows"
        raise ValueError(msg)
    rows = []
    for index in range(count):
        values = row.unpack_from(data, _TCP_TABLE_HEADER.size + index * row.size)
        state, address, port, pid = (values[field] for field in fields)
        rows.append(
            TcpRow(state, str(address_class(address)), int.from_bytes(port, "big"), pid)
        )
    return rows


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
    """Загрузить kernel32 и описать сигнатуры нужных функций.

    Отдельный объект библиотеки, а не _kernel32() из
    app/handlers/files.py: сигнатуры — состояние объекта, и модули не
    зависят друг от друга.
    """
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.RegisterApplicationRestart.argtypes = (ctypes.c_wchar_p, ctypes.c_uint32)
    kernel32.RegisterApplicationRestart.restype = ctypes.c_long
    kernel32.GetConsoleWindow.argtypes = ()
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    return kernel32


class _IoStatusBlock(ctypes.Structure):
    """IO_STATUS_BLOCK: состояние (объединение с указателем) и число."""

    _fields_ = (("status", ctypes.c_void_p), ("information", ctypes.c_size_t))


class _DeviceInformation(ctypes.Structure):
    """FILE_FS_DEVICE_INFORMATION: тип устройства и его свойства."""

    _fields_ = (("device_type", ctypes.c_uint32), ("characteristics", ctypes.c_uint32))


@functools.cache
def _ntdll() -> ctypes.CDLL:
    """Загрузить ntdll, описать NtQueryVolumeInformationFile."""
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtQueryVolumeInformationFile.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_IoStatusBlock),
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
    )
    ntdll.NtQueryVolumeInformationFile.restype = ctypes.c_long
    return ntdll


def tcp_listeners() -> list[TcpRow]:
    """Вернуть слушающие сокеты TCP IPv4 и IPv6 с PID владельцев.

    PID берётся из таблицы TCP, а не из процесса: права на чужой
    процесс для этого не нужны. IPv6 нужен потому, что слушатель на
    [::] без IPV6_V6ONLY занимает порт и для 0.0.0.0.

    Returns:
        Строки таблиц GetExtendedTcpTable: сначала IPv4, затем IPv6;
        без IPv6, если её таблицу получить не удалось (например, стек
        IPv6 отключён)

    Raises:
        OSError: Если не удалось получить таблицу IPv4

    """
    rows = _tcp_table(AF_INET)
    with contextlib.suppress(OSError):
        rows += _tcp_table(AF_INET6)
    return rows


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
    подходят и сокеты IPv6 на этом адресе, отображённом в IPv6
    (::ffff:127.0.0.1), и на [::]: режим IPV6_V6ONLY по таблице не
    узнать, а без него такой слушатель принимает и IPv4. Слушатель на
    другом адресе с тем же портом соединение не получит.

    Args:
        port: Номер порта
        address: Адрес, к которому подключается клиент

    Returns:
        PID слушателя или None, если соединение никто не примет

    Raises:
        OSError: Если таблицу TCP получить не удалось

    """
    target = ipaddress.ip_address(address)
    candidates = [target, ipaddress.IPv6Address("::")]
    if isinstance(target, ipaddress.IPv4Address):
        candidates[1:1] = [
            ipaddress.IPv6Address(f"::ffff:{target}"),
            ipaddress.IPv4Address("0.0.0.0"),
        ]
    # Адреса сравниваются как объекты: запись отображённого адреса
    # в строке зависит от версии Python
    pids: dict[ipaddress.IPv4Address | ipaddress.IPv6Address, int] = {}
    for row in tcp_listeners():
        if row.local_port == port:
            pids.setdefault(ipaddress.ip_address(row.local_address), row.pid)
    return next(
        (pids[candidate] for candidate in candidates if candidate in pids), None
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


def has_console_window() -> bool:
    """Проверить: у процесса есть окно консоли (GetConsoleWindow).

    В сборке Nuitka с --windows-console-mode=attach окно есть, если exe
    присоединился к консоли родителя: запущен из cmd, PowerShell,
    Windows Terminal или bat-файла. При входе пользователя и двойным
    щелчком консоли нет. Консоль, созданная без окна (CREATE_NO_WINDOW),
    окна тоже не даёт.
    """
    return bool(_kernel32().GetConsoleWindow())


def is_null_device(fd: int) -> bool:
    """Проверить: дескриптор ведёт на устройство NUL.

    По типу устройства (FileFsDeviceInformation): у NUL он
    FILE_DEVICE_NULL, у консоли — FILE_DEVICE_CONSOLE. Запрос не требует
    прав на чтение, поэтому консоль, открытая только на запись, за NUL
    не принимается. Если тип узнать не удалось — не NUL: поток лучше
    оставить, чем потерять вывод.
    """
    import msvcrt

    io_status = _IoStatusBlock()
    info = _DeviceInformation()
    status = _ntdll().NtQueryVolumeInformationFile(
        msvcrt.get_osfhandle(fd),
        ctypes.byref(io_status),
        ctypes.byref(info),
        ctypes.sizeof(info),
        _FILE_FS_DEVICE_INFORMATION,
    )
    return status == 0 and info.device_type == FILE_DEVICE_NULL
