"""Обработчики для работы с файлами."""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.settings import get_settings

router = APIRouter(prefix="/files", tags=["files"])


class FileInfo(BaseModel):
    """Модель информации о файле."""

    name: str
    size: int
    is_file: bool


class FileListResponse(BaseModel):
    """Модель ответа со списком файлов, отфильтрованных по размеру."""

    available_files: list[FileInfo] = Field(..., serialization_alias="availableFiles")
    not_available_files: list[FileInfo] = Field(
        ..., serialization_alias="notAvailableFiles"
    )


@router.get("", response_model=FileListResponse)
async def list_files() -> FileListResponse:
    """Получить список всех файлов в настроенной директории.

    Файлы разделяются на две категории:
    - availableFiles: файлы размером меньше 10 МБ
    - notAvailableFiles: файлы размером 10 МБ и больше

    Returns:
        Объект с двумя списками файлов

    """
    files_path = Path(get_settings().files_directory)

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

    # Фильтрация файлов по размеру (10 МБ = 10 * 1024 * 1024 байт)
    ten_mb = 10 * 1024 * 1024
    available_files = [f for f in file_list if f.size < ten_mb]
    not_available_files = [f for f in file_list if f.size >= ten_mb]

    return FileListResponse(
        available_files=sorted(available_files, key=lambda x: x.name),
        not_available_files=sorted(not_available_files, key=lambda x: x.name),
    )


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
    base_dir = Path(get_settings().files_directory).resolve()
    requested_path = (Path(get_settings().files_directory) / filename).resolve()

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
