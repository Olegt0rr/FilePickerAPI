"""FastAPI приложение для просмотра списка файлов и их загрузки."""

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

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


class FileInfo(BaseModel):
    """Модель информации о файле."""

    name: str
    size: int
    is_file: bool


@app.get("/")
async def root() -> dict[str, str | dict[str, str]]:
    """Корневая конечная точка."""
    return {
        "message": "File Picker API",
        "endpoints": {
            "/files": "List all files",
            "/files/{filename}": "Download a specific file",
        },
    }


@app.get("/files", response_model=list[FileInfo])
async def list_files() -> list[FileInfo]:
    """Получить список всех файлов в настроенной директории.

    Returns:
        Список объектов с информацией о файлах

    """
    files_path = Path(FILES_DIRECTORY)

    if not files_path.exists():
        msg = "Files directory not found"
        raise HTTPException(status_code=404, detail=msg)

    if not files_path.is_dir():
        msg = "Files path is not a directory"
        raise HTTPException(status_code=400, detail=msg)

    file_list = []
    try:
        for item in files_path.iterdir():
            is_file = item.is_file()
            file_list.append(
                FileInfo(
                    name=item.name,
                    size=item.stat().st_size if is_file else 0,
                    is_file=is_file,
                )
            )
    except Exception as e:
        msg = f"Error reading directory: {e!s}"
        raise HTTPException(status_code=500, detail=msg) from e

    return sorted(file_list, key=lambda x: x.name)


@app.get("/files/{filename}")
async def get_file(filename: str) -> FileResponse:
    """Скачать определенный файл из настроенной директории.

    Args:
        filename: Имя файла для загрузки

    Returns:
        Ответ с запрашиваемым файлом

    """
    # Безопасность: предотвращение обхода директорий
    # Получаем абсолютные пути и проверяем, что файл находится
    # в разрешенной директории
    base_dir = Path(FILES_DIRECTORY).resolve()
    requested_path = (Path(FILES_DIRECTORY) / filename).resolve()

    # Проверяем, что разрешенный путь находится внутри
    # базовой директории
    try:
        common_path = os.path.commonpath([base_dir, requested_path])
        if common_path != str(base_dir):
            msg = "Invalid filename"
            raise HTTPException(status_code=400, detail=msg)
    except ValueError as e:
        msg = "Invalid filename"
        raise HTTPException(status_code=400, detail=msg) from e

    file_path = Path(requested_path)

    if not file_path.exists():
        msg = "File not found"
        raise HTTPException(status_code=404, detail=msg)

    if not file_path.is_file():
        msg = "Path is not a file"
        raise HTTPException(status_code=400, detail=msg)

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
    )
