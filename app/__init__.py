"""Пакет приложения File Picker API.

Импорт пакета не создаёт HTTP-приложение и не читает настройки и
.env: это делает app.api.create_app() при запуске сервиса, уже после
настройки журнала, поэтому ошибка в .env попадает в журнал.
"""

try:
    # Файл генерирует setuptools-scm при pip install (в git его нет)
    from app._version import __version__
except ImportError:  # Запуск без установки пакета
    __version__ = "0.0.0"

__all__ = ["__version__"]
