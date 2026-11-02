"""Запуск сервиса File Picker API.

Модуль не импортирует app.api и app.settings на уровне модуля:
настройки читаются только в create_app(), после настройки журнала.
"""

import logging
from pathlib import Path

import uvicorn

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
    ensure_std_streams()
    # До запуска uvicorn логирование не настроено: без basicConfig
    # предупреждение ушло бы в stderr без времени и уровня
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    from pydantic import ValidationError

    from app.api import create_app

    try:
        app = create_app()
    except ValidationError:
        # Например, FILES_DIRECTORY в .env: путь задаётся только сборкой
        logger.exception("Ошибка настроек, сервис не запущен")
        return 1
    ensure_files_directory()
    uvicorn.run(app, host=HOST, port=PORT)
    return 0
