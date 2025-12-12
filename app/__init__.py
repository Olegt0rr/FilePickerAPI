"""Пакет приложения File Picker API."""

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.handlers import files

# Конфигурация
FILES_DIRECTORY = os.getenv("FILES_DIRECTORY", "./files")
cors_origins_str = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS = [
    origin.strip() for origin in cors_origins_str.split(",") if origin.strip()
] or ["*"]

app = FastAPI(
    title="File Picker API",
    description="API for listing and downloading files from a configured directory",
    version="1.0.0",
)

# Включение CORS для фронтенд-приложений
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Подключение обработчиков
app.include_router(files.router)

__all__ = ["FILES_DIRECTORY", "app"]
