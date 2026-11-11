"""Стандартные потоки и консоль процесса.

Лёгкий модуль: его импортирует точка входа до разбора аргументов.
WinAPI вызывается через ctypes, без pywin32.
"""

import ctypes
import io
import os
import sys
from pathlib import Path

# GetFileType: символьное устройство (консоль, NUL, COM-порт)
FILE_TYPE_CHAR = 0x0002


def _windows() -> bool:
    return sys.platform == "win32"


def _kernel32() -> ctypes.CDLL:
    """kernel32 с объявленными типами нужных функций."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetConsoleWindow.argtypes = ()
    kernel32.GetConsoleWindow.restype = ctypes.c_void_p
    kernel32.GetFileType.argtypes = (ctypes.c_void_p,)
    kernel32.GetFileType.restype = ctypes.c_uint32
    kernel32.GetConsoleMode.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    )
    kernel32.GetConsoleMode.restype = ctypes.c_int
    return kernel32


def _is_null_device(fd: int) -> bool:
    """Проверить: дескриптор Windows ведёт на устройство NUL.

    Exe Nuitka (--windows-console-mode=attach), запущенный без
    консоли, получает от Nuitka sys.stdout и sys.stderr, открытые на
    NUL: os.fstat на них проходит, но вывод никуда не попадает. У NUL,
    как и у консоли, тип символьного устройства, но GetConsoleMode на
    нём не срабатывает. Вне Windows /dev/null считается пригодным, как
    раньше.
    """
    if not _windows():
        return False
    import msvcrt

    handle = msvcrt.get_osfhandle(fd)
    kernel32 = _kernel32()
    if kernel32.GetFileType(handle) != FILE_TYPE_CHAR:
        return False
    mode = ctypes.c_uint32()
    return not kernel32.GetConsoleMode(handle, ctypes.byref(mode))


def _stream_is_unusable(stream: object) -> bool:
    """Проверить: потока нет, он закрыт, дескриптор плох или это NUL."""
    if stream is None or getattr(stream, "closed", False):
        return True
    try:
        fd = stream.fileno()
        os.fstat(fd)
    except (AttributeError, io.UnsupportedOperation):
        # Поток без дескриптора (например, перехват вывода в тестах)
        return False
    except (OSError, ValueError):
        return True
    return _is_null_device(fd)


def ensure_std_streams() -> bool:
    """Подставить os.devnull вместо непригодных stdout и stderr.

    Exe с --windows-console-mode=attach, запущенный без консоли (при
    входе пользователя, двойным щелчком, из run_production.bat), может
    получить sys.stdout и sys.stderr равными None, как pythonw, потоки
    на недействительном дескрипторе или на NUL (так делает Nuitka).
    Тогда журнал Uvicorn падал бы на sys.stdout.isatty() или на первой
    записи.

    Возвращает, был ли stderr пригоден, то есть есть ли куда выводить
    журнал. Способ запуска для события 1000 — has_console_window().
    """
    usable = not _stream_is_unusable(sys.stderr)
    for name in ("stdout", "stderr"):
        if _stream_is_unusable(getattr(sys, name)):
            # Поток живёт до конца процесса, закрывать его некому
            stream = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115
            setattr(sys, name, stream)
    return usable


def has_console_window(*, stderr_usable: bool) -> bool:
    """Проверить: процесс запущен в окне консоли.

    На Windows — по GetConsoleWindow(): после AttachConsole в сборке
    Nuitka окно есть, только если exe запущен из консоли (cmd,
    PowerShell, Windows Terminal). Пригодный stderr этого не говорит:
    без консоли Nuitka открывает его на NUL, а stderr в канал или файл
    бывает и у процесса без окна. Вне Windows признак — stderr_usable
    (результат ensure_std_streams), как раньше.
    """
    if not _windows():
        return stderr_usable
    return bool(_kernel32().GetConsoleWindow())
