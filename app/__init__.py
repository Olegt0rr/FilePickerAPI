"""Пакет приложения File Picker API."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.handlers import files
from app.settings import get_settings

app = FastAPI(
    title="File Picker API",
    description="API for listing and downloading files from a configured directory",
    version="1.0.0",
)

# Включение CORS для фронтенд-приложений
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение обработчиков
app.include_router(files.router)

__all__ = ["app", "get_settings"]
