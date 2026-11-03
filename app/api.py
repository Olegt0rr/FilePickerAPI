"""HTTP-приложение File Picker API."""

import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse, Response

from app import __version__, event_tracking
from app.event_tracking import EventId, is_tracked
from app.handlers import files
from app.settings import get_settings


def _route(request: Request) -> str:
    """Маршрут запроса для ключа повторов: шаблон, а не fileId."""
    route = request.scope.get("route")
    path = getattr(route, "path_format", request.url.path)
    return f"{request.method} {path}"


async def _unhandled_error(request: Request, exc: Exception) -> Response:
    """Непредвиденное исключение запроса: событие 4000 и ответ 500.

    Ответ тот же, что у Starlette без обработчика. После него Starlette
    выбрасывает исключение дальше, и Uvicorn пишет его в uvicorn.error:
    исключение помечено учтённым, и второго события не будет.
    """
    if not is_tracked(exc):
        route = _route(request)
        event_tracking.track(
            EventId.UNEXPECTED_ERROR,
            (route, type(exc).__name__),
            f"Непредвиденная ошибка запроса {request.method} "
            f"{request.url.path}: {event_tracking.describe_error(exc)}",
            level=logging.ERROR,
            exc=exc,
        )
    return PlainTextResponse("Internal Server Error", status_code=500)


async def _http_error(request: Request, exc: HTTPException) -> Response:
    """Ответ HTTPException без изменений; для 500 и выше — событие.

    Событие пишется по исходной причине (exc.__cause__) с её
    трассировкой, если обработчики её ещё не учли: ошибка ОС — через
    учёт директории и файлов (2000 или 3000), прочее — 4000.
    """
    cause = exc.__cause__
    if exc.status_code >= 500 and not is_tracked(cause):  # noqa: PLR2004 - класс 5xx
        directory = get_settings().files_directory
        file_id = request.path_params.get("fileId")
        if isinstance(cause, OSError) and file_id is not None:
            event_tracking.file_error(directory, file_id, cause, exc.status_code)
        elif isinstance(cause, OSError):
            event_tracking.directory_error(directory, cause, exc.status_code)
        else:
            route = _route(request)
            event_tracking.track(
                EventId.UNEXPECTED_ERROR,
                (route, type(cause).__name__),
                f"Ошибка {exc.status_code} запроса {request.method} "
                f"{request.url.path}: {exc.detail}",
                level=logging.ERROR,
                exc=cause,
            )
    return await http_exception_handler(request, exc)


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

    # Одна ошибка — одно событие в журнале событий (см. event_tracking)
    app.add_exception_handler(Exception, _unhandled_error)
    app.add_exception_handler(HTTPException, _http_error)

    # Подключение обработчиков
    app.include_router(files.router)
    return app
