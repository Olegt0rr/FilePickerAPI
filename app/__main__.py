"""Точка входа File Picker API: запуск сервиса без аргументов.

Модуль не импортирует app.api и app.settings: настройки читаются
только при создании HTTP-приложения, после настройки журнала.
"""

import os
import sys
from pathlib import Path

from app.stdio import ensure_std_streams

# Аргументы не принимаются: директория с файлами зашита в сборку, и
# задать её при запуске нельзя (требование, см. CLAUDE.md)
USAGE = (
    "Использование: FilePickerAPI.exe\n"
    "Сервис запускается без аргументов. Директория с файлами задаётся "
    "при сборке и при запуске не меняется."
)


def chdir_to_exe() -> None:
    """Перейти в папку собранного exe.

    Explorer запускает программы из Run с рабочей папкой профиля
    пользователя, а .env читается из рабочей папки. Путь к exe даёт
    только GetModuleFileName: в сборке Nuitka sys.executable указывает
    на несуществующий python.exe. `python -m app` работает в текущей
    папке, как раньше.
    """
    if "__compiled__" not in globals():
        return
    import win32api

    os.chdir(Path(win32api.GetModuleFileName(None)).parent)


def main(argv: list[str] | None = None) -> int:
    """Разобрать аргументы, запустить сервис и вернуть код выхода."""
    args = sys.argv[1:] if argv is None else argv
    if args:
        # Exe без консоли: подсказка уходит в os.devnull, а не падает
        ensure_std_streams()
        sys.stderr.write(USAGE + "\n")
        return 2
    chdir_to_exe()

    from app.runner import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
