"""Тесты вызовов WinAPI через ctypes (app/winapi.py).

Разбор таблицы и вызовы с подменой ctypes.WinDLL проверяются на любой
ОС; настоящие вызовы — только на Windows.
"""

import ctypes
import os
import socket
import struct
import sys
from pathlib import Path
from unittest import mock

import pytest

from app import winapi
from tests.platforms import windows_only

_LISTEN = 2


def tcp_row(address, port, pid, state=_LISTEN):
    """Собрать строку MIB_TCPROW_OWNER_PID.

    Порт — два байта в сетевом порядке и два байта выравнивания, как
    их заполняет Windows.
    """
    return struct.pack(
        "<I4s2s2x4s2s2xI",
        state,
        socket.inet_aton(address),
        port.to_bytes(2, "big"),
        socket.inet_aton("0.0.0.0"),
        b"\0\0",
        pid,
    )


def tcp6_row(address, port, pid, state=_LISTEN):
    """Собрать строку MIB_TCP6ROW_OWNER_PID."""
    return struct.pack(
        "<16sI2s2x16sI2s2xII",
        socket.inet_pton(socket.AF_INET6, address),
        0,
        port.to_bytes(2, "big"),
        bytes(16),
        0,
        b"\0\0",
        state,
        pid,
    )


def tcp_table(*rows):
    return struct.pack("<I", len(rows)) + b"".join(rows)


@pytest.fixture(autouse=True)
def clear_libraries():
    """Сбросить кэш загруженных библиотек до и после теста."""
    winapi._iphlpapi.cache_clear()  # noqa: SLF001 - кэш модуля
    winapi._kernel32.cache_clear()  # noqa: SLF001 - кэш модуля
    winapi._ntdll.cache_clear()  # noqa: SLF001 - кэш модуля
    yield
    winapi._iphlpapi.cache_clear()  # noqa: SLF001 - кэш модуля
    winapi._kernel32.cache_clear()  # noqa: SLF001 - кэш модуля
    winapi._ntdll.cache_clear()  # noqa: SLF001 - кэш модуля


class TestParseTcpTable:
    def test_rows(self):
        data = tcp_table(
            tcp_row("0.0.0.0", 8000, 1234),
            tcp_row("127.0.0.1", 445, 4),
        )
        assert winapi.parse_tcp_table(data) == [
            winapi.TcpRow(_LISTEN, "0.0.0.0", 8000, 1234),
            winapi.TcpRow(_LISTEN, "127.0.0.1", 445, 4),
        ]

    def test_port_in_network_byte_order(self):
        """Проверить: 8000 = 0x1F40 записан как 1F 40, а не 40 1F."""
        row = tcp_row("10.0.0.1", 8000, 1)
        assert row[8:12] == b"\x1f\x40\0\0"
        assert winapi.parse_tcp_table(tcp_table(row))[0].local_port == 8000

    def test_empty_table(self):
        assert winapi.parse_tcp_table(tcp_table()) == []

    def test_extra_bytes_ignored(self):
        """Проверить: буфер длиннее таблицы — лишнее не читается."""
        data = tcp_table(tcp_row("10.0.0.1", 80, 7)) + b"\xff" * 24
        assert len(winapi.parse_tcp_table(data)) == 1

    @pytest.mark.parametrize("data", [b"", b"\1\0\0"])
    def test_shorter_than_header(self, data):
        with pytest.raises(ValueError, match="header"):
            winapi.parse_tcp_table(data)

    def test_ipv6_rows(self):
        data = tcp_table(tcp6_row("::", 8000, 77), tcp6_row("::1", 445, 4))
        assert winapi.parse_tcp_table(data, winapi.AF_INET6) == [
            winapi.TcpRow(_LISTEN, "::", 8000, 77),
            winapi.TcpRow(_LISTEN, "::1", 445, 4),
        ]

    def test_ipv6_shorter_than_rows(self):
        """Проверить: строка IPv6 длиннее строки IPv4."""
        data = tcp_table(tcp_row("10.0.0.1", 80, 7), tcp_row("10.0.0.2", 80, 8))
        with pytest.raises(ValueError, match="2 rows"):
            winapi.parse_tcp_table(data, winapi.AF_INET6)

    def test_shorter_than_rows(self):
        data = tcp_table(tcp_row("10.0.0.1", 80, 7))
        data = struct.pack("<I", 2) + data[4:]
        with pytest.raises(ValueError, match="2 rows"):
            winapi.parse_tcp_table(data)


def fake_get_table(*tables, results=None, ipv6=None):
    """Вернуть поддельный GetExtendedTcpTable.

    tables — таблица IPv4 на момент каждого вызова (последняя
    повторяется), ipv6 — таблица IPv6 (по умолчанию пустая).
    Вызову без буфера или с коротким буфером сообщается размер и
    возвращается ERROR_INSUFFICIENT_BUFFER, иначе буфер заполняется.
    results — коды возврата по очереди вместо этого.
    """
    calls = iter(range(sys.maxsize))
    codes = iter(results or ())

    def get_table(buffer, size, _order, family, *_args):
        if results is not None:
            return next(codes)
        if family == winapi.AF_INET6:
            table = ipv6 or tcp_table()
        else:
            table = tables[min(next(calls), len(tables) - 1)]
        if buffer is None or len(buffer) < len(table):
            size._obj.value = len(table)  # noqa: SLF001 - объект за ctypes.byref
            return 122
        ctypes.memmove(buffer, table, len(table))
        return 0

    return mock.MagicMock(side_effect=get_table)


@pytest.fixture
def iphlpapi():
    """Подменить ctypes.WinDLL поддельной iphlpapi."""
    library = mock.MagicMock()
    with mock.patch.object(
        ctypes, "WinDLL", create=True, return_value=library
    ) as win_dll:
        library.win_dll = win_dll
        yield library


class TestTcpListeners:
    def test_reads_table(self, iphlpapi):
        table = tcp_table(tcp_row("0.0.0.0", 8000, 42))
        iphlpapi.GetExtendedTcpTable = fake_get_table(table)
        assert winapi.tcp_listeners() == [winapi.TcpRow(_LISTEN, "0.0.0.0", 8000, 42)]
        iphlpapi.win_dll.assert_called_once_with("iphlpapi")
        calls = iphlpapi.GetExtendedTcpTable.call_args_list
        assert calls[0].args[0] is None
        # Без сортировки, IPv4, затем IPv6, TCP_TABLE_OWNER_PID_LISTENER
        assert calls[1].args[2:] == (False, 2, 3, 0)
        assert calls[-1].args[2:] == (False, 23, 3, 0)

    def test_ipv6_listeners(self, iphlpapi):
        """Проверить: слушатель на [::] тоже в перечне, после IPv4."""
        iphlpapi.GetExtendedTcpTable = fake_get_table(
            tcp_table(tcp_row("127.0.0.1", 445, 4)),
            ipv6=tcp_table(tcp6_row("::", 8000, 77)),
        )
        assert winapi.tcp_listeners() == [
            winapi.TcpRow(_LISTEN, "127.0.0.1", 445, 4),
            winapi.TcpRow(_LISTEN, "::", 8000, 77),
        ]

    def test_signature(self, iphlpapi):
        iphlpapi.GetExtendedTcpTable = fake_get_table(tcp_table())
        winapi.tcp_listeners()
        function = iphlpapi.GetExtendedTcpTable
        assert function.restype is ctypes.c_uint32
        assert len(function.argtypes) == 6

    def test_table_grew(self, iphlpapi):
        """Проверить: таблица выросла после запроса размера — повтор."""
        small = tcp_table()
        large = tcp_table(tcp_row("127.0.0.1", 8000, 5))
        iphlpapi.GetExtendedTcpTable = fake_get_table(small, large)
        assert winapi.tcp_listeners()[0].pid == 5

    def test_table_keeps_growing(self, iphlpapi):
        """Проверить: попытки ограничены, затем OSError."""
        iphlpapi.GetExtendedTcpTable = fake_get_table(results=[122] * 10)
        with pytest.raises(OSError, match="GetExtendedTcpTable failed"):
            winapi.tcp_listeners()
        assert iphlpapi.GetExtendedTcpTable.call_count == 3

    def test_error(self, iphlpapi):
        iphlpapi.GetExtendedTcpTable = fake_get_table(results=[87])
        with pytest.raises(OSError, match="GetExtendedTcpTable failed") as info:
            winapi.tcp_listeners()
        if sys.platform == "win32":
            assert info.value.winerror == 87

    def test_ipv6_table_unavailable(self, iphlpapi):
        """Проверить: без таблицы IPv6 (стек отключён) — только IPv4."""
        table = tcp_table(tcp_row("0.0.0.0", 8000, 42))
        ipv4 = fake_get_table(table)

        def get_table(buffer, size, order, family, *args):
            if family == winapi.AF_INET6:
                return 50  # ERROR_NOT_SUPPORTED
            return ipv4(buffer, size, order, family, *args)

        iphlpapi.GetExtendedTcpTable = mock.MagicMock(side_effect=get_table)
        assert winapi.tcp_listeners() == [winapi.TcpRow(_LISTEN, "0.0.0.0", 8000, 42)]

    def test_success_without_buffer_allocates(self, iphlpapi):
        """Проверить: успех без буфера не считается таблицей."""
        table = tcp_table(tcp_row("127.0.0.1", 8000, 9))

        def get_table(buffer, size, _order, family, *_args):
            if family == winapi.AF_INET6:
                return 87
            size._obj.value = len(table)  # noqa: SLF001 - за ctypes.byref
            if buffer is None:
                return 0
            ctypes.memmove(buffer, table, len(table))
            return 0

        iphlpapi.GetExtendedTcpTable = mock.MagicMock(side_effect=get_table)
        assert winapi._tcp_table(winapi.AF_INET)[0].pid == 9  # noqa: SLF001

    def test_library_loaded_once(self, iphlpapi):
        iphlpapi.GetExtendedTcpTable = fake_get_table(tcp_table())
        for _ in range(3):
            winapi.tcp_listeners()
        iphlpapi.win_dll.assert_called_once()


class TestPortOwnerPid:
    def owner(self, *listeners, address="127.0.0.1"):
        """PID слушателя порта 8000 из (адрес, порт, PID)."""
        rows = [winapi.TcpRow(_LISTEN, *listener) for listener in listeners]
        with mock.patch.object(winapi, "tcp_listeners", return_value=rows):
            return winapi.port_owner_pid(8000, address)

    def test_exact_address_over_wildcard(self):
        """Проверить: соединение на 127.0.0.1 получает его слушатель."""
        pid = self.owner(
            ("192.168.1.10", 8000, 300),
            ("0.0.0.0", 8000, 100),
            ("127.0.0.1", 8000, 200),
            ("127.0.0.1", 445, 4),
        )
        assert pid == 200

    def test_ipv4_wildcard(self):
        pid = self.owner(("192.168.1.10", 8000, 300), ("0.0.0.0", 8000, 100))
        assert pid == 100

    def test_ipv4_wildcard_over_ipv6(self):
        pid = self.owner(("::", 8000, 77), ("0.0.0.0", 8000, 100))
        assert pid == 100

    def test_ipv4_mapped_address(self):
        """Проверить: сокет на ::ffff:127.0.0.1 принимает 127.0.0.1.

        Запись адреса в таблице зависит от версии Python, поэтому
        проверяются обе.
        """
        for mapped in ("::ffff:127.0.0.1", "::ffff:7f00:1"):
            pid = self.owner(("0.0.0.0", 8000, 100), (mapped, 8000, 55))
            assert pid == 55

    def test_dual_stack_ipv6_wildcard(self):
        """Проверить: [::] без IPV6_V6ONLY принимает и IPv4."""
        assert self.owner(("::", 8000, 77)) == 77

    def test_other_address_only(self):
        """Проверить: слушатель другого адреса соединение не получит."""
        pid = self.owner(("192.168.1.10", 8000, 300), ("::1", 8000, 5))
        assert pid is None

    def test_ipv6_target(self):
        listeners = [("0.0.0.0", 8000, 100), ("::", 8000, 77), ("::1", 8000, 5)]
        assert self.owner(*listeners, address="::1") == 5
        assert self.owner(*listeners[:2], address="::1") == 77
        assert self.owner(listeners[0], address="::1") is None

    def test_nobody_listens(self):
        assert self.owner(("127.0.0.1", 445, 4)) is None

    def test_default_address(self):
        rows = [winapi.TcpRow(_LISTEN, "127.0.0.1", 8000, 200)]
        with mock.patch.object(winapi, "tcp_listeners", return_value=rows):
            assert winapi.port_owner_pid(8000) == 200


@pytest.fixture
def kernel32():
    """Подменить ctypes.WinDLL поддельной kernel32."""
    library = mock.MagicMock()
    library.RegisterApplicationRestart.return_value = 0
    with mock.patch.object(
        ctypes, "WinDLL", create=True, return_value=library
    ) as win_dll:
        library.win_dll = win_dll
        yield library


class TestRegisterApplicationRestart:
    def test_flags_and_empty_command_line(self, kernel32):
        winapi.register_application_restart()
        kernel32.win_dll.assert_called_once_with("kernel32")
        kernel32.RegisterApplicationRestart.assert_called_once_with(
            None, 0x1 | 0x2 | 0x8
        )
        assert kernel32.RegisterApplicationRestart.restype is ctypes.c_long

    def test_restart_flags(self):
        assert winapi.RESTART_FLAGS == 11

    def test_failure(self, kernel32):
        # E_INVALIDARG; HRESULT отрицательный как c_long
        kernel32.RegisterApplicationRestart.return_value = -2147024809
        with pytest.raises(OSError, match="HRESULT 0x80070057"):
            winapi.register_application_restart()


@pytest.fixture
def msvcrt():
    """Подменить msvcrt: дескриптор Windows — 1000 + номер fd."""
    module = mock.Mock()
    module.get_osfhandle.side_effect = lambda fd: 1000 + fd
    with mock.patch.dict(sys.modules, {"msvcrt": module}):
        yield module


class TestConsole:
    @pytest.mark.parametrize(
        ("window", "expected"), [(0, False), (None, False), (0x1234, True)]
    )
    def test_has_console_window(self, kernel32, window, expected):
        kernel32.GetConsoleWindow.return_value = window
        assert winapi.has_console_window() is expected
        assert kernel32.GetConsoleWindow.restype is ctypes.c_void_p

    @pytest.mark.parametrize(
        ("status", "device_type", "null"),
        [
            # Не NUL: файл на диске и консоль
            (0, 0x07, False),
            (0, 0x50, False),
            (0, winapi.FILE_DEVICE_NULL, True),
            # Тип узнать не удалось (STATUS_INVALID_HANDLE): не NUL
            (-1073741816, winapi.FILE_DEVICE_NULL, False),
        ],
    )
    @pytest.mark.usefixtures("msvcrt")
    def test_is_null_device(self, kernel32, status, device_type, null):
        def query(handle, _io_status, info, size, info_class):
            device = info._obj  # noqa: SLF001 - структура за ctypes.byref
            assert (handle, info_class) == (1002, 4)
            assert size == ctypes.sizeof(device)
            device.device_type = device_type
            return status

        kernel32.NtQueryVolumeInformationFile.side_effect = query
        assert winapi.is_null_device(2) is null
        kernel32.win_dll.assert_called_once_with("ntdll")
        assert kernel32.NtQueryVolumeInformationFile.restype is ctypes.c_long


@windows_only
class TestWindows:
    def test_listener_pid(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            assert winapi.port_owner_pid(port) == os.getpid()

    def test_ipv6_listener_pid(self):
        with socket.socket(socket.AF_INET6) as listener:
            listener.bind(("::1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            assert winapi.port_owner_pid(port, "::1") == os.getpid()

    def test_register_application_restart(self):
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetApplicationRestartSettings.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
        )
        kernel32.GetApplicationRestartSettings.restype = ctypes.c_long
        winapi.register_application_restart()
        try:
            command_line = ctypes.create_unicode_buffer(1024)
            size = ctypes.c_uint32(len(command_line))
            flags = ctypes.c_uint32()
            result = kernel32.GetApplicationRestartSettings(
                kernel32.GetCurrentProcess(),
                command_line,
                ctypes.byref(size),
                ctypes.byref(flags),
            )
        finally:
            kernel32.UnregisterApplicationRestart()
        assert result == 0
        assert command_line.value == ""
        assert flags.value == winapi.RESTART_FLAGS

    def test_null_device(self, tmp_path):
        with Path(os.devnull).open("w", encoding="utf-8") as null:
            assert winapi.is_null_device(null.fileno()) is True
        with (tmp_path / "log.txt").open("w", encoding="utf-8") as file:
            assert winapi.is_null_device(file.fileno()) is False
