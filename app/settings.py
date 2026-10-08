"""Настройки приложения."""

from functools import lru_cache
from typing import ClassVar

from pydantic_settings import BaseSettings, SettingsConfigDict

try:
    # Файл генерируется в CI под конкретный филиал (см. branches.toml)
    from app._build_config import FILES_DIRECTORY
except ImportError:
    # Локальный запуск и Docker: файлы из ./files
    FILES_DIRECTORY = "./files"


class Settings(BaseSettings):
    """Настройки приложения File Picker API."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    files_directory: ClassVar[str] = FILES_DIRECTORY
    cors_origins: str = "*"

    @property
    def cors_origins_list(self) -> list[str]:
        """Получить список CORS origins из строки."""
        origins = [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]
        return origins or ["*"]


@lru_cache
def get_settings() -> Settings:
    """Получить настройки приложения (кэшируемая функция)."""
    return Settings()
