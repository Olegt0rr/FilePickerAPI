"""Точка входа для запуска приложения File Picker API."""

from pathlib import Path

import uvicorn

from app import app, get_settings

if __name__ == "__main__":
    # Создаем директорию для файлов, если она не существует
    Path(get_settings().files_directory).mkdir(parents=True, exist_ok=True)

    uvicorn.run(app, host="0.0.0.0", port=8000)
