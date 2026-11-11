"""Стандартные потоки процесса без консоли.

Лёгкий модуль: его импортирует точка входа до разбора аргументов.
"""

import io
import os
import sys
from pathlib import Path

STREAMS = ("stdout", "stderr")


def _stream_is_unusable(stream: object) -> bool:
    """Проверить: потока нет, он закрыт, дескриптор плох или это NUL.

    Exe Nuitka (--windows-console-mode=attach), запущенный без консоли,
    получает от Nuitka потоки, открытые на NUL: os.fstat на них
    проходит, но вывод никуда не попадает. Вне Windows /dev/null
    считается пригодным, как раньше.
    """
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
    if sys.platform != "win32":
        return False
    from app import winapi

    return winapi.is_null_device(fd)


def ensure_std_streams() -> bool:
    """Подставить os.devnull вместо непригодных stdout и stderr.

    Exe с --windows-console-mode=attach, запущенный без консоли (при
    входе пользователя, двойным щелчком), может получить sys.stdout и
    sys.stderr равными None, как pythonw, потоки на недействительном
    дескрипторе или на NUL (так делает Nuitka). Тогда журнал Uvicorn
    падал бы на sys.stdout.isatty() или на первой записи.

    Возвращает, был ли stderr пригоден, то есть есть ли куда выводить
    журнал. Есть ли консоль, говорит winapi.has_console_window().
    """
    usable = {name: not _stream_is_unusable(getattr(sys, name)) for name in STREAMS}
    for name, ok in usable.items():
        if not ok:
            # Поток живёт до конца процесса, закрывать его некому
            stream = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115
            setattr(sys, name, stream)
    return usable["stderr"]
