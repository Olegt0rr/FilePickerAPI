"""Обработчики для работы с файлами."""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/files", tags=["files"])


class FileInfo(BaseModel):
    """Модель информации о файле."""

    name: str
    size: int
    is_file: bool


@router.get("", response_model=list[FileInfo])
async def list_files() -> list[FileInfo]:
    """Получить список всех файлов в настроенной директории.

    Returns:
        Список объектов с информацией о файлах

    """
    files_path = Path(settings.files_directory)

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


@router.get("/{filename}")
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
    base_dir = Path(settings.files_directory).resolve()
    requested_path = (Path(settings.files_directory) / filename).resolve()

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
