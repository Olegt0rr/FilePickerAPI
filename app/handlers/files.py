"""Обработчики для работы с файлами."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import Annotated

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.settings import get_settings

router = APIRouter(prefix="/files", tags=["files"])

# Максимальный размер файла для импорта (10 МБ)
MAX_AVAILABLE_FILE_SIZE = 10 * 1024 * 1024


def check_file_availability(file_path: Path, file_size: int) -> bool:
    """Проверить, доступен ли файл.

    Файл считается доступным, если:
    - Размер меньше 10 МБ
    - Формат .txt

    Args:
        file_path: Путь к файлу
        file_size: Размер файла в байтах

    Returns:
        True, если файл доступен

    """
    return file_size < MAX_AVAILABLE_FILE_SIZE and file_path.suffix.lower() == ".txt"


class CamelCaseModel(BaseModel):
    """Базовая модель с автоматическим преобразованием в camelCase."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )


class FileInfo(CamelCaseModel):
    """Модель информации о файле."""

    id: str = Field(description="Идентификатор файла (имя файла)")
    name: str = Field(description="Имя файла")
    size: int = Field(description="Размер файла в байтах")
    created_at: datetime = Field(
        description=(
            "Дата и время создания файла в формате ISO 8601 (UTC); если ОС "
            "не отдаёт время создания, то время последнего изменения"
        )
    )


class FileListResponse(CamelCaseModel):
    """Модель ответа со списком файлов."""

    available_files: list[FileInfo]
    unavailable_files: list[FileInfo]


@contextmanager
def _map_os_errors(target: str, not_found: str | None = None) -> Iterator[None]:
    """Превратить ошибки ОС в HTTP-ошибки с понятным detail.

    Args:
        target: Что читалось: "directory" или "file"
        not_found: detail для 404, если путь не найден; без него
            отсутствие пути считается ошибкой ввода-вывода

    Raises:
        HTTPException: 404 если путь не найден (при заданном
            not_found), 403 при отказе в доступе, 500 при прочих
            ошибках ввода-вывода

    """
    try:
        yield
    except (FileNotFoundError, NotADirectoryError) as e:
        if not_found is None:
            msg = f"OS error when reading {target}: {e!s}"
            raise HTTPException(status_code=500, detail=msg) from e
        raise HTTPException(status_code=404, detail=not_found) from e
    except PermissionError as e:
        msg = f"Permission denied when reading {target}: {e!s}"
        raise HTTPException(status_code=403, detail=msg) from e
    except OSError as e:
        msg = f"OS error when reading {target}: {e!s}"
        raise HTTPException(status_code=500, detail=msg) from e


def _collect_file_info(files_path: Path) -> list[FileInfo]:
    """Собрать информацию о всех .txt файлах в директории.

    Args:
        files_path: Путь к директории с файлами

    Returns:
        Список FileInfo объектов для всех .txt файлов

    Raises:
        HTTPException: При ошибках доступа к файловой системе

    """
    file_list = []
    try:
        with _map_os_errors("directory"):
            for item in files_path.iterdir():
                # Игнорируем директории, обрабатываем только файлы
                if not item.is_file():
                    continue
                # Игнорируем файлы, которые не являются .txt
                if item.suffix.lower() != ".txt":
                    continue
                stat = item.stat()
                file_list.append(
                    FileInfo(
                        id=item.name,
                        name=item.name,
                        size=stat.st_size,
                        created_at=datetime.fromtimestamp(
                            getattr(stat, "st_birthtime", stat.st_mtime), tz=UTC
                        ),
                    )
                )
    except HTTPException:
        raise
    except Exception as e:
        msg = f"Unexpected error when reading directory: {e!s}"
        raise HTTPException(status_code=500, detail=msg) from e

    return file_list


def _categorize_files(
    file_list: list[FileInfo],
) -> tuple[list[FileInfo], list[FileInfo]]:
    """Разделить файлы на доступные и недоступные.

    Args:
        file_list: Список всех файлов

    Returns:
        Кортеж (доступные_файлы, недоступные_файлы)

    """
    available_files = []
    unavailable_files = []
    for file_info in file_list:
        if check_file_availability(Path(file_info.name), file_info.size):
            available_files.append(file_info)
        else:
            unavailable_files.append(file_info)

    return available_files, unavailable_files


# Обработчики синхронные: FastAPI выполняет их в пуле потоков, поэтому
# медленный ответ сетевой папки не блокирует цикл событий
@router.get("", response_model=FileListResponse)
def list_files() -> FileListResponse:
    """Получить список всех файлов в настроенной директории.

    Только файлы .txt возвращаются в ответе. Файлы
    разделяются на две категории:
    - availableFiles: файлы .txt размером меньше 10 МБ
    - unavailableFiles: файлы .txt размером 10 МБ и больше

    Returns:
        Объект с двумя списками файлов, отсортированными по createdAt
        (новые первыми)

    """
    files_path = Path(get_settings().files_directory)

    # Один stat() вместо exists() + is_dir(): меньше обращений к
    # сетевой папке и одинаковое поведение на всех версиях Python
    with _map_os_errors("directory", not_found="Files directory not found"):
        dir_stat = files_path.stat()

    if not S_ISDIR(dir_stat.st_mode):
        msg = "Files path is not a directory"
        raise HTTPException(status_code=400, detail=msg)

    # Собрать информацию о всех .txt файлах
    file_list = _collect_file_info(files_path)

    # Сортировка файлов по createdAt (новые первыми)
    file_list.sort(key=lambda x: x.created_at, reverse=True)

    # Разделение файлов на доступные и недоступные
    available_files, unavailable_files = _categorize_files(file_list)

    return FileListResponse(
        available_files=available_files,
        unavailable_files=unavailable_files,
    )


@router.get("/{fileId}")
def get_file(
    file_id: Annotated[str, PathParam(alias="fileId")],
) -> FileResponse:
    """Скачать определенный файл из настроенной директории.

    Args:
        file_id: ID файла для загрузки (имя файла)

    Returns:
        Ответ с запрашиваемым файлом

    Raises:
        HTTPException: 403 если файл недоступен для загрузки
            (не соответствует критериям availableFiles)

    """
    files_directory = Path(get_settings().files_directory)
    try:
        with _map_os_errors("file"):
            base_dir = files_directory.resolve()
            file_path = (files_directory / file_id).resolve()
    except ValueError as e:
        # Например, NUL-байт в fileId
        msg = "Invalid filename"
        raise HTTPException(status_code=400, detail=msg) from e

    # Безопасность: отдаём только файлы непосредственно из директории,
    # как и в списке. Это отсекает и выход наружу через "..", и
    # поддиректории (на Windows fileId с %5C указывает в поддиректорию)
    if file_path.parent != base_dir:
        msg = "Invalid filename"
        raise HTTPException(status_code=400, detail=msg)

    with _map_os_errors("file", not_found="File not found"):
        file_stat = file_path.stat()

    if not S_ISREG(file_stat.st_mode):
        msg = "Path is not a file"
        raise HTTPException(status_code=400, detail=msg)

    # Проверяем, что файл доступен для загрузки
    if not check_file_availability(file_path, file_stat.st_size):
        msg = "File is not available for download"
        raise HTTPException(status_code=403, detail=msg)

    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream",
        stat_result=file_stat,
    )
