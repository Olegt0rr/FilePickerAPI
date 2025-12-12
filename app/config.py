"""Конфигурация приложения."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки приложения File Picker API."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    files_directory: str = "./files"
    cors_origins: str = "*"

    @property
    def cors_origins_list(self) -> list[str]:
        """Получить список CORS origins из строки."""
        origins = [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]
        return origins or ["*"]


settings = Settings()
