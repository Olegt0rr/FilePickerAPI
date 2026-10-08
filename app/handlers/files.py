"""Обработчики для работы с файлами."""

import errno
import functools
import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from stat import S_ISDIR, S_ISREG
from typing import Annotated

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.settings import get_settings

logger = logging.getLogger(__name__)

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
            "Дата и время создания файла в формате ISO 8601 (UTC); там, где "
            "ОС не отдаёт время создания (Linux), — время последнего изменения"
        )
    )


class FileListResponse(CamelCaseModel):
    """Модель ответа со списком файлов."""

    available_files: list[FileInfo]
    unavailable_files: list[FileInfo]


# Ошибки, означающие недопустимое имя файла, а не сбой ФС: на Windows
# ERROR_INVALID_NAME, ERROR_FILENAME_EXCED_RANGE и
# ERROR_CANT_RESOLVE_FILENAME, на остальных ОС слишком длинное имя и
# петля символических ссылок
_INVALID_NAME_WINERRORS = frozenset({123, 206, 1921})
_INVALID_NAME_ERRNOS = frozenset({errno.ENAMETOOLONG, errno.ELOOP})


def _is_invalid_name_error(error: OSError) -> bool:
    """Проверить, вызвана ли ошибка ОС недопустимым именем файла."""
    winerror = getattr(error, "winerror", None)
    if winerror is not None:
        return winerror in _INVALID_NAME_WINERRORS
    return error.errno in _INVALID_NAME_ERRNOS


@contextmanager
def _directory_errors(*, not_found: bool = False) -> Iterator[None]:
    """Превратить ошибки ОС при работе с директорией в HTTP-ошибки.

    Текст исходной ошибки попадает в detail.

    Args:
        not_found: Отвечать 404 «Files directory not found», если
            директория не найдена; иначе это ошибка ввода-вывода

    Raises:
        HTTPException: 404, 403 при отказе в доступе или 500 при
            прочих ошибках ввода-вывода

    """
    try:
        yield
    except OSError as e:
        if not_found and isinstance(e, (FileNotFoundError, NotADirectoryError)):
            msg = "Files directory not found"
            raise HTTPException(status_code=404, detail=msg) from e
        if isinstance(e, PermissionError):
            msg = f"Permission denied when reading directory: {e!s}"
            raise HTTPException(status_code=403, detail=msg) from e
        msg = f"OS error when reading directory: {e!s}"
        raise HTTPException(status_code=500, detail=msg) from e


@contextmanager
def _file_errors(file_id: str) -> Iterator[None]:
    """Превратить ошибки при поиске запрошенного файла в HTTP-ошибки.

    Текст исходной ошибки в detail не попадает: в нём полный путь к
    сетевой папке. Он пишется в лог.

    Args:
        file_id: Запрошенный идентификатор файла

    Raises:
        HTTPException: 400 при недопустимом имени, 404 если файла нет,
            403 при отказе в доступе, 500 при прочих ошибках

    """
    try:
        yield
    except (ValueError, RuntimeError) as e:
        # ValueError — NUL-байт в имени, RuntimeError — петля
        # символических ссылок в resolve() на Python 3.11
        msg = "Invalid filename"
        raise HTTPException(status_code=400, detail=msg) from e
    except OSError as e:
        if _is_invalid_name_error(e):
            msg = "Invalid filename"
            raise HTTPException(status_code=400, detail=msg) from e
        if isinstance(e, (FileNotFoundError, NotADirectoryError)):
            msg = "File not found"
            raise HTTPException(status_code=404, detail=msg) from e
        logger.warning("Ошибка при чтении файла %r: %s", file_id, e)
        if isinstance(e, PermissionError):
            msg = "Permission denied when reading file"
            raise HTTPException(status_code=403, detail=msg) from e
        msg = "OS error when reading file"
        raise HTTPException(status_code=500, detail=msg) from e


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DATETIME_MIN = datetime.min.replace(tzinfo=UTC)


def file_created_at(file_stat: os.stat_result) -> datetime:
    """Вернуть время создания файла, а без него — время изменения.

    На Windows до Python 3.12 время создания лежит в st_ctime, с 3.12 —
    в st_birthtime. На Linux время создания недоступно.
    """
    if hasattr(file_stat, "st_birthtime"):
        timestamp = file_stat.st_birthtime
    elif sys.platform == "win32":
        timestamp = file_stat.st_ctime
    else:
        timestamp = file_stat.st_mtime
    # Не datetime.fromtimestamp: на Windows она падает на времени до
    # 1970 года, а архиваторы и robocopy оставляют и такое
    try:
        return _EPOCH + timedelta(seconds=timestamp)
    except OverflowError:
        # Метка вне 1–9999 годов: метаданные повреждены. Файл всё равно
        # показываем, а не роняем из-за него весь список
        return datetime.max.replace(tzinfo=UTC) if timestamp > 0 else _DATETIME_MIN


def _stat_listed_item(
    entry: os.DirEntry[str], base_dir: Callable[[], Path]
) -> tuple[os.stat_result | None, bool]:
    """Вернуть stat файла для списка или None, если его не показывать.

    Args:
        entry: Элемент директории с расширением .txt
        base_dir: Функция, возвращающая разрешённый путь директории

    Returns:
        Кортеж (stat или None, файл не найден)

    Raises:
        OSError: При прочих ошибках файловой системы

    """
    try:
        # На Windows признак ссылки, размер и время обычного файла уже
        # получены при перечислении, и запроса к сетевой папке нет
        if entry.is_symlink():
            # Ссылку показываем, только если её можно скачать: цель —
            # .txt прямо в директории (см. get_file)
            target = Path(entry.path).resolve()
            if target.parent != base_dir() or target.suffix.lower() != ".txt":
                return None, False
        # Для ссылки stat() идёт к цели
        file_stat = entry.stat()
    except RuntimeError:
        # Петля ссылок в resolve() на Python 3.11
        return None, False
    except OSError as e:
        # Как и прежний is_file(), пропускаем битые ссылки, петли
        # ссылок и файлы, удалённые после чтения списка
        if isinstance(e, (FileNotFoundError, NotADirectoryError)):
            return None, True
        if _is_invalid_name_error(e):
            return None, False
        raise
    # Директории не показываем
    if not S_ISREG(file_stat.st_mode):
        return None, False
    return file_stat, False


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
    # Разрешаем путь директории, только если встретилась ссылка
    base_dir = functools.cache(files_path.resolve)
    skipped_missing = False
    try:
        # Директория могла исчезнуть после проверки в list_files
        # Отбор по расширению сразу при перечислении: он не требует
        # обращения к ФС, и прочие элементы не держатся в памяти
        with _directory_errors(not_found=True), os.scandir(files_path) as it:
            entries = [
                entry for entry in it if Path(entry.name).suffix.lower() == ".txt"
            ]
        with _directory_errors():
            for entry in entries:
                file_stat, missing = _stat_listed_item(entry, base_dir)
                skipped_missing |= missing
                if file_stat is None:
                    continue
                file_list.append(
                    FileInfo(
                        id=entry.name,
                        name=entry.name,
                        size=file_stat.st_size,
                        created_at=file_created_at(file_stat),
                    )
                )
        if skipped_missing:
            # Пропавшие файлы могут означать, что пропала вся директория
            # (например, отвалилась сетевая папка): тогда это 404, а не
            # пустой список
            with _directory_errors(not_found=True):
                files_path.stat()
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
    with _directory_errors(not_found=True):
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


# Разрешённый путь к директории с файлами. Ключ — только путь из
# настроек, зашитый в сборку, поэтому кэш не даёт способа подменить
# директорию. Сюда попадает только результат строгого resolve()
_real_path_cache: dict[Path, Path] = {}


def _files_directory_real_path(files_directory: Path, *, refresh: bool = False) -> Path:
    """Вернуть разрешённый путь к директории с файлами.

    Директория не меняется, поэтому путь разрешается один раз: это
    экономит запросы к сетевой папке на каждом скачивании. Строгий
    resolve() либо разрешает путь целиком, либо падает, и тогда кэш
    не заполняется, а прежний путь из него убирается.

    Args:
        files_directory: Директория из настроек
        refresh: Разрешить путь заново, не глядя в кэш. Кэш заранее
            не очищается, и параллельные скачивания им пользуются

    Raises:
        HTTPException: 500 при сбое самой директории, без пути в
            detail: это ошибка сервера, а не клиента

    """
    cached = _real_path_cache.get(files_directory)
    if cached is not None and not refresh:
        return cached
    try:
        try:
            real_path = files_directory.resolve(strict=True)
        except OSError:
            # Папка недоступна: нестрогий resolve(), как и раньше, но
            # без кэша — он может вернуть неразрешённый путь
            _real_path_cache.pop(files_directory, None)
            return files_directory.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        _real_path_cache.pop(files_directory, None)
        logger.warning("Не удалось разрешить путь к директории с файлами: %s", e)
        msg = "OS error when reading file"
        raise HTTPException(status_code=500, detail=msg) from e
    _real_path_cache[files_directory] = real_path
    return real_path


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

    # Безопасность: fileId должен быть просто именем, и проверяется это
    # до любого обращения к ФС. Абсолютный путь (на Windows и
    # UNC-путь \\host\share) заменил бы директорию при склейке, и
    # resolve() пошёл бы на чужой хост, отдав ему NTLM-хеш учётной
    # записи сервиса. На Windows в именах файлов не бывает двоеточия
    # (диск или альтернативный поток NTFS) и точек или пробелов в
    # конце: Win32 их отбрасывает, и ".. " превращается в ".."
    candidate = files_directory / file_id
    if (
        # Склейка нормализует путь: на Windows ".\\x.txt", "x.txt\\" и
        # полный путь к файлу в директории — тот же файл, но не имя
        candidate.name != file_id
        or "\x00" in file_id
        or file_id in {".", ".."}
        or (sys.platform == "win32" and (":" in file_id or file_id[-1] in ". "))
    ):
        msg = "Invalid filename"
        raise HTTPException(status_code=400, detail=msg)

    base_dir = _files_directory_real_path(files_directory)

    with _file_errors(file_id):
        file_path = candidate.resolve()

    # Отдаём только файлы непосредственно из директории, как и в
    # списке: символическая ссылка не должна уводить в поддиректорию
    # или за пределы директории. Перед отказом сверяемся с заново
    # разрешённым путём: закэшированный мог устареть (например, DFS
    # переключил сетевую папку на другой сервер)
    if file_path.parent != base_dir and file_path.parent != (
        _files_directory_real_path(files_directory, refresh=True)
    ):
        msg = "Invalid filename"
        raise HTTPException(status_code=400, detail=msg)

    with _file_errors(file_id):
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
