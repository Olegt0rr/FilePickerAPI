"""HTTP-приложение File Picker API."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.handlers import files
from app.settings import get_settings


def create_app() -> FastAPI:
    """Создать FastAPI-приложение.

    Настройки (и .env) читаются здесь, а не при импорте модуля:
    ошибка в них (например, FILES_DIRECTORY в .env) — ValidationError
    из этой функции.
    """
    app = FastAPI(
        title="File Picker API",
        description="API for listing and downloading files from a configured directory",
        version=__version__,
    )

    # Включение CORS для фронтенд-приложений. Учётные данные
    # разрешаются только для явного списка источников: при `*`
    # Starlette иначе возвращает Origin запроса и пускает запросы с
    # куками с любого сайта.
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
    return app
