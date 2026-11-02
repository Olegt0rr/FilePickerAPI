"""Стандартные потоки процесса без консоли.

Лёгкий модуль: его импортирует точка входа до разбора аргументов.
"""

import io
import os
import sys
from pathlib import Path


def _stream_is_unusable(stream: object) -> bool:
    """Проверить: потока нет, он закрыт или с плохим дескриптором."""
    if stream is None or getattr(stream, "closed", False):
        return True
    try:
        os.fstat(stream.fileno())
    except (AttributeError, io.UnsupportedOperation):
        # Поток без дескриптора (например, перехват вывода в тестах)
        return False
    except (OSError, ValueError):
        return True
    return False


def ensure_std_streams() -> None:
    """Подставить os.devnull вместо непригодных stdout и stderr.

    Exe с --windows-console-mode=attach, запущенный без консоли (при
    входе пользователя, двойным щелчком, из run_production.bat), может
    получить sys.stdout и sys.stderr равными None, как pythonw, или
    потоки на недействительном дескрипторе. Тогда журнал Uvicorn падал
    бы на sys.stdout.isatty() или на первой записи.
    """
    for name in ("stdout", "stderr"):
        if _stream_is_unusable(getattr(sys, name)):
            # Поток живёт до конца процесса, закрывать его некому
            stream = Path(os.devnull).open("w", encoding="utf-8")  # noqa: SIM115
            setattr(sys, name, stream)
