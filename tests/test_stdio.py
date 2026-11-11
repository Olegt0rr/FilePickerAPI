"""Тесты стандартных потоков и определения консоли (app/stdio.py)."""

import ctypes
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from app import stdio
from tests.conftest import ROOT, child_env
from tests.platforms import linux_only, windows_only

# Тип файла «диск» в GetFileType
FILE_TYPE_DISK = 0x0001


class FakeKernel32:
    """Поддельный kernel32: тип файла, режим консоли, окно консоли."""

    def __init__(self, *, file_type=FILE_TYPE_DISK, console_mode=False, window=0):
        self.file_type = file_type
        self.console_mode = console_mode
        self.window = window
        self.handles = []

    def GetFileType(self, handle):  # noqa: N802 - имя функции WinAPI
        self.handles.append(handle)
        return self.file_type

    def GetConsoleMode(self, handle, _mode):  # noqa: N802 - имя функции WinAPI
        self.handles.append(handle)
        return int(self.console_mode)

    def GetConsoleWindow(self):  # noqa: N802 - имя функции WinAPI
        return self.window


@pytest.fixture
def fake_windows(monkeypatch):
    """Windows с поддельными kernel32 и msvcrt; даёт установщик."""
    monkeypatch.setattr(sys, "platform", "win32")
    msvcrt = types.ModuleType("msvcrt")
    msvcrt.get_osfhandle = lambda fd: 1000 + fd
    monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)

    def install(**kwargs):
        kernel32 = FakeKernel32(**kwargs)
        monkeypatch.setattr(stdio, "_kernel32", lambda: kernel32)
        return kernel32

    return install


class TestNullDevice:
    """Поток на устройстве NUL непригоден на Windows."""

    @pytest.mark.parametrize(
        ("file_type", "console_mode", "null"),
        [
            # Файл или канал: не NUL
            (FILE_TYPE_DISK, False, False),
            # Символьное устройство с режимом консоли: консоль
            (stdio.FILE_TYPE_CHAR, True, False),
            # Символьное устройство без режима консоли: NUL
            (stdio.FILE_TYPE_CHAR, False, True),
        ],
    )
    def test_null_device_detection(self, fake_windows, file_type, console_mode, null):
        """Проверить: NUL — символьное устройство без режима консоли;
        дескриптор передаётся в WinAPI через get_osfhandle.
        """
        kernel32 = fake_windows(file_type=file_type, console_mode=console_mode)

        assert stdio._is_null_device(2) is null  # noqa: SLF001

        assert set(kernel32.handles) == {1002}

    def test_null_stream_replaced(self, fake_windows, monkeypatch, tmp_path):
        """Проверить: stderr на NUL (exe Nuitka без консоли) заменяется
        на os.devnull, и ensure_std_streams() сообщает, что он
        непригоден.
        """
        fake_windows(file_type=stdio.FILE_TYPE_CHAR)
        null = (tmp_path / "nul").open("w", encoding="utf-8")
        monkeypatch.setattr(sys, "stderr", null)
        monkeypatch.setattr(sys, "stdout", null)

        try:
            assert stdio.ensure_std_streams() is False
            assert sys.stderr is not null
            assert sys.stderr.name == os.devnull
            assert sys.stdout.name == os.devnull
        finally:
            sys.stderr.close()
            sys.stdout.close()
            null.close()

    def test_not_windows(self, monkeypatch):
        """Проверить: вне Windows NUL не проверяется."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(stdio, "_kernel32", mock.Mock(side_effect=AssertionError))

        assert stdio._is_null_device(2) is False  # noqa: SLF001

    @windows_only
    def test_real_null_device(self, tmp_path):
        """Проверить: настоящий os.devnull на Windows непригоден, файл —
        пригоден.
        """
        with Path(os.devnull).open("w", encoding="utf-8") as null:
            assert stdio._stream_is_unusable(null) is True  # noqa: SLF001
        with (tmp_path / "log.txt").open("w", encoding="utf-8") as file:
            assert stdio._stream_is_unusable(file) is False  # noqa: SLF001

    @linux_only
    def test_dev_null_usable_on_linux(self):
        """Проверить: вне Windows /dev/null пригоден, как раньше."""
        with Path(os.devnull).open("w", encoding="utf-8") as null:
            assert stdio._stream_is_unusable(null) is False  # noqa: SLF001


class TestConsoleWindow:
    """Способ запуска: по окну консоли, а не по stderr."""

    @pytest.mark.parametrize(
        ("window", "expected"), [(0, False), (None, False), (0x1234, True)]
    )
    @pytest.mark.parametrize("stderr_usable", [False, True])
    def test_windows(self, fake_windows, window, expected, stderr_usable):
        """Проверить: на Windows признак — GetConsoleWindow(),
        пригодность stderr не важна.
        """
        fake_windows(window=window)

        assert stdio.has_console_window(stderr_usable=stderr_usable) is expected

    @pytest.mark.parametrize("stderr_usable", [False, True])
    def test_not_windows(self, monkeypatch, stderr_usable):
        """Проверить: вне Windows признак — пригодность stderr."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(stdio, "_kernel32", mock.Mock(side_effect=AssertionError))

        assert stdio.has_console_window(stderr_usable=stderr_usable) is stderr_usable

    def test_kernel32_signatures(self, monkeypatch):
        """Проверить: у функций kernel32 объявлены типы (дескрипторы —
        указатели, а не int).
        """
        win_dll = mock.Mock()
        monkeypatch.setattr(ctypes, "WinDLL", win_dll, raising=False)

        kernel32 = stdio._kernel32()  # noqa: SLF001

        win_dll.assert_called_once_with("kernel32", use_last_error=True)
        assert kernel32.GetConsoleWindow.restype is ctypes.c_void_p
        assert kernel32.GetFileType.argtypes == (ctypes.c_void_p,)
        assert kernel32.GetConsoleMode.argtypes[0] is ctypes.c_void_p


# Дочерний процесс сообщает, что увидел, через файл: его потоков нет
CHILD_SCRIPT = """
import json, sys
from app.stdio import ensure_std_streams, has_console_window
usable = ensure_std_streams()
result = {
    "usable": usable,
    "window": has_console_window(stderr_usable=usable),
}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(result, f)
"""


def run_child(tmp_path, *, creationflags, stderr=subprocess.DEVNULL, startupinfo=None):
    """Запустить python с CHILD_SCRIPT и вернуть его вывод."""
    out = tmp_path / "result.json"
    subprocess.run(  # noqa: S603 - аргументы задаются тестом
        [sys.executable, "-c", CHILD_SCRIPT, str(out)],
        cwd=ROOT,
        env=child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        creationflags=creationflags,
        startupinfo=startupinfo,
        timeout=60,
        check=True,
    )
    return json.loads(out.read_text(encoding="utf-8"))


@windows_only
class TestChildProcess:
    """Отдельный процесс: как при входе пользователя и из консоли."""

    def test_detached_without_streams(self, tmp_path):
        """Проверить: без консоли и с потоками на NUL (так exe Nuitka
        запускается при входе) — «без окна», журнал в stderr не идёт.
        """
        result = run_child(tmp_path, creationflags=subprocess.DETACHED_PROCESS)

        assert result == {"usable": False, "window": False}

    def test_hidden_console_without_window(self, tmp_path):
        """Проверить: консоль без окна (CREATE_NO_WINDOW) — без окна."""
        result = run_child(tmp_path, creationflags=subprocess.CREATE_NO_WINDOW)

        assert result == {"usable": False, "window": False}

    def test_detached_with_redirected_stderr(self, tmp_path):
        """Проверить: stderr в файл у процесса без консоли — журнал в
        него идёт, но запуск всё равно «без окна».
        """
        with (tmp_path / "stderr.txt").open("w", encoding="utf-8") as stderr:
            result = run_child(
                tmp_path, creationflags=subprocess.DETACHED_PROCESS, stderr=stderr
            )

        assert result == {"usable": True, "window": False}

    def test_console_window(self, tmp_path):
        """Проверить: в своём (скрытом) окне консоли — «из консоли»."""
        startupinfo = subprocess.STARTUPINFO(
            dwFlags=subprocess.STARTF_USESHOWWINDOW, wShowWindow=0
        )

        result = run_child(
            tmp_path,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            stderr=None,
            startupinfo=startupinfo,
        )

        assert result["window"] is True
