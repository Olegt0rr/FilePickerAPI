"""Обработчики для работы с файлами."""

import os
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.settings import get_settings

router = APIRouter(prefix="/files", tags=["files"])

# Максимальный размер файла для импорта (10 МБ)
MAX_AVAILABLE_FILE_SIZE = 10 * 1024 * 1024


def is_file_available(file_info: "FileInfo") -> bool:
    """Проверить, доступен ли файл для импорта.

    Файл считается доступным, если:
    - Размер меньше 10 МБ
    - Формат .txt

    Args:
        file_info: Информация о файле

    Returns:
        True, если файл доступен для импорта

    """
    return file_info.size < MAX_AVAILABLE_FILE_SIZE and file_info.name.lower().endswith(
        ".txt"
    )


class CamelCaseModel(BaseModel):
    """Базовая модель с автоматическим преобразованием в camelCase."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class FileInfo(CamelCaseModel):
    """Модель информации о файле."""

    id: str
    name: str
    size: int
    created_at: float


class FileListResponse(CamelCaseModel):
    """Модель ответа со списком файлов."""

    available_files: list[FileInfo]
    not_available_files: list[FileInfo]


@router.get("", response_model=FileListResponse)
async def list_files() -> FileListResponse:
    """Получить список всех файлов в настроенной директории.

    Файлы разделяются на две категории:
    - availableFiles: файлы .txt размером меньше 10 МБ
    - notAvailableFiles: файлы других форматов или размером
      10 МБ и больше

    Returns:
        Объект с двумя списками файлов, отсортированными по дате
        создания (новые первыми)

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
            # Игнорируем директории, обрабатываем только файлы
            if not item.is_file():
                continue
            stat = item.stat()
            file_list.append(
                FileInfo(
                    id=item.name,
                    name=item.name,
                    size=stat.st_size,
                    created_at=stat.st_ctime,
                )
            )
    except Exception as e:
        msg = f"Error reading directory: {e!s}"
        raise HTTPException(status_code=500, detail=msg) from e

    # Разделение файлов на доступные и недоступные
    available_files = []
    not_available_files = []
    for file_info in file_list:
        if is_file_available(file_info):
            available_files.append(file_info)
        else:
            not_available_files.append(file_info)

    return FileListResponse(
        available_files=sorted(
            available_files, key=lambda x: x.created_at, reverse=True
        ),
        not_available_files=sorted(
            not_available_files, key=lambda x: x.created_at, reverse=True
        ),
    )


@router.get("/{fileId}")
async def get_file(
    file_id: Annotated[str, PathParam(alias="fileId")],
) -> FileResponse:
    """Скачать определенный файл из настроенной директории.

    Args:
        file_id: ID файла для загрузки (имя файла)

    Returns:
        Ответ с запрашиваемым файлом

    """
    # Безопасность: предотвращение обхода директорий
    # Получаем абсолютные пути и проверяем, что файл находится
    # в разрешенной директории
    base_dir = Path(get_settings().files_directory).resolve()
    requested_path = (Path(get_settings().files_directory) / file_id).resolve()

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
