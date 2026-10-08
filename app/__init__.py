"""Пакет приложения File Picker API."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.handlers import files
from app.settings import get_settings

try:
    # Файл генерирует setuptools-scm при pip install (в git его нет)
    from app._version import __version__
except ImportError:  # pragma: no cover - запуск без установки пакета
    __version__ = "0.0.0"

app = FastAPI(
    title="File Picker API",
    description="API for listing and downloading files from a configured directory",
    version=__version__,
)

# Включение CORS для фронтенд-приложений. Учётные данные разрешаются
# только для явного списка источников: при `*` Starlette иначе
# возвращает Origin запроса и пускает запросы с куками с любого сайта.
cors_origins = get_settings().cors_origins_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials="*" not in cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение обработчиков
app.include_router(files.router)

__all__ = ["app", "get_settings"]
