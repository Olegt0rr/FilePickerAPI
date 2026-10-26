"""Обработчики для работы с файлами."""

import ctypes
import errno
import functools
import logging
import os
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from stat import S_ISDIR, S_ISLNK, S_ISREG
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


# Имена устройств Windows. Устройством считается и имя с расширением
# (nul.txt) и пробелами перед ним, поэтому сравнивается часть до первой
# точки. На Windows 11 имена с расширением уже не устройства, но на
# Windows 10 сервис обратился бы к порту или консоли, а не к файлу.
# COM0 и LPT0 Microsoft тоже относит к зарезервированным именам
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"{port}{n}" for port in ("COM", "LPT") for n in "0123456789\xb9\xb2\xb3"}
)

# Символы, недопустимые в именах файлов Windows. Кроме того, это
# шаблоны поиска для FindFirstFileExW (см. _real_file_name)
_WINDOWS_INVALID_CHARS = frozenset('*?"<>|')


def _is_invalid_windows_name(name: str) -> bool:
    """Проверить, что на Windows имя не может быть именем файла.

    В именах файлов Windows не бывает двоеточия (диск или
    альтернативный поток NTFS), символов *?"<>| и точек или пробелов
    в конце: Win32 их отбрасывает, и ".. " превращается в "..". Имена
    устройств (CON, nul.txt, COM1) ведут к устройству, а не к файлу.

    Args:
        name: Непустое имя файла

    Returns:
        True, если сервис работает на Windows и имя недопустимо

    """
    return sys.platform == "win32" and (
        ":" in name
        or not _WINDOWS_INVALID_CHARS.isdisjoint(name)
        or name[-1] in ". "
        or name.partition(".")[0].rstrip(" ").upper() in _WINDOWS_RESERVED_NAMES
    )


class _Win32FindData(ctypes.Structure):
    """Структура WIN32_FIND_DATAW — ответ FindFirstFileExW.

    Поля описаны типами фиксированного размера, а не ctypes.wintypes:
    модуль импортируется и на Linux, где тесты подменяют вызовы WinAPI.
    """

    _fields_ = (
        ("attributes", ctypes.c_uint32),
        ("times", ctypes.c_uint32 * 6),
        ("size", ctypes.c_uint32 * 2),
        ("reserved", ctypes.c_uint32 * 2),
        ("name", ctypes.c_wchar * 260),
        ("alternate_name", ctypes.c_wchar * 14),
    )


# FINDEX_INFO_LEVELS.FindExInfoBasic: без короткого имени 8.3
_FIND_EX_INFO_BASIC = 1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_ERROR_ACCESS_DENIED = 5
# Открытие файла только для чтения атрибутов, без перехода по ссылкам
# (FILE_FLAG_OPEN_REPARSE_POINT); BACKUP_SEMANTICS позволяет открыть и
# директорию, чтобы на неё ответить 400, а не 403
_FILE_READ_ATTRIBUTES = 0x80
_FILE_SHARE_ALL = 0x7
_OPEN_EXISTING = 3
_OPEN_FLAGS = 0x00200000 | 0x02000000
_FILE_NAME_NORMALIZED = 0
# Наибольшая длина пути Windows с префиксом \\?\
_MAX_FINAL_PATH = 32768


@functools.cache
def _kernel32() -> ctypes.CDLL:
    """Загрузить kernel32 и описать сигнатуры используемых функций.

    Один раз на процесс: argtypes — общее состояние объектов функций,
    и задавать его на каждом запросе из потоков пула незачем.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.FindFirstFileExW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    )
    kernel32.FindFirstFileExW.restype = ctypes.c_void_p
    kernel32.FindClose.argtypes = (ctypes.c_void_p,)
    kernel32.CreateFileW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.GetFinalPathNameByHandleW.argtypes = (
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    )
    kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    return kernel32


def _winapi_error(function: str, path: Path, winerror: int) -> OSError:
    """Вернуть OSError для неудачного вызова WinAPI с кодом winerror.

    По коду ошибки Windows OSError сам выбирает подкласс и errno
    (FileNotFoundError, PermissionError). Если код не сохранился (0),
    подкласса не будет, а в журнале было бы «Операция успешно
    завершена», поэтому это явная ошибка ввода-вывода.
    """
    if not winerror:
        msg = f"{function} failed without an error code"
        return OSError(errno.EIO, msg, str(path))
    return OSError(None, f"{function} failed", str(path), winerror)


def _real_file_name(path: Path) -> str:
    """Вернуть имя файла в том виде, в каком оно записано в директории.

    Windows находит файл по имени без учёта регистра и по короткому
    имени 8.3 (REPORT~1.TXT для report.txtx). FindFirstFileExW
    возвращает настоящее длинное имя. Это поиск в директории, а не
    открытие файла, поэтому по символическим ссылкам он не переходит.
    Если права на просмотр директории нет, см.
    _real_file_name_without_listing.

    Args:
        path: Путь к файлу; имя без символов-шаблонов *?"<>

    Returns:
        Имя файла в директории

    Raises:
        OSError: Если файла нет или директория недоступна (на Windows —
            подкласс по коду ошибки, например FileNotFoundError)

    """
    kernel32 = _kernel32()
    data = _Win32FindData()
    handle = kernel32.FindFirstFileExW(
        str(path), _FIND_EX_INFO_BASIC, ctypes.byref(data), 0, None, 0
    )
    if handle not in {None, _INVALID_HANDLE_VALUE}:
        kernel32.FindClose(handle)
        return data.name
    winerror = ctypes.get_last_error()
    # Поиск требует права на просмотр содержимого директории. На
    # ресурсе может быть выдано только чтение файлов: тогда файл,
    # который раньше скачивался, не должен стать 403
    if winerror == _ERROR_ACCESS_DENIED:
        return _real_file_name_without_listing(path)
    function = "FindFirstFileExW"
    raise _winapi_error(function, path, winerror)


def _real_file_name_without_listing(path: Path) -> str:
    """Вернуть настоящее имя файла без права просмотра директории.

    Опасно только короткое имя 8.3: через него report.txtx отдался бы
    как REPORT~1.TXT. Короткие имена, которые создаёт Windows, всегда
    содержат «~», поэтому имя без «~» — это длинное имя самого файла
    (с точностью до регистра), и обойти им проверку расширения нельзя.
    Тогда никаких обращений к ФС не нужно, в том числе к родительским
    папкам, к которым у учётной записи может не быть доступа. Только
    для имени с «~» настоящее имя запрашивается по дескриптору.

    Args:
        path: Путь к файлу

    Returns:
        Имя файла в директории

    Raises:
        OSError: Если для имени с «~» настоящее имя получить не удалось

    """
    if "~" not in path.name:
        return path.name
    return _real_file_name_by_handle(path)


def _real_file_name_by_handle(path: Path) -> str:
    """Вернуть настоящее имя файла по его дескриптору.

    Файл открывается только для чтения атрибутов и без перехода по
    ссылкам, а GetFinalPathNameByHandleW возвращает нормализованный
    путь с длинными именами в их регистре. Нормализация может
    обращаться к родительским папкам, и без доступа к ним вызов
    завершится отказом: тогда для имени с «~» скачивание — 403, это
    безопаснее, чем отдать файл по непроверенному короткому имени.

    Args:
        path: Путь к файлу

    Returns:
        Имя файла в директории

    Raises:
        OSError: Если файл не открылся или путь не получен

    """
    kernel32 = _kernel32()
    handle = kernel32.CreateFileW(
        str(path),
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _OPEN_FLAGS,
        None,
    )
    if handle in {None, _INVALID_HANDLE_VALUE}:
        function = "CreateFileW"
        raise _winapi_error(function, path, ctypes.get_last_error())
    try:
        buffer = ctypes.create_unicode_buffer(_MAX_FINAL_PATH)
        length = kernel32.GetFinalPathNameByHandleW(
            handle, buffer, len(buffer), _FILE_NAME_NORMALIZED
        )
        if not length or length >= len(buffer):
            function = "GetFinalPathNameByHandleW"
            raise _winapi_error(function, path, ctypes.get_last_error())
    finally:
        kernel32.CloseHandle(handle)
    return PureWindowsPath(buffer.value).name


def _invalid_filename() -> HTTPException:
    """Вернуть ошибку 400 «Invalid filename» для такого fileId."""
    return HTTPException(status_code=400, detail="Invalid filename")


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
        # «Сетевой путь не найден» (ERROR_BAD_NETPATH, 53) и «сетевое
        # имя не найдено» (ERROR_BAD_NET_NAME, 67) Python на Windows сам
        # превращает в FileNotFoundError, поэтому недоступная сетевая
        # папка — 404, как и отсутствующая. Отдельной проверки
        # winerror здесь нет: на это поведение опирается спецификация
        # (тест test_list_files_network_path_not_found)
        if not_found and isinstance(e, (FileNotFoundError, NotADirectoryError)):
            msg = "Files directory not found"
            raise HTTPException(status_code=404, detail=msg) from e
        if isinstance(e, PermissionError):
            msg = f"Permission denied when reading directory: {e!s}"
            raise HTTPException(status_code=403, detail=msg) from e
        # 500 означает сбой, который надо разбирать по журналу, а не
        # только по detail ответа, который фронтенд не показывает
        logger.warning("Ошибка при чтении директории с файлами: %s", e)
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
        raise _invalid_filename() from e
    except OSError as e:
        if _is_invalid_name_error(e):
            raise _invalid_filename() from e
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

    Для файла в сетевой папке время создания отдаёт файловый сервер:
    это время появления файла на ресурсе (или время, сохранённое при
    копировании, например robocopy), а не время на машине сервиса.
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
            # .txt прямо в директории. На Windows ссылки не показываем
            # и к их цели не обращаемся. Подробнее — в get_file
            target = None if sys.platform == "win32" else Path(entry.path).resolve()
            if (
                target is None
                or target.parent != base_dir()
                or target.suffix.lower() != ".txt"
            ):
                return None, False
        # Для ссылки stat() идёт к цели. На Windows ни к какой цели не
        # обращаемся: is_symlink() не распознаёт junction, а stat() с
        # переходом пошёл бы через неё дальше (в том числе по ссылке на
        # \\host\share). Без перехода junction — директория и
        # отсеивается ниже, а для обычного файла атрибуты те же, что
        # получены при перечислении
        file_stat = entry.stat(follow_symlinks=sys.platform != "win32")
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
        # обращения к ФС, и прочие элементы не держатся в памяти. Не
        # показываем и файлы, которые get_file не отдаст из-за имени,
        # как nul.txt на Windows 11
        with _directory_errors(not_found=True), os.scandir(files_path) as it:
            entries = [
                entry
                for entry in it
                if Path(entry.name).suffix.lower() == ".txt"
                and not _is_invalid_windows_name(entry.name)
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
        # С трассировкой: непредвиденную ошибку иначе не разобрать
        logger.exception("Непредвиденная ошибка при чтении директории")
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


# Разрешённый путь к директории с файлами (не на Windows, см.
# get_file). Ключ — только путь из настроек, зашитый в сборку, поэтому
# кэш не даёт способа подменить директорию. Сюда попадает только
# результат строгого resolve()
_real_path_cache: dict[Path, Path] = {}


def _resolve_files_directory(files_directory: Path, *, refresh: bool = False) -> Path:
    """Разрешить путь к директории с файлами и обновить кэш.

    Строгий resolve() либо разрешает путь целиком, либо падает, и
    тогда кэш не заполняется, а прежний путь из него убирается.

    Args:
        files_directory: Директория из настроек
        refresh: Повторная проверка закэшированного пути. Недоступная
            директория тогда — сбой, а не повод сверяться с
            неразрешённым путём

    Raises:
        HTTPException: 500 при сбое самой директории, без пути в
            detail: это ошибка сервера, а не клиента

    """
    try:
        try:
            real_path = files_directory.resolve(strict=True)
        except OSError:
            _real_path_cache.pop(files_directory, None)
            if refresh:
                raise
            # Папка недоступна: нестрогий resolve(), как и раньше, но
            # без кэша — он может вернуть неразрешённый путь
            return files_directory.resolve()
    except (OSError, RuntimeError, ValueError) as e:
        _real_path_cache.pop(files_directory, None)
        logger.warning("Не удалось разрешить путь к директории с файлами: %s", e)
        msg = "OS error when reading file"
        raise HTTPException(status_code=500, detail=msg) from e
    _real_path_cache[files_directory] = real_path
    return real_path


def _resolve_in_directory(candidate: Path, files_directory: Path, file_id: str) -> Path:
    """Разрешить путь к файлу и проверить, что он прямо в директории.

    Используется не на Windows: там символическим ссылкам сервис не
    следует (см. get_file).

    Args:
        candidate: Путь к файлу в директории из настроек
        files_directory: Директория из настроек
        file_id: Запрошенный идентификатор файла

    Returns:
        Разрешённый путь к файлу

    Raises:
        HTTPException: 400, если путь ведёт не прямо в директорию, и
            ошибки _file_errors и _resolve_files_directory

    """
    # Директория не меняется, поэтому её путь разрешается один раз:
    # это экономит запросы к ФС на каждом скачивании
    cached_dir = _real_path_cache.get(files_directory)
    base_dir = (
        cached_dir
        if cached_dir is not None
        else _resolve_files_directory(files_directory)
    )

    with _file_errors(file_id):
        file_path = candidate.resolve()

    # Отдаём только файлы непосредственно из директории, как и в
    # списке: символическая ссылка не должна уводить в поддиректорию
    # или за пределы директории. Если путь директории взят из кэша,
    # перед отказом сверяемся с заново разрешённым: закэшированный
    # мог устареть (например, сама директория — ссылка, которую
    # перенаправили)
    if file_path.parent != base_dir and (
        cached_dir is None
        or file_path.parent != _resolve_files_directory(files_directory, refresh=True)
    ):
        raise _invalid_filename()
    return file_path


@router.get("/{fileId}")
def get_file(
    file_id: Annotated[str, PathParam(alias="fileId")],
) -> FileResponse:
    """Скачать определенный файл из настроенной директории.

    Обращения к файловой системе ограничены настроенной директорией:
    fileId проверяется как имя до любого обращения к ней, а на Windows
    символическим ссылкам сервис не следует. Поэтому ни данные
    запроса, ни ссылки в директории не заставят сервис подключиться к
    чужому хосту.

    Args:
        file_id: ID файла для загрузки (имя файла)

    Returns:
        Ответ с запрашиваемым файлом

    Raises:
        HTTPException: 400 «Invalid filename», если fileId — не имя
            файла (путь, UNC-путь, имя устройства Windows и т.п.),
            ОС отвергает имя, файл — символическая ссылка на Windows
            или лежит не прямо в директории; 400 «Path is not a file»
            для директории; 403 «File is not available for download»,
            если файл не соответствует критериям availableFiles;
            403/500 при ошибке ОС (текст ошибки — только в лог);
            404 «File not found», если файла нет или сетевая папка
            недоступна; 500, если не удалось разрешить путь самой
            директории (кроме Windows)

    """
    files_directory = Path(get_settings().files_directory)

    # Безопасность: fileId должен быть просто именем, и проверяется это
    # до любого обращения к ФС. Абсолютный путь (на Windows и
    # UNC-путь \\host\share) заменил бы директорию при склейке, и
    # resolve() пошёл бы на чужой хост, отдав ему NTLM-хеш учётной
    # записи сервиса
    candidate = files_directory / file_id
    if (
        # Склейка нормализует путь: на Windows ".\\x.txt", "x.txt\\" и
        # полный путь к файлу в директории — тот же файл, но не имя
        candidate.name != file_id
        or "\x00" in file_id
        or file_id in {".", ".."}
        or _is_invalid_windows_name(file_id)
    ):
        raise _invalid_filename()

    if sys.platform == "win32":
        # На Windows символическим ссылкам не следуем: ОС открыла бы
        # цель, и ссылка на \\host\share отдала бы хосту NTLM-хеш
        # учётной записи сервиса. lstat() читает саму ссылку, не
        # обращаясь к цели. fileId — уже проверенное имя, и раз это не
        # ссылка, файл лежит прямо в директории: resolve() и сверка с
        # директорией не нужны, а атрибуты для проверок и ответа уже
        # получены
        with _file_errors(file_id):
            file_stat = candidate.lstat()
        if S_ISLNK(file_stat.st_mode):
            raise _invalid_filename()
        # Windows находит файл и без учёта регистра, и по короткому
        # имени 8.3: report.txtx отдался бы как REPORT~1.TXT в обход
        # проверки расширения. Проверки и имя в ответе — по настоящему
        # имени, как в списке. Второе и последнее обращение к сетевой
        # папке до чтения файла
        with _file_errors(file_id):
            real_name = _real_file_name(candidate)
        if _is_invalid_windows_name(real_name):
            raise _invalid_filename()
        file_path = candidate.with_name(real_name)
    else:
        file_path = _resolve_in_directory(candidate, files_directory, file_id)
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
