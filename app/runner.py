"""Запуск сервиса File Picker API.

Модуль не импортирует app.api и app.settings на уровне модуля:
настройки читаются только в create_app(), после настройки журнала.
"""

import logging
from pathlib import Path

import uvicorn

from app.logging_setup import send_startup_events, setup_logging
from app.stdio import ensure_std_streams

logger = logging.getLogger(__name__)

# Сервис слушает все интерфейсы на порту 8000
HOST = "0.0.0.0"
PORT = 8000


def ensure_files_directory() -> None:
    """Создать директорию для файлов, если её ещё нет.

    Ошибка создания не останавливает запуск: если сетевая папка
    недоступна, сервис стартует, а GET /files отвечает ошибкой
    (404, 403 или 500), пока папка не станет доступна.
    """
    from app.settings import Settings

    # ClassVar: путь берётся без создания настроек и чтения .env
    files_directory = Settings.files_directory
    try:
        Path(files_directory).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(
            "Не удалось создать директорию с файлами %s: %s", files_directory, e
        )


def run() -> int:
    """Запустить сервис и вернуть код выхода процесса."""
    setup_logging(console=ensure_std_streams())

    from pydantic import ValidationError

    from app.api import create_app

    try:
        app = create_app()
    except ValidationError:
        # Например, FILES_DIRECTORY в .env: путь задаётся только сборкой
        logger.exception("Ошибка настроек, сервис не запущен")
        send_startup_events()
        return 1
    ensure_files_directory()
    # Пока нет отслеживания старта HTTP-сервера, буфер стартовых событий
    # отправляется перед запуском Uvicorn
    send_startup_events()
    # log_config=None: Uvicorn пишет через наши обработчики, а не
    # переписывает их своей настройкой
    uvicorn.run(app, host=HOST, port=PORT, log_config=None)
    return 0
