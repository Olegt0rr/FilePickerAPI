"""Тесты стандартных потоков и определения консоли.

Функции WinAPI проверяются в tests/test_winapi.py; здесь — что из них
следует для потоков, и отдельные процессы без консоли и с ней.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from app import stdio, winapi
from tests.conftest import ROOT, child_env
from tests.platforms import linux_only, windows_only


class TestNullDevice:
    """Поток на устройстве NUL непригоден на Windows."""

    @pytest.mark.parametrize("null", [False, True])
    def test_windows(self, monkeypatch, tmp_path, null):
        """Проверить: на Windows пригодность потока зависит от
        is_null_device с его дескриптором.
        """
        monkeypatch.setattr(sys, "platform", "win32")
        is_null = mock.Mock(return_value=null)
        monkeypatch.setattr(winapi, "is_null_device", is_null)

        with (tmp_path / "log.txt").open("w", encoding="utf-8") as stream:
            assert stdio._stream_is_unusable(stream) is null  # noqa: SLF001
            is_null.assert_called_once_with(stream.fileno())

    def test_not_windows(self, monkeypatch, tmp_path):
        """Проверить: вне Windows NUL не проверяется."""
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            winapi, "is_null_device", mock.Mock(side_effect=AssertionError)
        )

        with (tmp_path / "log.txt").open("w", encoding="utf-8") as stream:
            assert stdio._stream_is_unusable(stream) is False  # noqa: SLF001

    def test_null_stderr_replaced(self, monkeypatch, tmp_path):
        """Проверить: stderr на NUL (exe Nuitka без консоли) заменяется
        на os.devnull, и ensure_std_streams() сообщает, что он
        непригоден; пригодный stdout остаётся.
        """
        null = (tmp_path / "nul").open("w", encoding="utf-8")
        stdout = (tmp_path / "out").open("w", encoding="utf-8")
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(winapi, "is_null_device", lambda fd: fd == null.fileno())
        monkeypatch.setattr(sys, "stderr", null)
        monkeypatch.setattr(sys, "stdout", stdout)

        try:
            assert stdio.ensure_std_streams() is False
            assert sys.stdout is stdout
            assert sys.stderr.name == os.devnull
        finally:
            sys.stderr.close()
            stdout.close()
            null.close()

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


# Дочерний процесс сообщает, что увидел, через файл: его потоков нет
CHILD_SCRIPT = """
import json, sys
from app import winapi
from app.stdio import ensure_std_streams
result = {
    "usable": ensure_std_streams(),
    "window": winapi.has_console_window(),
}
if result["window"]:
    # Консоль, открытая только на запись, как её открывает Nuitka
    with open("CONOUT$", "w") as console:
        result["console_null"] = winapi.is_null_device(console.fileno())
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
        запускается при входе) — без окна, журнал в stderr не идёт.
        """
        result = run_child(tmp_path, creationflags=subprocess.DETACHED_PROCESS)

        assert result == {"usable": False, "window": False}

    def test_hidden_console_without_window(self, tmp_path):
        """Проверить: консоль без окна (CREATE_NO_WINDOW) — без окна."""
        result = run_child(tmp_path, creationflags=subprocess.CREATE_NO_WINDOW)

        assert result == {"usable": False, "window": False}

    def test_detached_with_redirected_stderr(self, tmp_path):
        """Проверить: stderr в файл у процесса без консоли — журнал в
        него идёт, но запуск всё равно без окна.
        """
        with (tmp_path / "stderr.txt").open("w", encoding="utf-8") as stderr:
            result = run_child(
                tmp_path, creationflags=subprocess.DETACHED_PROCESS, stderr=stderr
            )

        assert result == {"usable": True, "window": False}

    def test_console_window(self, tmp_path):
        """Проверить: в своём окне консоли — из консоли, а консоль,
        открытая только на запись, не принимается за NUL.

        Окно скрыто (SW_HIDE), чтобы тест ничего не показывал. Окно
        есть и у скрытой консоли, и на рабочем столе службы (раннер
        CI), а при терминале по умолчанию Windows Terminal — его
        псевдоокно; GetConsoleWindow() во всех случаях не 0.
        """
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
        assert result["console_null"] is False
