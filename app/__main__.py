"""Точка входа для запуска приложения File Picker API."""

import logging
from pathlib import Path

import uvicorn

from app import app, get_settings

logger = logging.getLogger(__name__)


def ensure_files_directory() -> None:
    """Создать директорию для файлов, если её ещё нет.

    Ошибка создания не останавливает запуск: если сетевая папка
    недоступна, сервис стартует, а GET /files отвечает ошибкой
    (404, 403 или 500), пока папка не станет доступна.
    """
    files_directory = get_settings().files_directory
    try:
        Path(files_directory).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(
            "Не удалось создать директорию с файлами %s: %s", files_directory, e
        )


if __name__ == "__main__":
    # До запуска uvicorn логирование не настроено: без basicConfig
    # предупреждение ушло бы в stderr без времени и уровня
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    ensure_files_directory()
    uvicorn.run(app, host="0.0.0.0", port=8000)
