"""
Комплексные тесты для File Picker API.
"""

import ctypes
import errno
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient


def reload_app(files_directory=None):
    """Вспомогательная функция для перезагрузки главного модуля.

    С обновленными переменными окружения.
    """
    # Перезагружаем настройки и обработчики, чтобы они получили
    # новые переменные окружения
    if "app.settings" in sys.modules:
        # Очищаем кэш lru_cache перед перезагрузкой
        from app.settings import get_settings

        get_settings.cache_clear()
        importlib.reload(sys.modules["app.settings"])
    if "app.handlers.files" in sys.modules:
        importlib.reload(sys.modules["app.handlers.files"])
    if "app" in sys.modules:
        importlib.reload(sys.modules["app"])
    import app.settings as settings_module

    if files_directory is not None:
        settings_module.Settings.files_directory = files_directory
    from app import app as test_app

    return test_app


def assert_sorted_by_created_at(files: list[dict]) -> None:
    """Проверить, что файлы отсортированы по дате создания
    (убывание).

    Сравниваем datetime, а не строки: "…00Z" как строка больше более
    позднего "…00.5Z".
    """
    created_times = [datetime.fromisoformat(item["createdAt"]) for item in files]
    assert created_times == sorted(created_times, reverse=True)


# Проверки, которые имеют смысл только на Windows: на Linux обратный
# слэш — обычный символ имени, а время создания файла недоступно
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Только Windows")
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Только Linux"
)
# На Windows сервис символическим ссылкам не следует (см.
# TestWindowsSymlinks), поэтому сценарии с переходом по ссылке — для
# остальных ОС
follows_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="На Windows ссылкам не следуем"
)


def fail_for(method, name, error):
    """Вернуть патч метода Path, который падает только для файла name.

    name — имя или кортеж имён. Падать на всех путях нельзя: ошибка
    случилась бы уже на базовой директории (на Python 3.11 resolve()
    сам вызывает stat()).
    """
    original = getattr(Path, method)
    names = {name} if isinstance(name, str) else set(name)

    def fake(self, *args, **kwargs):
        if self.name in names:
            raise error
        return original(self, *args, **kwargs)

    return mock.patch.object(Path, method, autospec=True, side_effect=fake)


@contextmanager
def fail_stat_for(name, error):
    """Подменить Path.stat и Path.lstat: падают только для name.

    lstat() тоже: на Windows атрибуты скачиваемого файла сервис читает
    через него (на Python 3.12+ lstat() не вызывает stat()).
    """
    with fail_for("stat", name, error), fail_for("lstat", name, error):
        yield


@contextmanager
def as_windows():
    """Подменить платформу на Windows только для app.handlers.files.

    sys.platform подменяется не глобально: иначе по Windows-веткам
    пошли бы и библиотеки, обслуживающие запрос (anyio, starlette).
    Вызывать после reload_app: подменяется атрибут модуля.

    Поиск настоящего имени файла (WinAPI) заменяется заглушкой: имя
    совпадает с запрошенным. Сам поиск проверяет
    TestWindowsRealFileName.
    """
    from app.handlers import files

    with (
        mock.patch.object(files, "sys", SimpleNamespace(platform="win32")),
        mock.patch.object(files, "_real_file_name", side_effect=lambda p: p.name),
    ):
        yield


# На Windows путь к скачиваемому файлу не разрешается через resolve(), и
# кэша пути директории нет (см. get_file): эти сценарии — для остальных
# ОС
resolves_paths = pytest.mark.skipif(
    sys.platform == "win32", reason="На Windows путь не разрешается"
)


class FailingEntry:
    """Обёртка DirEntry, у которой stat() падает с заданной ошибкой."""

    def __init__(self, entry, error):
        self._entry = entry
        self._error = error

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def stat(self, **_kwargs):
        raise self._error


def wrap_entry_for(name, wrap):
    """Вернуть патч os.scandir: элемент name обёрнут функцией wrap.

    Методы DirEntry нельзя подменить напрямую, поэтому подменяем сами
    элементы перечисления.
    """
    original = os.scandir

    @contextmanager
    def fake(path):
        with original(path) as it:
            yield [wrap(e) if e.name == name else e for e in it]

    return mock.patch("os.scandir", side_effect=fake)


def fail_entry_stat_for(name, error):
    """Вернуть патч os.scandir: DirEntry.stat() падает для name."""
    return wrap_entry_for(name, lambda entry: FailingEntry(entry, error))


class NoFollowEntry:
    """Обёртка DirEntry: stat() с переходом по ссылке — ошибка теста.

    AssertionError в обработчике даёт ответ 500.
    """

    def __init__(self, entry):
        self._entry = entry

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def stat(self, *, follow_symlinks=True):
        if follow_symlinks:
            msg = "DirEntry.stat() must not follow links"
            raise AssertionError(msg)
        return self._entry.stat(follow_symlinks=False)


class TimedEntry:
    """Обёртка DirEntry, у которой stat() отдаёт заданное время."""

    def __init__(self, entry, timestamp):
        self._entry = entry
        self._timestamp = timestamp

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def stat(self, **_kwargs):
        real = self._entry.stat()
        return SimpleNamespace(
            st_mode=real.st_mode,
            st_size=real.st_size,
            st_birthtime=self._timestamp,
            st_ctime=self._timestamp,
            st_mtime=self._timestamp,
        )


def entry_time_for(name, timestamp):
    """Вернуть патч os.scandir: у файла name время равно timestamp."""
    return wrap_entry_for(name, lambda entry: TimedEntry(entry, timestamp))


def symlink_or_skip(link, target, *, target_is_directory=None):
    """Создать символическую ссылку или пропустить тест.

    target_is_directory=None — определить по цели; для цели на
    сетевом ресурсе его нужно передать явно, чтобы не обращаться к ней.
    """
    try:
        # На Windows ссылка на директорию должна создаваться как ссылка
        # на директорию, иначе через неё нельзя зайти внутрь
        # Относительная цель считается от папки ссылки, а не от текущей
        if target_is_directory is None:
            target_is_directory = (Path(link).parent / target).is_dir()
        Path(link).symlink_to(target, target_is_directory=target_is_directory)
    except OSError:
        # В CI (REQUIRE_SYMLINKS) ссылки создать можно всегда: пропуск
        # там скрыл бы непроверенные сценарии
        if os.environ.get("REQUIRE_SYMLINKS"):
            raise
        # На Windows без прав администратора или режима
        # разработчика символические ссылки создать нельзя
        pytest.skip("Символические ссылки недоступны")


@pytest.fixture
def test_files_dir():
    """Создать временную директорию с тестовыми файлами."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Создаем тестовые файлы
        test_file_1 = Path(tmpdir) / "test1.txt"
        test_file_1.write_text("Test content 1")

        test_file_2 = Path(tmpdir) / "test2.txt"
        test_file_2.write_text("Test content 2 with more data")

        test_file_3 = Path(tmpdir) / "document.pdf"
        test_file_3.write_bytes(b"PDF content here")

        # Создаем поддиректорию
        subdir = Path(tmpdir) / "subdir"
        subdir.mkdir()

        yield tmpdir


@pytest.fixture
def subdir_link(test_files_dir):
    """Создать link.txt — ссылку на файл в поддиректории."""
    inner = Path(test_files_dir) / "subdir" / "inner.txt"
    inner.write_text("inner")
    symlink_or_skip(Path(test_files_dir) / "link.txt", inner)
    return test_files_dir


@pytest.fixture
def client(test_files_dir, monkeypatch):
    """Создать тестовый клиент с временной директорией файлов."""
    # Устанавливаем переменную окружения перед импортом
    monkeypatch.setenv("CORS_ORIGINS", "*")

    # Принудительно перезагружаем главный модуль для применения
    # новых переменных окружения
    test_app = reload_app(files_directory=test_files_dir)
    return TestClient(test_app)


class TestListFilesEndpoint:
    """Тесты для конечной точки списка файлов."""

    def test_list_files_success(self, client):
        """Проверить, что список файлов возвращает корректную
        информацию о файлах.
        """
        response = client.get("/files")
        assert response.status_code == 200
        data = response.json()

        # Должен вернуть объект с двумя списками
        assert isinstance(data, dict)
        assert "availableFiles" in data
        assert "unavailableFiles" in data
        assert isinstance(data["availableFiles"], list)
        assert isinstance(data["unavailableFiles"], list)

        # Все файлы из обоих списков
        # (директории и не-.txt файлы игнорируются)
        all_files = data["availableFiles"] + data["unavailableFiles"]
        # 2 .txt файла (директория subdir и
        # document.pdf игнорируются)
        assert len(all_files) == 2

        # Только .txt файлы в availableFiles
        assert len(data["availableFiles"]) == 2  # test1.txt и test2.txt
        # Больших .txt файлов нет, поэтому unavailableFiles пустой
        assert len(data["unavailableFiles"]) == 0

        # Проверяем, что файлы отсортированы по дате создания
        # (новые первыми)
        assert_sorted_by_created_at(data["availableFiles"])
        assert_sorted_by_created_at(data["unavailableFiles"])

        # Проверяем структуру файла
        for item in all_files:
            assert "id" in item
            assert "name" in item
            assert "size" in item
            assert "createdAt" in item

    def test_list_files_contains_correct_metadata(self, client):
        """Проверить, что метаданные файла корректны."""
        response = client.get("/files")
        assert response.status_code == 200
        data = response.json()

        # Объединяем все файлы из обоих списков
        all_files = data["availableFiles"] + data["unavailableFiles"]

        # Находим test1.txt (должен быть в availableFiles)
        test1 = next((item for item in all_files if item["name"] == "test1.txt"), None)
        assert test1 is not None
        assert test1["size"] == 14  # длина "Test content 1"
        assert test1["id"] == "test1.txt"
        assert "createdAt" in test1

        # document.pdf не должен присутствовать (не .txt файл)
        pdf_file = next(
            (item for item in all_files if item["name"] == "document.pdf"), None
        )
        assert pdf_file is None

        # Директории не должны присутствовать в ответе
        subdir = next((item for item in all_files if item["name"] == "subdir"), None)
        assert subdir is None

    def test_list_files_nonexistent_directory(self):
        """Проверить вывод списка файлов, когда директория
        не существует.
        """
        test_app = reload_app(files_directory="/nonexistent/path")
        client = TestClient(test_app)

        response = client.get("/files")
        assert response.status_code == 404
        assert "Files directory not found" in response.json()["detail"]

    def test_list_files_when_path_is_file(self, test_files_dir):
        """Проверить вывод списка файлов, когда FILES_DIRECTORY
        указывает на файл.
        """
        file_path = Path(test_files_dir) / "test1.txt"
        test_app = reload_app(files_directory=str(file_path))
        client = TestClient(test_app)

        response = client.get("/files")
        assert response.status_code == 400
        assert "Files path is not a directory" in response.json()["detail"]

    def test_list_files_filters_by_size_and_format(self):
        """Проверить, что файлы фильтруются по размеру и формату."""
        from app.handlers.files import MAX_AVAILABLE_FILE_SIZE

        with tempfile.TemporaryDirectory() as tmpdir:
            # Создаем .txt файл меньше 10 МБ
            # (должен быть в availableFiles)
            small_txt = Path(tmpdir) / "small.txt"
            small_txt.write_bytes(b"x" * 1024)

            # Создаем .pdf файл меньше 10 МБ
            # (не .txt - в unavailableFiles)
            small_pdf = Path(tmpdir) / "small.pdf"
            small_pdf.write_bytes(b"x" * 1024)

            # Создаем .txt файл ровно 10 МБ
            # (большой - в unavailableFiles)
            exact_10mb = Path(tmpdir) / "exact_10mb.txt"
            exact_10mb.write_bytes(b"x" * MAX_AVAILABLE_FILE_SIZE)

            # Создаем .txt файл больше 10 МБ
            # (большой - в unavailableFiles)
            large_txt = Path(tmpdir) / "large.txt"
            large_txt.write_bytes(b"x" * int(MAX_AVAILABLE_FILE_SIZE * 1.5))

            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            response = client.get("/files")
            assert response.status_code == 200
            data = response.json()

            # Только маленький .txt файл в availableFiles
            available_names = [f["name"] for f in data["availableFiles"]]
            assert "small.txt" in available_names

            # Все большие .txt файлы в unavailableFiles
            not_available_names = [f["name"] for f in data["unavailableFiles"]]
            # small.pdf не включается (не .txt файл)
            assert "small.pdf" not in not_available_names
            assert "exact_10mb.txt" in not_available_names  # большой
            assert "large.txt" in not_available_names  # большой

            # Проверяем количество (small.pdf игнорируется)
            assert len(data["availableFiles"]) == 1
            assert len(data["unavailableFiles"]) == 2

    def test_list_files_uppercase_extension(self, test_files_dir):
        """Проверить: REPORT.TXT есть в списке и скачивается."""
        (Path(test_files_dir) / "REPORT.TXT").write_text("upper")
        client = TestClient(reload_app(files_directory=test_files_dir))

        names = [f["name"] for f in client.get("/files").json()["availableFiles"]]
        assert "REPORT.TXT" in names
        response = client.get("/files/REPORT.TXT")
        assert response.status_code == 200
        assert response.content == b"upper"

    def test_list_files_sorted_newest_first(self, tmp_path):
        """Проверить порядок «новые первыми» в обоих списках.

        Время подменяем: время создания на Windows не выставить через
        os.utime. Метки отличаются долями секунды — на них ломалось бы
        сравнение строк.
        """
        from app.handlers import files

        # Файлы различаются размером, по нему и выбираем время
        sizes = {"old.txt": 1, "new.txt": 2, "old_big.txt": 101, "new_big.txt": 102}
        base = datetime(2024, 1, 1, 10, tzinfo=UTC)
        created = {
            1: base,
            2: base + timedelta(milliseconds=500),
            101: base,
            102: base + timedelta(milliseconds=500),
        }
        for name, size in sizes.items():
            (tmp_path / name).write_bytes(b"x" * size)
        client = TestClient(reload_app(files_directory=str(tmp_path)))

        with (
            mock.patch.object(files, "MAX_AVAILABLE_FILE_SIZE", 100),
            mock.patch.object(
                files, "file_created_at", side_effect=lambda st: created[st.st_size]
            ),
        ):
            data = client.get("/files").json()
        assert [f["name"] for f in data["availableFiles"]] == ["new.txt", "old.txt"]
        assert [f["name"] for f in data["unavailableFiles"]] == [
            "new_big.txt",
            "old_big.txt",
        ]
        assert_sorted_by_created_at(data["availableFiles"])

    def test_list_files_created_at_is_utc(self, client):
        """Проверить, что createdAt передаётся в UTC."""
        for item in client.get("/files").json()["availableFiles"]:
            created_at = datetime.fromisoformat(item["createdAt"])
            assert created_at.utcoffset() == timedelta(0)

    @windows_only
    def test_list_files_created_at_is_creation_time_on_windows(
        self, client, test_files_dir
    ):
        """Проверить: на Windows createdAt — время создания, а не
        изменения.
        """
        old = datetime(2020, 1, 1, tzinfo=UTC).timestamp()
        os.utime(Path(test_files_dir) / "test1.txt", (old, old))

        items = {f["name"]: f for f in client.get("/files").json()["availableFiles"]}
        created_at = datetime.fromisoformat(items["test1.txt"]["createdAt"])
        assert created_at > datetime.now(UTC) - timedelta(hours=1)

    @linux_only
    def test_list_files_created_at_is_mtime_on_linux(self, client, test_files_dir):
        """Проверить: на Linux createdAt — время изменения файла."""
        old = datetime(2020, 1, 1, tzinfo=UTC)
        os.utime(Path(test_files_dir) / "test1.txt", (old.timestamp(),) * 2)

        items = {f["name"]: f for f in client.get("/files").json()["availableFiles"]}
        assert datetime.fromisoformat(items["test1.txt"]["createdAt"]) == old

    def test_list_files_txt_only_in_available(self, client):
        """Проверить, что только .txt файлы в availableFiles."""
        response = client.get("/files")
        assert response.status_code == 200
        data = response.json()

        # Только .txt файлы в availableFiles
        assert len(data["availableFiles"]) == 2  # test1.txt и test2.txt
        for file in data["availableFiles"]:
            assert file["name"].lower().endswith(".txt")

        # document.pdf игнорируется (не .txt файл),
        # subdir игнорируется (директория)
        assert len(data["unavailableFiles"]) == 0
        not_available_names = [f["name"] for f in data["unavailableFiles"]]
        # document.pdf не включается (не .txt файл)
        assert "document.pdf" not in not_available_names
        # Директории не включаются в результаты
        assert "subdir" not in not_available_names


class TestDownloadFileEndpoint:
    """Тесты для конечной точки загрузки файла."""

    def test_download_file_success(self, client):
        """Проверить успешную загрузку файла."""
        response = client.get("/files/test1.txt")
        assert response.status_code == 200
        assert response.content == b"Test content 1"
        assert response.headers["content-type"] == "application/octet-stream"
        assert 'attachment; filename="test1.txt"' in response.headers.get(
            "content-disposition", ""
        )

    def test_download_different_file(self, client):
        """Проверить загрузку другого файла."""
        response = client.get("/files/test2.txt")
        assert response.status_code == 200
        assert response.content == b"Test content 2 with more data"

    def test_download_binary_file(self, client):
        """Проверить, что недоступный бинарный файл нельзя загрузить."""
        response = client.get("/files/document.pdf")
        assert response.status_code == 403
        assert "File is not available for download" in response.json()["detail"]

    def test_download_nonexistent_file(self, client):
        """Проверить загрузку несуществующего файла."""
        response = client.get("/files/nonexistent.txt")
        assert response.status_code == 404
        assert "File not found" in response.json()["detail"]

    def test_download_directory(self, client):
        """Проверить, что директории нельзя загрузить."""
        response = client.get("/files/subdir")
        assert response.status_code == 400
        assert "Path is not a file" in response.json()["detail"]

    def test_download_unavailable_large_file(self):
        """Проверить, что большие файлы нельзя загрузить."""
        from app.handlers.files import MAX_AVAILABLE_FILE_SIZE

        with tempfile.TemporaryDirectory() as tmpdir:
            # Создаем .txt файл ровно 10 МБ (недоступен)
            large_txt = Path(tmpdir) / "large.txt"
            large_txt.write_bytes(b"x" * MAX_AVAILABLE_FILE_SIZE)

            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            response = client.get("/files/large.txt")
            assert response.status_code == 403
            assert "File is not available for download" in response.json()["detail"]

    def test_download_unavailable_non_txt_file(self):
        """Проверить, что не .txt файлы нельзя загрузить."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Создаем маленький .pdf файл (недоступен)
            small_pdf = Path(tmpdir) / "small.pdf"
            small_pdf.write_bytes(b"PDF content")

            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            response = client.get("/files/small.pdf")
            assert response.status_code == 403
            assert "File is not available for download" in response.json()["detail"]

    def test_download_available_file_just_under_limit(self):
        """Проверить, что файлы чуть меньше лимита можно загрузить."""
        from app.handlers.files import MAX_AVAILABLE_FILE_SIZE

        with tempfile.TemporaryDirectory() as tmpdir:
            # Создаем .txt файл чуть меньше 10 МБ (доступен)
            almost_limit = Path(tmpdir) / "almost_limit.txt"
            almost_limit.write_bytes(b"x" * (MAX_AVAILABLE_FILE_SIZE - 1))

            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            response = client.get("/files/almost_limit.txt")
            assert response.status_code == 200
            assert len(response.content) == MAX_AVAILABLE_FILE_SIZE - 1


class TestSecurityDirectoryTraversal:
    """Тесты для безопасности обхода директорий."""

    @pytest.mark.usefixtures("subdir_link")
    def test_symlink_into_subdirectory(self, test_files_dir):
        """Проверить: файл из поддиректории не виден и не скачивается.

        На Windows так же разрешается fileId с обратным слэшем
        (``subdir%5Cinner.txt``): путь остаётся внутри директории,
        но указывает не на её непосредственный файл.
        """
        client = TestClient(reload_app(files_directory=test_files_dir))

        response = client.get("/files/link.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

        names = [f["name"] for f in client.get("/files").json()["availableFiles"]]
        assert "link.txt" not in names

    @follows_symlinks
    @pytest.mark.parametrize(
        ("target", "listed"),
        [("test1.txt", True), ("document.pdf", False)],
    )
    def test_symlink_listed_only_if_downloadable(self, test_files_dir, target, listed):
        """Проверить: ссылка в списке, только если её можно скачать."""
        symlink_or_skip(Path(test_files_dir) / "link.txt", target)
        client = TestClient(reload_app(files_directory=test_files_dir))

        names = [f["name"] for f in client.get("/files").json()["availableFiles"]]
        assert ("link.txt" in names) is listed
        download = client.get("/files/link.txt")
        assert (download.status_code == 200) is listed
        if listed:
            assert download.content == b"Test content 1"

    def test_symlink_outside_directory(self, test_files_dir, tmp_path):
        """Проверить: ссылка за пределы директории не видна и не
        скачивается.
        """
        outside = tmp_path / "outside.txt"
        outside.write_text("secret")
        symlink_or_skip(Path(test_files_dir) / "link.txt", outside)
        client = TestClient(reload_app(files_directory=test_files_dir))

        response = client.get("/files/link.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

        data = client.get("/files").json()
        names = [f["name"] for f in data["availableFiles"] + data["unavailableFiles"]]
        assert "link.txt" not in names

    def test_symlink_loop_skipped_in_listing(self, test_files_dir):
        """Проверить: петля ссылок на Python 3.11 не роняет список."""
        symlink_or_skip(Path(test_files_dir) / "loop.txt", "test1.txt")
        client = TestClient(reload_app(files_directory=test_files_dir))

        error = RuntimeError("Symlink loop from 'loop.txt'")
        with fail_for("resolve", "loop.txt", error):
            response = client.get("/files")
        assert response.status_code == 200
        names = [f["name"] for f in response.json()["availableFiles"]]
        assert "loop.txt" not in names

    def test_directory_traversal_with_dotdot(self, client):
        """Проверить, что обход директорий с .. предотвращен."""
        response = client.get("/files/../main.py")
        # Не должно быть возможности получить доступ к файлам
        # вне настроенной директории
        assert response.status_code == 404

    def test_directory_traversal_with_absolute_path(self, client):
        """Проверить, что абсолютные пути обрабатываются корректно."""
        response = client.get("/files//etc/passwd")
        assert response.status_code in [400, 404]

    def test_directory_traversal_url_encoded(self, client):
        """Проверить обход директорий через URL-кодирование."""
        # %2E%2E - это URL-кодированный ..
        # FastAPI/Starlette декодирует это до обработчика
        # Логика безопасности проверяет, что путь остается
        # внутри директории
        response = client.get("/files/%2E%2E%2Fmain.py")
        # Не должно быть доступа к файлам вне директории
        assert response.status_code == 404

    def test_directory_traversal_complex_path(self, client):
        """Проверить сложные попытки обхода директорий."""
        response = client.get("/files/subdir/../../main.py")
        # Не должно быть возможности получить доступ к файлам
        # вне директории
        assert response.status_code == 404

    def test_valid_filename_works(self, client):
        """Проверить, что валидные имена файлов все еще работают
        после проверок безопасности.
        """
        response = client.get("/files/test1.txt")
        assert response.status_code == 200


class TestWindowsSymlinks:
    """На Windows сервис символическим ссылкам не следует.

    Платформа подменяется, поэтому тесты идут и на Linux; на Windows
    (CI) они проверяют и то, что ОС не переходит по ссылке.
    """

    # Хост в зоне .invalid не существует: даже если сервис пойдёт по
    # ссылке, подключения никуда не будет
    UNC_TARGET = r"\\attacker.invalid\s\x.txt"

    @pytest.fixture(params=["test1.txt", UNC_TARGET])
    def link_client(self, request, test_files_dir):
        """Клиент на Windows и link.txt — ссылка на файл или UNC."""
        symlink_or_skip(
            Path(test_files_dir) / "link.txt", request.param, target_is_directory=False
        )
        client = TestClient(reload_app(files_directory=test_files_dir))

        # resolve(), stat() или DirEntry.stat() ссылки с переходом — уже
        # переход по ней: AssertionError даст 500.
        # stat(follow_symlinks=False) — это lstat() (так он устроен на
        # Python 3.11), ссылку он не раскрывает
        error = AssertionError("symlink must not be followed")
        original_stat = Path.stat

        def stat_without_following(self, *args, **kwargs):
            if self.name == "link.txt" and kwargs.get("follow_symlinks", True):
                raise error
            return original_stat(self, *args, **kwargs)

        with (
            as_windows(),
            fail_for("resolve", "link.txt", error),
            mock.patch.object(
                Path, "stat", autospec=True, side_effect=stat_without_following
            ),
            wrap_entry_for("link.txt", NoFollowEntry),
        ):
            yield client

    def test_symlink_not_listed(self, link_client):
        """Проверить: ссылки нет в списке, остальные файлы на месте."""
        response = link_client.get("/files")
        assert response.status_code == 200
        data = response.json()
        names = [f["name"] for f in data["availableFiles"] + data["unavailableFiles"]]
        assert sorted(names) == ["test1.txt", "test2.txt"]

    def test_symlink_download_rejected(self, link_client):
        """Проверить: скачивание ссылки — 400 без перехода по ней."""
        response = link_client.get("/files/link.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    def test_regular_file_still_downloaded(self, link_client):
        """Проверить: обычный файл рядом со ссылкой скачивается."""
        response = link_client.get("/files/test1.txt")
        assert response.status_code == 200
        assert response.content == b"Test content 1"

    def test_listing_reads_attributes_without_following(self, client):
        """Проверить: на Windows атрибуты файлов списка читаются без
        перехода по ссылкам (stat(follow_symlinks=False)).

        Иначе junction, которую is_symlink() не распознаёт, увела бы
        stat() к своей цели.
        """
        with as_windows(), wrap_entry_for("test1.txt", NoFollowEntry):
            response = client.get("/files")
        assert response.status_code == 200
        files = {f["name"]: f for f in response.json()["availableFiles"]}
        assert files["test1.txt"]["size"] == 14

    @windows_only
    def test_junction_not_followed(self, test_files_dir, tmp_path):
        """Проверить: junction с именем .txt не показывается, не
        скачивается, и сервис не идёт через неё к цели.
        """
        import _winapi

        target = tmp_path / "target"
        target.mkdir()
        junction = Path(test_files_dir) / "redirect.txt"
        _winapi.CreateJunction(str(target), str(junction))
        client = TestClient(reload_app(files_directory=test_files_dir))

        with wrap_entry_for("redirect.txt", NoFollowEntry):
            response = client.get("/files")
        assert response.status_code == 200
        data = response.json()
        names = [f["name"] for f in data["availableFiles"] + data["unavailableFiles"]]
        assert sorted(names) == ["test1.txt", "test2.txt"]
        assert client.get("/files/redirect.txt").status_code == 400


def short_name_or_skip(path):
    """Вернуть короткое имя 8.3 файла или пропустить тест.

    На томе с отключёнными короткими именами (fsutil 8dot3name)
    короткое имя совпадает с длинным, и проверять нечего.
    """
    get_short = ctypes.WinDLL("kernel32").GetShortPathNameW
    buffer = ctypes.create_unicode_buffer(1024)
    if not get_short(str(path), buffer, len(buffer)):
        pytest.skip("Не удалось получить короткое имя")
    short = Path(buffer.value).name
    if short == path.name:
        pytest.skip("Короткие имена 8.3 на этом томе отключены")
    return short


class TestWindowsRealFileName:
    """На Windows проверки и имя в ответе — по настоящему имени файла.

    Windows находит файл без учёта регистра и по короткому имени 8.3,
    поэтому сервис узнаёт настоящее имя через FindFirstFileExW.
    """

    @pytest.fixture
    def real_file_name(self, test_files_dir):
        reload_app(files_directory=test_files_dir)
        from app.handlers.files import _real_file_name

        return _real_file_name

    def download_as(self, test_files_dir, requested, real_name):
        """Скачать requested на «Windows» с настоящим именем."""
        client = TestClient(reload_app(files_directory=test_files_dir))
        from app.handlers import files

        with (
            as_windows(),
            mock.patch.object(files, "_real_file_name", return_value=real_name),
        ):
            return client.get("/files/" + quote(requested))

    def test_short_name_checked_by_real_extension(self, test_files_dir):
        """Проверить: REPORT~1.TXT для report.txtx — 403."""
        (Path(test_files_dir) / "REPORT~1.TXT").write_text("not a txt")
        response = self.download_as(test_files_dir, "REPORT~1.TXT", "report.txtx")
        assert response.status_code == 403
        assert response.json()["detail"] == "File is not available for download"

    def test_response_uses_real_name(self, test_files_dir):
        """Проверить: в Content-Disposition настоящее имя."""
        # На Linux это два файла, на Windows — один и тот же
        for name in ("payment.txt", "Payment.txt"):
            (Path(test_files_dir) / name).write_text("content")
        response = self.download_as(test_files_dir, "payment.txt", "Payment.txt")
        assert response.status_code == 200
        assert response.content == b"content"
        assert 'filename="Payment.txt"' in response.headers["content-disposition"]

    def test_real_name_is_device_name(self, test_files_dir):
        """Проверить: короткое имя файла nul.txt — 400."""
        (Path(test_files_dir) / "NUL~1.TXT").write_text("device")
        response = self.download_as(test_files_dir, "NUL~1.TXT", "nul.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    def test_file_removed_before_name_lookup(self, test_files_dir):
        """Проверить: файл пропал до поиска имени — 404."""
        client = TestClient(reload_app(files_directory=test_files_dir))
        from app.handlers import files

        error = FileNotFoundError(errno.ENOENT, "No such file")
        with (
            as_windows(),
            mock.patch.object(files, "_real_file_name", side_effect=error),
        ):
            response = client.get("/files/test1.txt")
        assert response.status_code == 404
        assert response.json()["detail"] == "File not found"

    def fake_kernel32(self, name=None):
        """Вернуть поддельный kernel32, который находит name."""
        kernel32 = mock.MagicMock()

        def find_first(_path, _level, data, *_args):
            if name is None:
                return ctypes.c_void_p(-1).value
            data._obj.name = name  # noqa: SLF001 - объект за ctypes.byref
            return 1234

        kernel32.FindFirstFileExW.side_effect = find_first
        return kernel32

    def test_real_file_name_found(self, real_file_name, test_files_dir):
        """Проверить вызов WinAPI: имя из ответа, поиск закрыт."""
        kernel32 = self.fake_kernel32("Payment.txt")
        with mock.patch.object(ctypes, "WinDLL", create=True, return_value=kernel32):
            name = real_file_name(Path(test_files_dir) / "payment.txt")
        assert name == "Payment.txt"
        args = kernel32.FindFirstFileExW.call_args.args
        assert args[0] == str(Path(test_files_dir) / "payment.txt")
        # FindExInfoBasic: короткое имя не запрашивается
        assert args[1] == 1
        kernel32.FindClose.assert_called_once_with(1234)

    def test_real_file_name_not_found(self, real_file_name, test_files_dir):
        """Проверить: ошибка поиска — OSError с путём без FindClose."""
        kernel32 = self.fake_kernel32()
        path = Path(test_files_dir) / "missing.txt"
        with (
            mock.patch.object(ctypes, "WinDLL", create=True, return_value=kernel32),
            mock.patch.object(ctypes, "get_last_error", create=True, return_value=2),
            pytest.raises(OSError, match="FindFirstFileExW failed") as exc_info,
        ):
            real_file_name(path)
        assert exc_info.value.filename == str(path)
        kernel32.FindClose.assert_not_called()

    @windows_only
    def test_real_file_name_on_windows(self, real_file_name, test_files_dir):
        """Проверить поиск на Windows: регистр и отсутствие файла."""
        (Path(test_files_dir) / "Payment.txt").write_text("content")
        real = real_file_name(Path(test_files_dir) / "PAYMENT.TXT")
        assert real == "Payment.txt"
        with pytest.raises(FileNotFoundError):
            real_file_name(Path(test_files_dir) / "missing.txt")

    @windows_only
    def test_short_name_download_on_windows(self, test_files_dir):
        """Проверить: короткое имя не обходит проверку .txt."""
        other = Path(test_files_dir) / "report.txtx"
        other.write_text("not a txt")
        payment = Path(test_files_dir) / "long payment name.txt"
        payment.write_text("payment")
        client = TestClient(reload_app(files_directory=test_files_dir))

        response = client.get("/files/" + quote(short_name_or_skip(other)))
        assert response.status_code == 403
        response = client.get("/files/" + quote(short_name_or_skip(payment)))
        assert response.status_code == 200
        assert response.content == b"payment"
        assert "long%20payment%20name.txt" in response.headers["content-disposition"]


# Отдельный SMB-ресурс для тестов сетевой папки. В CI его создаёт джоб
# test-windows (build-exe.yml) и передаёт имя и локальную папку
# ресурса; без них тесты пропускаются. Тесты убирают ресурс и создают
# его заново, поэтому нужны права администратора (раннер CI их даёт)
SMB_SHARE_NAME = os.environ.get("SMB_TEST_SHARE")
SMB_SHARE_DIR = os.environ.get("SMB_TEST_SHARE_DIR")
# Значения передаются через переменные окружения, а не подставляются в
# текст команды. Параметры создания — те же, что в build-exe.yml
_CURRENT_USER = "([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
SMB_SHARE_CREATE = (
    "New-SmbShare -Name $env:SHARE_NAME -Path $env:SHARE_DIR"
    f" -FullAccess {_CURRENT_USER} | Out-Null"
)
SMB_SHARE_REMOVE = "Remove-SmbShare -Name $env:SHARE_NAME -Force"
SMB_SHARE_BLOCK = (
    f"Block-SmbShareAccess -Name $env:SHARE_NAME -AccountName {_CURRENT_USER}"
    " -Force | Out-Null"
)
SMB_SHARE_UNBLOCK = (
    f"Unblock-SmbShareAccess -Name $env:SHARE_NAME -AccountName {_CURRENT_USER}"
    " -Force | Out-Null"
)


def smb_share(command):
    """Выполнить команду PowerShell над тестовым SMB-ресурсом."""
    powershell = (
        Path(os.environ["SYSTEMROOT"])
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    subprocess.run(  # noqa: S603 - команда и параметры задаются тестами
        [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
        env={**os.environ, "SHARE_NAME": SMB_SHARE_NAME, "SHARE_DIR": SMB_SHARE_DIR},
        check=True,
        capture_output=True,
        timeout=120,
    )


def wait_for_status(client, url, expected, timeout=60):
    """Повторять запрос, пока не придёт статус expected.

    SMB-клиент Windows несколько секунд кэширует атрибуты файлов и
    директорий и то, что файла нет, поэтому изменение ресурса видно не
    сразу. Возвращает последний ответ и все полученные статусы.
    """
    deadline = time.monotonic() + timeout
    statuses = []
    while True:
        response = client.get(url)
        statuses.append(response.status_code)
        if response.status_code == expected or time.monotonic() > deadline:
            return response, statuses
        time.sleep(1)


@windows_only
@pytest.mark.skipif(
    not (SMB_SHARE_NAME and SMB_SHARE_DIR),
    reason="Нет тестового SMB-ресурса (SMB_TEST_SHARE, SMB_TEST_SHARE_DIR)",
)
class TestSmbShare:
    """Директория на настоящем SMB-ресурсе этого компьютера."""

    @pytest.fixture
    def share_dir(self):
        """Своя подпапка ресурса с test1.txt для каждого теста."""
        local = Path(SMB_SHARE_DIR) / f"t{uuid.uuid4().hex[:12]}"
        local.mkdir()
        (local / "test1.txt").write_text("Test content 1")
        yield local
        shutil.rmtree(local, ignore_errors=True)

    @staticmethod
    def unc(host, local):
        """Вернуть UNC-путь к подпапке ресурса через хост host."""
        return f"\\\\{host}\\{SMB_SHARE_NAME}\\{local.name}"

    def test_download_over_unc(self, share_dir):
        """Проверить список и скачивание по UNC-пути ресурса."""
        (share_dir / "Payment.txt").write_text("payment")
        client = TestClient(
            reload_app(files_directory=self.unc("localhost", share_dir))
        )

        data = client.get("/files").json()
        names = [f["name"] for f in data["availableFiles"]]
        assert sorted(names) == ["Payment.txt", "test1.txt"]

        response = client.get("/files/test1.txt")
        assert response.status_code == 200
        assert response.content == b"Test content 1"

        # Настоящее имя файла на ресурсе ищется по SMB
        response = client.get("/files/PAYMENT.TXT")
        assert response.status_code == 200
        assert response.content == b"payment"
        assert 'filename="Payment.txt"' in response.headers["content-disposition"]

        response = client.get("/files/missing.txt")
        assert response.status_code == 404
        assert response.json()["detail"] == "File not found"

    def test_share_access_denied(self, share_dir):
        """Проверить: запрет доступа к ресурсу — 403."""
        # Права ресурса проверяются при подключении к нему, поэтому
        # нужно новое подключение: по имени компьютера, а не localhost,
        # к которому могли подключиться другие тесты
        files_directory = self.unc(os.environ["COMPUTERNAME"], share_dir)
        smb_share(SMB_SHARE_BLOCK)
        try:
            client = TestClient(reload_app(files_directory=files_directory))
            listing = client.get("/files")
            download = client.get("/files/test1.txt")
        finally:
            smb_share(SMB_SHARE_UNBLOCK)

        assert listing.status_code == 403
        assert listing.json()["detail"].startswith(
            "Permission denied when reading directory:"
        )
        assert download.status_code == 403
        assert download.json()["detail"] == "Permission denied when reading file"

    def test_share_removed_and_restored(self, share_dir):
        """Проверить: пропавший ресурс — 404, вернувшийся — снова 200.

        Сервис не перезапускается: тот же клиент до и после.
        """
        client = TestClient(
            reload_app(files_directory=self.unc("localhost", share_dir))
        )
        assert client.get("/files").status_code == 200

        smb_share(SMB_SHARE_REMOVE)
        try:
            listing, listing_statuses = wait_for_status(client, "/files", 404)
            download, download_statuses = wait_for_status(
                client, "/files/test1.txt", 404
            )
        finally:
            smb_share(SMB_SHARE_CREATE)

        # Пока кэш SMB-клиента не устарел, возможен прежний ответ, а
        # в момент удаления — сбой ОС (500); итог — 404
        assert listing.status_code == 404, listing_statuses
        assert listing.json()["detail"] == "Files directory not found"
        assert set(listing_statuses) <= {200, 404, 500}, listing_statuses
        assert download.status_code == 404, download_statuses
        assert download.json()["detail"] == "File not found"
        assert set(download_statuses) <= {200, 404, 500}, download_statuses

        listing, statuses = wait_for_status(client, "/files", 200)
        assert listing.status_code == 200, statuses
        names = [f["name"] for f in listing.json()["availableFiles"]]
        assert names == ["test1.txt"]
        response, statuses = wait_for_status(client, "/files/test1.txt", 200)
        assert response.status_code == 200, statuses
        assert response.content == b"Test content 1"


class TestCORSConfiguration:
    """Тесты для конфигурации CORS."""

    def test_cors_headers_present(self, client):
        """Проверить, что заголовки CORS присутствуют."""
        response = client.get("/files", headers={"Origin": "http://example.com"})
        assert "access-control-allow-origin" in response.headers

    def test_cors_allows_all_origins_by_default(self, client):
        """Проверить, что CORS разрешает все источники
        по умолчанию.
        """
        response = client.get("/files", headers={"Origin": "http://example.com"})
        assert response.headers["access-control-allow-origin"] == "*"

    def test_cors_custom_origins(self, monkeypatch, test_files_dir):
        """Проверить, что CORS может быть настроен с конкретными
        источниками.
        """
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,https://example.com")
        test_app = reload_app(files_directory=test_files_dir)
        client = TestClient(test_app)

        response = client.get("/files", headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 200

    def test_cors_no_credentials_for_all_origins(self, client):
        """Проверить, что при `*` учётные данные не разрешаются даже
        для запроса с куками: источник запроса не возвращается.
        """
        response = client.get(
            "/files",
            headers={"Origin": "http://example.com", "Cookie": "session=1"},
        )
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers

    def test_cors_preflight_no_credentials_for_all_origins(self, client):
        """Проверить, что preflight-ответ при `*` тоже не разрешает
        учётные данные и не возвращает источник запроса.
        """
        response = client.options(
            "/files",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers

    def test_cors_credentials_for_custom_origins(self, monkeypatch, test_files_dir):
        """Проверить, что для явного списка источников разрешаются
        учётные данные, а в ответе возвращается источник запроса.
        """
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,https://example.com")
        test_app = reload_app(files_directory=test_files_dir)
        client = TestClient(test_app)

        response = client.get("/files", headers={"Origin": "http://localhost:3000"})
        assert (
            response.headers["access-control-allow-origin"] == "http://localhost:3000"
        )
        assert response.headers["access-control-allow-credentials"] == "true"

        # Источник не из списка не получает разрешения
        response = client.get("/files", headers={"Origin": "http://evil.example"})
        assert "access-control-allow-origin" not in response.headers

    def test_cors_empty_string_falls_back_to_default(self, monkeypatch, test_files_dir):
        """Проверить, что пустая строка CORS_ORIGINS возвращается
        к значению по умолчанию.
        """
        monkeypatch.setenv("CORS_ORIGINS", "")
        test_app = reload_app(files_directory=test_files_dir)
        client = TestClient(test_app)

        response = client.get("/files", headers={"Origin": "http://example.com"})
        assert response.status_code == 200
        # Должно вернуться к разрешению всех источников
        assert response.headers["access-control-allow-origin"] == "*"
        assert "access-control-allow-credentials" not in response.headers

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (
                " http://a.example , ,http://b.example ",
                ["http://a.example", "http://b.example"],
            ),
            (" , ", ["*"]),
            ("", ["*"]),
        ],
    )
    def test_cors_origins_parsing(self, value, expected):
        """Проверить: пробелы и пустые элементы отбрасываются."""
        from app.settings import Settings

        assert Settings(cors_origins=value).cors_origins_list == expected

    def test_cors_origins_from_dotenv(self, monkeypatch, tmp_path, test_files_dir):
        """Проверить: CORS_ORIGINS читается из .env рабочей папки."""
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text(
            "CORS_ORIGINS=http://localhost:3000\n", encoding="utf-8"
        )
        client = TestClient(reload_app(files_directory=test_files_dir))

        response = client.get("/files", headers={"Origin": "http://localhost:3000"})
        assert (
            response.headers["access-control-allow-origin"] == "http://localhost:3000"
        )
        assert response.headers["access-control-allow-credentials"] == "true"

    def test_cors_preflight_allows_any_method_and_header(self, client):
        """Проверить: preflight разрешает любые методы и заголовки."""
        response = client.options(
            "/files",
            headers={
                "Origin": "http://example.com",
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": "X-Custom-Header",
            },
        )
        assert response.status_code == 200
        assert "DELETE" in response.headers["access-control-allow-methods"]
        assert "x-custom-header" in (
            response.headers["access-control-allow-headers"].lower()
        )


class TestEdgeCases:
    """Тесты для граничных случаев и обработки ошибок."""

    def test_empty_directory(self):
        """Проверить вывод списка файлов в пустой директории."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            response = client.get("/files")
            assert response.status_code == 200
            data = response.json()
            assert data == {"availableFiles": [], "unavailableFiles": []}

    def test_filename_with_spaces(self):
        """Проверить загрузку файла с пробелами в имени."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "file with spaces.txt"
            test_file.write_text("Content")

            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            response = client.get("/files/file with spaces.txt")
            assert response.status_code == 200
            assert response.content == b"Content"

    def test_filename_with_special_characters(self):
        """Проверить загрузку файла со специальными символами."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "file-name_123.txt"
            test_file.write_text("Special content")

            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            response = client.get("/files/file-name_123.txt")
            assert response.status_code == 200
            assert response.content == b"Special content"

    def test_cyrillic_filename(self, tmp_path):
        """Проверить имя на кириллице: в списке и при скачивании.

        Имя не из ASCII передаётся в Content-Disposition через
        filename* (RFC 5987).
        """
        name = "Платёжка №1.txt"
        (tmp_path / name).write_text("content", encoding="utf-8")
        client = TestClient(reload_app(files_directory=str(tmp_path)))

        names = [f["id"] for f in client.get("/files").json()["availableFiles"]]
        assert names == [name]
        response = client.get("/files/" + quote(name))
        assert response.status_code == 200
        assert response.content == b"content"
        assert response.headers["content-disposition"] == (
            f"attachment; filename*=utf-8''{quote(name)}"
        )

    def test_large_file_listing(self):
        """Проверить вывод списка директории с большим количеством
        файлов.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Создаем 100 тестовых файлов
            for i in range(100):
                test_file = Path(tmpdir) / f"file_{i:03d}.txt"
                test_file.write_text(f"Content {i}")

            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            response = client.get("/files")
            assert response.status_code == 200
            data = response.json()
            all_files = data["availableFiles"] + data["unavailableFiles"]
            assert len(all_files) == 100
            # Проверяем, что файлы отсортированы по дате создания
            # (новые первыми)
            assert_sorted_by_created_at(data["availableFiles"])
            assert_sorted_by_created_at(data["unavailableFiles"])


class TestAPIDocumentation:
    """Тесты для конечных точек документации API."""

    def test_openapi_schema_accessible(self, client):
        """Проверить, что схема OpenAPI доступна."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert "openapi" in schema
        assert "info" in schema
        assert schema["info"]["title"] == "File Picker API"

    def test_openapi_schema_version(self, client):
        """Проверить, что версия в схеме — версия пакета."""
        import app

        schema = client.get("/openapi.json").json()
        assert schema["info"]["version"] == app.__version__

    def test_openapi_schema_describes_files_endpoints(self, client):
        """Проверить описание эндпоинтов /files и моделей ответа."""
        schema = client.get("/openapi.json").json()

        assert "get" in schema["paths"]["/files"]
        download = schema["paths"]["/files/{fileId}"]["get"]
        assert [p["name"] for p in download["parameters"]] == ["fileId"]
        models = schema["components"]["schemas"]
        assert set(models["FileListResponse"]["properties"]) == {
            "availableFiles",
            "unavailableFiles",
        }
        assert set(models["FileInfo"]["properties"]) == {
            "id",
            "name",
            "size",
            "createdAt",
        }

    def test_docs_endpoint_accessible(self, client):
        """Проверить, что конечная точка /docs доступна."""
        response = client.get("/docs")
        assert response.status_code == 200
        assert "swagger-ui" in response.text

    def test_redoc_endpoint_accessible(self, client):
        """Проверить, что конечная точка /redoc доступна."""
        response = client.get("/redoc")
        assert response.status_code == 200
        assert "redoc" in response.text

    def test_openapi_version_without_installed_package(
        self, test_files_dir, monkeypatch
    ):
        """Проверить версию 0.0.0, если app/_version.py нет."""
        # None в sys.modules: import app._version даёт ImportError.
        # Не patch.dict: при восстановлении он очищает sys.modules и
        # теряет модули, импортированные внутри блока
        with monkeypatch.context() as m:
            m.setitem(sys.modules, "app._version", None)
            client = TestClient(reload_app(files_directory=test_files_dir))
            response = client.get("/openapi.json")
        # Вернуть app настоящую версию для следующих тестов
        reload_app()
        assert response.status_code == 200
        assert response.json()["info"]["version"] == "0.0.0"


class TestExceptionHandling:
    """Тесты для обработки исключений и ошибочных случаев."""

    def test_list_files_permission_error(self):
        """Проверить вывод списка файлов, когда в доступе отказано."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            # Эмулируем отказ в доступе через scandir, а не через chmod:
            # под root права на директорию не проверяются
            error = PermissionError(errno.EACCES, "Permission denied", tmpdir)
            with mock.patch("os.scandir", side_effect=error):
                response = client.get("/files")
            # Должны получить ошибку 403 из-за отказа в доступе,
            # а текст исходного исключения должен попасть в detail.
            # Сравниваем с str(error), а не с tmpdir: путь в сообщении
            # выводится через repr, и на Windows слэши удваиваются
            assert response.status_code == 403
            assert response.json()["detail"] == (
                f"Permission denied when reading directory: {error}"
            )

    def test_list_files_oserror(self):
        """Проверить обработку OSError при чтении директории."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")

            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            # Мокируем scandir для возбуждения OSError
            with mock.patch(
                "os.scandir",
                side_effect=OSError("Disk error"),
            ):
                response = client.get("/files")
                # Должны получить ошибку 500 из-за OSError
                assert response.status_code == 500
                assert "OS error" in response.json()["detail"]

    def test_list_files_unexpected_error(self):
        """Проверить обработку неожиданных исключений."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")

            test_app = reload_app(files_directory=tmpdir)
            client = TestClient(test_app)

            # Мокируем scandir для возбуждения произвольного Exception
            with mock.patch(
                "os.scandir",
                side_effect=RuntimeError("Unexpected error"),
            ):
                response = client.get("/files")
                # Должны получить ошибку 500 из-за неожиданного
                # исключения
                assert response.status_code == 500
                assert "Unexpected error" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("error", "status_code", "prefix"),
        [
            (PermissionError("Access is denied"), 403, "Permission denied"),
            # Не «сетевой путь не найден»: на Windows это
            # FileNotFoundError и 404 (см. *_network_path_not_found)
            (OSError(errno.EIO, "Input/output error"), 500, "OS error"),
        ],
    )
    def test_list_files_directory_check_error(self, error, status_code, prefix):
        """Проверить ошибку ОС при проверке самой директории."""
        with tempfile.TemporaryDirectory() as tmpdir:
            client = TestClient(reload_app(files_directory=tmpdir))
            with mock.patch("pathlib.Path.stat", side_effect=error):
                response = client.get("/files")
            assert response.status_code == status_code
            assert response.json()["detail"] == (
                f"{prefix} when reading directory: {error}"
            )

    @pytest.mark.parametrize(
        ("error", "status_code", "prefix"),
        [
            (PermissionError("Access is denied"), 403, "Permission denied"),
            # Не «сетевой путь не найден»: на Windows это
            # FileNotFoundError и 404 (см. *_network_path_not_found)
            (OSError(errno.EIO, "Input/output error"), 500, "OS error"),
        ],
    )
    def test_download_file_check_error(
        self, client, caplog, error, status_code, prefix
    ):
        """Проверить ошибку ОС при проверке скачиваемого файла.

        Текст исходной ошибки (с путём к сетевой папке) в ответ не
        попадает, а пишется в лог.
        """
        with fail_stat_for("test1.txt", error):
            response = client.get("/files/test1.txt")
        assert response.status_code == status_code
        assert response.json()["detail"] == f"{prefix} when reading file"
        assert str(error) in caplog.text
        assert "test1.txt" in caplog.text

    @windows_only
    @pytest.mark.parametrize("winerror", [53, 67])
    def test_list_files_network_path_not_found(self, winerror):
        """Проверить: «сетевой путь не найден» на Windows — 404.

        ERROR_BAD_NETPATH (53) и ERROR_BAD_NET_NAME (67) Python
        превращает в FileNotFoundError.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            client = TestClient(reload_app(files_directory=tmpdir))
            error = OSError(None, "The network path was not found", None, winerror)
            with mock.patch("pathlib.Path.stat", side_effect=error):
                response = client.get("/files")
            assert response.status_code == 404
            assert response.json()["detail"] == "Files directory not found"

    @windows_only
    @pytest.mark.parametrize(
        "file_id", ["a%2A.txt", "a%3F.txt", "a%3Cb.txt", "a%3Eb.txt", "a%7Cb.txt"]
    )
    def test_download_invalid_chars_on_windows(self, client, file_id):
        """Проверить: имя с недопустимыми в Windows символами — 400."""
        response = client.get("/files/" + file_id)
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    @windows_only
    @pytest.mark.parametrize(
        "file_id", ["NUL", "nul.txt", "CON", "COM1.txt", "COM0.txt", "AUX", "CONIN$"]
    )
    def test_download_reserved_device_names_on_windows(self, client, file_id):
        """Проверить: имя устройства — 400 без обращения к ФС."""
        error = AssertionError("resolve() must not be called")
        with mock.patch("pathlib.Path.resolve", side_effect=error):
            response = client.get("/files/" + file_id)
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    @pytest.mark.parametrize(
        "name",
        [
            "console.txt",
            "COM10.txt",
            "LPT00.txt",
            "nul_report.txt",
            "LPT.txt",
            "xcon.txt",
        ],
    )
    def test_download_name_resembling_device(self, test_files_dir, name):
        """Проверить: имя, похожее на имя устройства, скачивается."""
        (Path(test_files_dir) / name).write_text("content")
        client = TestClient(reload_app(files_directory=test_files_dir))

        with as_windows():
            response = client.get("/files/" + name)
        assert response.status_code == 200
        assert response.content == b"content"

    def test_list_files_skips_device_name(self, test_files_dir):
        """Проверить: файл с именем устройства не попадает в список.

        Скачать его нельзя (400), поэтому и показывать нельзя. На
        Windows 10 такой файл не создать: запись уходит в устройство.
        """
        try:
            (Path(test_files_dir) / "nul.txt").write_text("device")
        except OSError:
            pytest.skip("ОС не даёт создать файл с именем устройства")
        if "nul.txt" not in [p.name for p in Path(test_files_dir).iterdir()]:
            pytest.skip("ОС не даёт создать файл с именем устройства")
        client = TestClient(reload_app(files_directory=test_files_dir))

        with as_windows():
            data = client.get("/files").json()
            download = client.get("/files/nul.txt")
        names = [f["name"] for f in data["availableFiles"] + data["unavailableFiles"]]
        assert sorted(names) == ["test1.txt", "test2.txt"]
        assert download.status_code == 400

    @pytest.mark.parametrize(
        "error",
        [
            OSError(errno.ENAMETOOLONG, "File name too long"),
            OSError(errno.ELOOP, "Too many levels of symbolic links"),
        ],
    )
    def test_download_invalid_name_error(self, client, error):
        """Проверить, что ошибка «недопустимое имя» даёт 400."""
        with fail_stat_for("test1.txt", error):
            response = client.get("/files/test1.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    def test_download_invalid_name_on_windows(self, client):
        """Проверить ERROR_INVALID_NAME на Windows (имя a*.txt)."""
        error = OSError(errno.EINVAL, "The filename syntax is incorrect")
        error.winerror = 123
        with fail_stat_for("test1.txt", error):
            response = client.get("/files/test1.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    def test_download_network_error_on_windows(self, client):
        """Проверить, что сбой сети на Windows остаётся ошибкой 500."""
        # ERROR_NETNAME_DELETED тоже приходит с errno EINVAL
        error = OSError(errno.EINVAL, "The network name is no longer available")
        error.winerror = 64
        with fail_stat_for("test1.txt", error):
            response = client.get("/files/test1.txt")
        assert response.status_code == 500
        assert response.json()["detail"] == "OS error when reading file"

    def test_download_too_long_name(self, client):
        """Проверить, что слишком длинное имя файла даёт 400."""
        response = client.get("/files/" + "a" * 300 + ".txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    @resolves_paths
    def test_download_symlink_loop_on_python_311(self, client):
        """Проверить RuntimeError из resolve() при петле ссылок."""
        error = RuntimeError("Symlink loop from 'loop.txt'")
        with fail_for("resolve", "loop.txt", error):
            response = client.get("/files/loop.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    @pytest.mark.parametrize("file_id", ["..", ".", "../outside.txt"])
    def test_security_rejects_paths_outside_directory(self, test_files_dir, file_id):
        """Проверить отказ для путей, не указывающих на файл директории.

        Вызываем обработчик напрямую: HTTP-клиент нормализует ".."
        в URL до того, как запрос дойдёт до маршрута.
        """
        from fastapi import HTTPException

        reload_app(files_directory=test_files_dir)
        from app.handlers.files import get_file

        with pytest.raises(HTTPException) as exc_info:
            get_file(file_id)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Invalid filename"

    @pytest.mark.parametrize(
        "file_id",
        ["/etc/passwd", "sub/x.txt", "./test1.txt", "test1.txt/", "a\x00.txt"],
    )
    def test_security_rejects_non_name_before_fs_access(self, test_files_dir, file_id):
        """Проверить отказ для пути вместо имени до обращения к ФС.

        На Windows так же отсекается UNC-путь к чужому хосту: иначе
        resolve() подключился бы к нему.
        """
        from fastapi import HTTPException

        reload_app(files_directory=test_files_dir)
        from app.handlers.files import get_file

        error = AssertionError("resolve() must not be called")
        with (
            mock.patch("pathlib.Path.resolve", side_effect=error),
            pytest.raises(HTTPException) as exc_info,
        ):
            get_file(file_id)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Invalid filename"

    @windows_only
    @pytest.mark.parametrize(
        "file_id",
        [
            # UNC-пути в любом написании и пути к устройствам
            r"\\attacker\s\x.txt",
            "//attacker/s/x.txt",
            r"\/attacker\s\x.txt",
            r"\\attacker",
            r"\\?\UNC\attacker\s\x.txt",
            r"\\?\C:\Windows\win.ini",
            r"\\.\C:\Windows\win.ini",
            r"\\.\pipe\x",
            r"\??\C:\Windows\win.ini",
            # Диск и корень диска
            r"C:\x.txt",
            "C:/x.txt",
            r"\Windows\win.ini",
            # Разделители и точки
            r"subdir\inner.txt",
            r"subdir\..\test1.txt",
            "test1.txt\\",
            r".\test1.txt",
            r"..\main.py",
            "test1.txt. .",
            # Альтернативный поток NTFS
            "test1.txt::$DATA",
        ],
    )
    def test_security_rejects_windows_paths_before_fs_access(
        self, test_files_dir, file_id
    ):
        """Проверить отказ для пути Windows до обращения к ФС.

        UNC-путь заменил бы директорию при склейке, и resolve()
        подключился бы к чужому хосту, отдав ему NTLM-хеш.
        """
        from fastapi import HTTPException

        reload_app(files_directory=test_files_dir)
        from app.handlers.files import get_file

        error = AssertionError("resolve() must not be called")
        with (
            mock.patch("pathlib.Path.resolve", side_effect=error),
            mock.patch("pathlib.Path.stat", side_effect=error),
            pytest.raises(HTTPException) as exc_info,
        ):
            get_file(file_id)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Invalid filename"

    @windows_only
    @pytest.mark.parametrize(
        "url", ["/files/%5C%5Cattacker%5Cs%5Cx.txt", "/files/subdir%5Cinner.txt"]
    )
    def test_security_rejects_windows_paths_over_http(self, client, url):
        """Проверить отказ для UNC-пути и обратного слэша в URL."""
        error = AssertionError("resolve() must not be called")
        with mock.patch("pathlib.Path.resolve", side_effect=error):
            response = client.get(url)
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    @windows_only
    @pytest.mark.parametrize(
        "unc_prefix", ["\\\\localhost\\", "\\\\?\\UNC\\localhost\\"]
    )
    def test_unc_files_directory(self, tmp_path, unc_prefix):
        """Проверить работу с директорией по UNC-пути, как в сборке.

        Сетевую папку заменяет административный ресурс диска (C$) этой
        же машины. Без него тест пропускается, а в CI
        (REQUIRE_UNC_SHARE) падает: пропуск скрыл бы непроверенный
        сценарий.
        """
        local = tmp_path.resolve()
        unc = f"{unc_prefix}{local.drive[0]}${str(local)[2:]}"
        if not Path(unc).is_dir():
            msg = f"Административный ресурс недоступен: {unc}"
            if os.environ.get("REQUIRE_UNC_SHARE"):
                pytest.fail(msg)
            pytest.skip(msg)
        (local / "test1.txt").write_text("Test content 1")
        client = TestClient(reload_app(files_directory=unc))

        names = [f["name"] for f in client.get("/files").json()["availableFiles"]]
        assert names == ["test1.txt"]
        # Скачивание по UNC-пути: lstat() и поиск настоящего имени в
        # директории ресурса, без resolve()
        response = client.get("/files/test1.txt")
        assert response.status_code == 200
        assert response.content == b"Test content 1"

    def test_security_rejects_full_path_inside_directory(self, test_files_dir):
        """Проверить отказ для полного пути даже к своему файлу."""
        from fastapi import HTTPException

        reload_app(files_directory=test_files_dir)
        from app.handlers.files import get_file

        error = AssertionError("resolve() must not be called")
        with (
            mock.patch("pathlib.Path.resolve", side_effect=error),
            pytest.raises(HTTPException) as exc_info,
        ):
            get_file(str(Path(test_files_dir) / "test1.txt"))
        assert exc_info.value.status_code == 400

    @pytest.mark.parametrize(
        "file_id",
        [
            "test1.txt:stream",
            "C:x.txt",
            ".. ",
            "...",
            "test1.txt.",
            # Имена устройств, в том числе с расширением и пробелами
            "CON",
            "prn",
            "nul.txt",
            "NUL .txt",
            "aux.tar.gz",
            "com1.txt",
            "COM9",
            "COM0.txt",
            "lpt0.TXT",
            "LPT9.log",
            "LPT\xb9",
            "CONIN$",
            "conout$.txt",
            # Недопустимые символы, они же шаблоны поиска Windows
            "a*.txt",
            "a?.txt",
            'a"b.txt',
            "a<b.txt",
            "a>b.txt",
            "a|b.txt",
        ],
    )
    def test_security_rejects_windows_only_names(self, test_files_dir, file_id):
        """Проверить отказ для имён, недопустимых на Windows."""
        from fastapi import HTTPException

        reload_app(files_directory=test_files_dir)
        from app.handlers import files

        error = AssertionError("resolve() must not be called")
        with (
            mock.patch.object(files.sys, "platform", "win32"),
            mock.patch("pathlib.Path.resolve", side_effect=error),
            pytest.raises(HTTPException) as exc_info,
        ):
            files.get_file(file_id)
        assert exc_info.value.status_code == 400
        assert exc_info.value.detail == "Invalid filename"

    @pytest.mark.parametrize(
        "error",
        [
            OSError(errno.EIO, "Network error", "/secret/share"),
            RuntimeError("Symlink loop from '/secret/share'"),
        ],
    )
    @resolves_paths
    def test_base_directory_error_does_not_leak_path(self, test_files_dir, error):
        """Проверить: сбой resolve() базы — 500 без пути в detail."""
        client = TestClient(reload_app(files_directory=test_files_dir))
        with fail_for("resolve", Path(test_files_dir).name, error):
            response = client.get("/files/test1.txt")
        assert response.status_code == 500
        assert response.json()["detail"] == "OS error when reading file"

    def test_security_null_byte_in_file_id(self, client):
        """Проверить, что NUL-байт в fileId даёт 400, а не 500."""
        response = client.get("/files/a%00.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    def test_list_files_directory_disappears_while_reading(self):
        """Проверить ответ 404, если директория исчезла при чтении."""
        with tempfile.TemporaryDirectory() as tmpdir:
            client = TestClient(reload_app(files_directory=tmpdir))
            error = FileNotFoundError("Directory was removed")
            with mock.patch("os.scandir", side_effect=error):
                response = client.get("/files")
            assert response.status_code == 404
            assert response.json()["detail"] == "Files directory not found"

    def test_list_files_skips_broken_and_vanished_files(self, test_files_dir):
        """Проверить пропуск битых ссылок и исчезнувших файлов."""
        symlink_or_skip(Path(test_files_dir) / "broken.txt", "missing.txt")
        client = TestClient(reload_app(files_directory=test_files_dir))

        error = FileNotFoundError("Removed")
        with fail_entry_stat_for("test2.txt", error):
            response = client.get("/files")
        assert response.status_code == 200
        names = [f["name"] for f in response.json()["availableFiles"]]
        assert names == ["test1.txt"]

    def test_list_files_ignores_directory_named_txt(self, test_files_dir):
        """Проверить: директории и файлы в них не попадают в список."""
        (Path(test_files_dir) / "folder.txt").mkdir()
        (Path(test_files_dir) / "folder.txt" / "nested.txt").write_text("x")
        (Path(test_files_dir) / "subdir" / "inner.txt").write_text("x")
        client = TestClient(reload_app(files_directory=test_files_dir))
        data = client.get("/files").json()
        names = [f["name"] for f in data["availableFiles"] + data["unavailableFiles"]]
        assert sorted(names) == ["test1.txt", "test2.txt"]

    def test_list_files_skips_invalid_name(self, client):
        """Проверить пропуск файла с ошибкой «недопустимое имя»."""
        error = OSError(errno.ELOOP, "Too many levels of symbolic links")
        with fail_entry_stat_for("test2.txt", error):
            response = client.get("/files")
        assert response.status_code == 200
        names = [f["name"] for f in response.json()["availableFiles"]]
        assert names == ["test1.txt"]

    def test_list_files_directory_disappears_after_listing(self, test_files_dir):
        """Проверить 404, если директория пропала после перечисления."""
        client = TestClient(reload_app(files_directory=test_files_dir))
        base = Path(test_files_dir)
        original_stat = Path.stat
        base_stat_calls = []

        def fake_stat(self, *args, **kwargs):
            # Первый stat директории (проверка в list_files) проходит,
            # дальше пропала и сама директория
            if self == base:
                base_stat_calls.append(self)
                if len(base_stat_calls) > 1:
                    raise FileNotFoundError(self)
            return original_stat(self, *args, **kwargs)

        # Файлы из перечисления уже не находятся
        error = FileNotFoundError("Removed")
        with (
            fail_entry_stat_for("test1.txt", error),
            mock.patch.object(Path, "stat", autospec=True, side_effect=fake_stat),
        ):
            response = client.get("/files")
        assert response.status_code == 404
        assert response.json()["detail"] == "Files directory not found"

    def test_list_files_file_permission_error(self, client):
        """Проверить, что отказ в доступе к файлу списка даёт 403."""
        error = PermissionError("Access is denied")
        with fail_entry_stat_for("test2.txt", error):
            response = client.get("/files")
        assert response.status_code == 403
        assert response.json()["detail"] == (
            f"Permission denied when reading directory: {error}"
        )


class TestCreatedAt:
    """Тесты выбора времени для createdAt."""

    @pytest.mark.parametrize(
        ("platform", "fields", "expected"),
        [
            ("linux", {"st_birthtime": 1.0}, 1.0),
            ("win32", {}, 2.0),
            ("linux", {}, 3.0),
            # FILETIME 0 (1601 год): fromtimestamp на Windows тут падает
            ("win32", {"st_ctime": -11644473600.0}, -11644473600.0),
        ],
    )
    def test_created_at_source(self, platform, fields, expected):
        """Проверить: birthtime, на Windows ctime, иначе mtime."""
        from app.handlers import files

        file_stat = SimpleNamespace(**{"st_ctime": 2.0, "st_mtime": 3.0, **fields})
        with mock.patch.object(files.sys, "platform", platform):
            created_at = files.file_created_at(file_stat)
        expected_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=expected)
        assert created_at == expected_at

    @pytest.mark.parametrize(
        ("timestamp", "expected"),
        [
            (1e12, datetime.max.replace(tzinfo=UTC)),
            (-1e12, datetime.min.replace(tzinfo=UTC)),
        ],
    )
    def test_created_at_out_of_range(self, timestamp, expected):
        """Проверить, что повреждённая метка не роняет список."""
        from app.handlers import files

        file_stat = SimpleNamespace(st_ctime=timestamp, st_mtime=timestamp)
        with mock.patch.object(files.sys, "platform", "linux"):
            assert files.file_created_at(file_stat) == expected

    @pytest.mark.parametrize(
        ("timestamp", "expected"),
        [
            # FILETIME 0: такое оставляют архиваторы и robocopy
            (-11644473600.0, "1601-01-01T00:00:00Z"),
            (1e12, "9999-12-31T23:59:59.999999Z"),
            (-1e12, "0001-01-01T00:00:00Z"),
        ],
    )
    def test_list_files_edge_created_at(self, client, timestamp, expected):
        """Проверить: краевая метка времени не роняет список."""
        with entry_time_for("test1.txt", timestamp):
            response = client.get("/files")
        assert response.status_code == 200
        items = {f["name"]: f for f in response.json()["availableFiles"]}
        assert items["test1.txt"]["createdAt"] == expected
        assert "test2.txt" in items


class TestListingScandir:
    """Тесты чтения списка через os.scandir."""

    def test_list_files_stats_only_directory(self, client):
        """Проверить: Path.stat() вызывается только для директории.

        Атрибуты файлов берутся из DirEntry.stat(): на Windows для
        обычных файлов он не делает отдельного запроса к ФС.
        """
        with mock.patch.object(
            Path, "stat", autospec=True, side_effect=Path.stat
        ) as stat:
            response = client.get("/files")
        assert response.status_code == 200
        assert len(response.json()["availableFiles"]) == 2
        assert stat.call_count == 1

    def test_list_files_reads_directory_once(self, client):
        """Проверить, что директория перечисляется один раз."""
        with mock.patch("os.scandir", side_effect=os.scandir) as scandir:
            response = client.get("/files")
        assert response.status_code == 200
        assert scandir.call_count == 1

    @follows_symlinks
    def test_list_files_follows_symlink_to_file(self, test_files_dir):
        """Проверить: ссылка на файл в списке с атрибутами цели."""
        target = Path(test_files_dir) / "target.txt"
        target.write_bytes(b"x" * 123)
        symlink_or_skip(Path(test_files_dir) / "link.txt", target)
        client = TestClient(reload_app(files_directory=test_files_dir))

        response = client.get("/files")
        files = {f["name"]: f for f in response.json()["availableFiles"]}
        assert files["link.txt"]["size"] == 123

    def test_list_files_skips_symlink_loop(self, test_files_dir):
        """Проверить пропуск петли символических ссылок."""
        loop = Path(test_files_dir) / "loop.txt"
        symlink_or_skip(loop, loop)
        client = TestClient(reload_app(files_directory=test_files_dir))

        response = client.get("/files")
        assert response.status_code == 200
        names = [f["name"] for f in response.json()["availableFiles"]]
        assert sorted(names) == ["test1.txt", "test2.txt"]

    def test_list_files_created_at_matches_stat(self, test_files_dir):
        """Проверить: createdAt тот же, что из os.stat() файла."""
        from app.handlers import files

        file_path = Path(test_files_dir) / "test1.txt"
        os.utime(file_path, (1_600_000_000, 1_600_000_000))
        client = TestClient(reload_app(files_directory=test_files_dir))

        response = client.get("/files")
        items = {f["name"]: f for f in response.json()["availableFiles"]}
        expected = files.file_created_at(file_path.stat())
        created_at = datetime.fromisoformat(items["test1.txt"]["createdAt"])
        assert created_at == expected


def count_resolve(directory):
    """Вернуть патч Path.resolve, считающий вызовы для directory."""
    original = Path.resolve
    calls = []

    def fake(self, *args, **kwargs):
        if self == Path(directory):
            calls.append(kwargs.get("strict", args[0] if args else False))
        return original(self, *args, **kwargs)

    patch = mock.patch.object(Path, "resolve", autospec=True, side_effect=fake)
    return patch, calls


def fail_strict_resolve(directory):
    """Вернуть патч Path.resolve: строгий resolve() directory падает.

    Так ведёт себя недоступная сетевая папка: нестрогий resolve()
    отрабатывает, строгий — нет.
    """
    original = Path.resolve

    def fake(self, *, strict=False):
        if self == Path(directory) and strict:
            raise FileNotFoundError(errno.ENOENT, "Network path not found")
        return original(self, strict=strict)

    return mock.patch.object(Path, "resolve", autospec=True, side_effect=fake)


class TestFilesDirectoryCache:
    """Тесты кэша разрешённого пути к директории с файлами."""

    @resolves_paths
    def test_directory_resolved_once(self, client, test_files_dir):
        """Проверить, что путь к директории разрешается один раз."""
        patch, calls = count_resolve(test_files_dir)
        with patch:
            for _ in range(3):
                assert client.get("/files/test1.txt").status_code == 200
        assert calls == [True]

    @resolves_paths
    def test_error_does_not_fill_cache(self, client, test_files_dir):
        """Проверить, что при сбое директории кэш не заполняется."""
        error = OSError(errno.EIO, "Network error")
        with fail_for("resolve", Path(test_files_dir).name, error):
            response = client.get("/files/test1.txt")
        assert response.status_code == 500

        patch, calls = count_resolve(test_files_dir)
        with patch:
            for _ in range(2):
                assert client.get("/files/test1.txt").status_code == 200
        assert calls == [True]

    @resolves_paths
    def test_unavailable_directory_is_not_cached(self, client, test_files_dir):
        """Проверить: нестрогий resolve() не попадает в кэш.

        Пока сетевая папка недоступна, строгий resolve() падает, и
        скачивание идёт по нестрогому, как раньше.
        """
        with fail_strict_resolve(test_files_dir):
            assert client.get("/files/test1.txt").status_code == 200

        patch, calls = count_resolve(test_files_dir)
        with patch:
            for _ in range(2):
                assert client.get("/files/test1.txt").status_code == 200
        assert calls == [True]

    def test_directory_appears_later(self, tmp_path):
        """Проверить: папка, недоступная при первом запросе, потом
        работает без перезапуска.

        Путь к директории идёт через ещё не созданную ссылку: пока её
        нет, нестрогий resolve() возвращает неразрешённый путь.
        """
        real = tmp_path / "real"
        link = tmp_path / "link"
        files_dir = link / "share"
        client = TestClient(reload_app(files_directory=str(files_dir)))

        response = client.get("/files/test1.txt")
        assert response.status_code == 404
        assert response.json()["detail"] == "File not found"

        (real / "share").mkdir(parents=True)
        (real / "share" / "test1.txt").write_text("content")
        symlink_or_skip(link, real)
        response = client.get("/files/test1.txt")
        assert response.status_code == 200
        assert response.content == b"content"

    def test_listing_recovers_when_directory_appears(self, tmp_path):
        """Проверить: список снова работает, когда папка появилась.

        Сценарий «Сетевая папка снова доступна»: без перезапуска сервиса
        следующий GET /files отвечает 200.
        """
        files_dir = tmp_path / "share"
        client = TestClient(reload_app(files_directory=str(files_dir)))

        response = client.get("/files")
        assert response.status_code == 404
        assert response.json()["detail"] == "Files directory not found"

        files_dir.mkdir()
        (files_dir / "test1.txt").write_text("content")
        response = client.get("/files")
        assert response.status_code == 200
        names = [f["name"] for f in response.json()["availableFiles"]]
        assert names == ["test1.txt"]

    @pytest.mark.parametrize(
        ("error", "status_code"),
        [
            (FileNotFoundError(errno.ENOENT, "The network path was not found"), 404),
            (OSError(errno.EIO, "The network name is no longer available"), 500),
        ],
    )
    def test_listing_and_download_recover_after_network_error(
        self, client, test_files_dir, error, status_code
    ):
        """Проверить восстановление после сбоя сетевой папки.

        Пока папка недоступна, строгий resolve() директории и stat() и
        lstat() директории и файла падают, а нестрогий resolve()
        отрабатывает — так ведёт себя недоступная сетевая папка. После
        сбоя оба эндпоинта работают без перезапуска: сбой не оставляет
        следа в кэше пути директории.
        """
        with (
            fail_strict_resolve(test_files_dir),
            fail_stat_for((Path(test_files_dir).name, "test1.txt"), error),
        ):
            assert client.get("/files").status_code == status_code
            assert client.get("/files/test1.txt").status_code == status_code

        response = client.get("/files")
        assert response.status_code == 200
        assert len(response.json()["availableFiles"]) == 2
        download = client.get("/files/test1.txt")
        assert download.status_code == 200
        assert download.content == b"Test content 1"

    def test_stale_cache_is_refreshed(self, tmp_path):
        """Проверить: директория-ссылка перенаправлена — файл берётся
        из новой цели.

        Не на Windows устаревший путь в кэше разрешается заново; на
        Windows кэша нет, и файл открывается по пути из настроек.
        """
        for name in ("a", "b"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "test1.txt").write_text(name)
        link = tmp_path / "link"
        symlink_or_skip(link, tmp_path / "a")
        client = TestClient(reload_app(files_directory=str(link)))
        assert client.get("/files/test1.txt").content == b"a"

        link.unlink()
        link.symlink_to(tmp_path / "b", target_is_directory=True)
        response = client.get("/files/test1.txt")
        assert response.status_code == 200
        assert response.content == b"b"

    @follows_symlinks
    @pytest.mark.usefixtures("subdir_link")
    def test_cache_is_refreshed_before_rejecting(self, test_files_dir):
        """Проверить: перед отказом 400 путь разрешается заново.

        Кэш при этом не очищается: следующее скачивание берёт путь
        из него.
        """
        client = TestClient(reload_app(files_directory=test_files_dir))
        assert client.get("/files/test1.txt").status_code == 200

        patch, calls = count_resolve(test_files_dir)
        with patch:
            response = client.get("/files/link.txt")
            assert client.get("/files/test1.txt").status_code == 200
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"
        assert calls == [True]

    @follows_symlinks
    @pytest.mark.usefixtures("subdir_link")
    def test_refresh_unavailable_directory_is_server_error(self, test_files_dir):
        """Проверить: папка, недоступная при повторной проверке, — 500.

        Нестрогий resolve() тут не используется: сравнение с
        неразрешённым путём дало бы клиенту 400 за сбой сервера. Путь
        убирается из кэша, и следующее скачивание разрешает его заново.
        """
        client = TestClient(reload_app(files_directory=test_files_dir))
        assert client.get("/files/test1.txt").status_code == 200

        with fail_strict_resolve(test_files_dir):
            response = client.get("/files/link.txt")
        assert response.status_code == 500
        assert response.json()["detail"] == "OS error when reading file"

        patch, calls = count_resolve(test_files_dir)
        with patch:
            assert client.get("/files/test1.txt").status_code == 200
        assert calls == [True]

    @resolves_paths
    @pytest.mark.usefixtures("subdir_link")
    def test_fresh_directory_is_not_resolved_twice(self, test_files_dir):
        """Проверить: свежий путь директории не перепроверяется."""
        client = TestClient(reload_app(files_directory=test_files_dir))

        patch, calls = count_resolve(test_files_dir)
        with patch:
            response = client.get("/files/link.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"
        assert calls == [True]

    @resolves_paths
    @pytest.mark.usefixtures("subdir_link")
    def test_unavailable_directory_rejects_as_before(self, test_files_dir):
        """Проверить: без кэша несовпадение даёт 400, как и раньше.

        Например, на сетевой папке, где строгий resolve() не работает.
        """
        client = TestClient(reload_app(files_directory=test_files_dir))

        with fail_strict_resolve(test_files_dir):
            response = client.get("/files/link.txt")
        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid filename"

    @follows_symlinks
    @pytest.mark.usefixtures("subdir_link")
    @pytest.mark.parametrize(
        "error",
        [
            OSError(errno.EIO, "Network error", "/secret/share"),
            RuntimeError("Symlink loop from '/secret/share'"),
        ],
    )
    def test_refresh_error_is_server_error(self, test_files_dir, error):
        """Проверить: сбой при повторном разрешении — 500 и сброс кэша.

        Сбой директории — ошибка сервера, как и раньше; путь в detail
        не попадает.
        """
        client = TestClient(reload_app(files_directory=test_files_dir))
        assert client.get("/files/test1.txt").status_code == 200

        with fail_for("resolve", Path(test_files_dir).name, error):
            response = client.get("/files/link.txt")
        assert response.status_code == 500
        assert response.json()["detail"] == "OS error when reading file"

        patch, calls = count_resolve(test_files_dir)
        with patch:
            assert client.get("/files/test1.txt").status_code == 200
        assert calls == [True]


class TestMainExecution:
    """Тесты для блока выполнения main."""

    def test_main_module_directly(self):
        """Проверить выполнение app/__main__.py с __name__,
        установленным в '__main__'.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            files_dir = Path(tmpdir) / "main_test_dir"

            # Перезагружаем модули и патчим files_directory
            reload_app(files_directory=str(files_dir))

            with mock.patch("uvicorn.run") as mock_run:
                # Выполняем файл app/__main__.py напрямую
                main_file = Path(__file__).parent.parent / "app" / "__main__.py"
                # Кодировка явно: на Windows по умолчанию берётся
                # кодовая страница системы (cp1252 в CI), а в файле
                # кириллица
                with open(main_file, encoding="utf-8") as f:
                    code = compile(f.read(), str(main_file), "exec")

                # Создаем пространство имен с __name__
                # как '__main__'
                namespace = {"__name__": "__main__"}
                exec(code, namespace)

                # Сервис слушает все интерфейсы на порту 8000
                mock_run.assert_called_once_with(
                    namespace["app"], host="0.0.0.0", port=8000
                )
                # Проверяем, что директория была создана
                assert files_dir.exists()

    def test_unreachable_directory_does_not_stop_startup(self, caplog):
        """Проверить, что недоступная директория не мешает запуску."""
        reload_app(files_directory=r"\\unreachable\share")
        from app.__main__ import ensure_files_directory

        error = OSError("The network path was not found")
        with mock.patch("pathlib.Path.mkdir", side_effect=error):
            ensure_files_directory()
        assert "The network path was not found" in caplog.text


class TestFilesDirectoryIsFixed:
    """Требование: пользователь не может подменить директорию."""

    ATTACKER_DIR = "/attacker/controlled/dir"

    @pytest.fixture
    def settings_module(self, monkeypatch, tmp_path):
        """Чистый модуль настроек, запущенный из временной папки."""
        monkeypatch.chdir(tmp_path)
        # reload создаёт новый get_settings с пустым lru_cache
        module = importlib.reload(importlib.import_module("app.settings"))
        yield module
        # Перезагружаем и обработчики с приложением: иначе они остаются
        # привязаны к get_settings из модуля до перезагрузки. Сначала
        # возвращаем рабочую папку и окружение (не читать .env теста)
        monkeypatch.undo()
        reload_app()

    def test_files_directory_is_not_a_settings_field(self, settings_module):
        """Проверить, что путь не является полем настроек."""
        assert "files_directory" not in settings_module.Settings.model_fields

    def test_env_variable_does_not_override(self, settings_module, monkeypatch):
        """Проверить, что переменная окружения игнорируется."""
        expected = settings_module.Settings.files_directory
        assert expected != self.ATTACKER_DIR
        monkeypatch.setenv("FILES_DIRECTORY", self.ATTACKER_DIR)

        assert settings_module.get_settings().files_directory == expected

    def test_dotenv_does_not_override(self, settings_module, tmp_path):
        """Проверить, что .env не подменяет путь.

        Вместо подмены приложение отказывается запускаться.
        """
        from pydantic import ValidationError

        (tmp_path / ".env").write_text(
            f"FILES_DIRECTORY={self.ATTACKER_DIR}\n", encoding="utf-8"
        )

        with pytest.raises(ValidationError):
            settings_module.Settings()

    @pytest.fixture
    def served_dir(self, settings_module, monkeypatch, tmp_path):  # noqa: ARG002
        """Директория, зашитая в сборку, и директория злоумышленника.

        Путь задаётся так же, как в сборке: через app._build_config.
        Переменная окружения FILES_DIRECTORY указывает на чужую
        директорию с файлом-меткой. settings_module нужна ради её
        завершения: она снимает эти подмены и перезагружает приложение.
        """
        served = tmp_path / "served"
        attacker = tmp_path / "attacker"
        attacker.mkdir()
        (attacker / "marker.txt").write_text("attacker")

        build_config = types.ModuleType("app._build_config")
        build_config.FILES_DIRECTORY = str(served)
        monkeypatch.setitem(sys.modules, "app._build_config", build_config)
        monkeypatch.setenv("FILES_DIRECTORY", str(attacker))
        return served, attacker

    def test_build_config_directory_is_served(self, served_dir):
        """Проверить: API работает с директорией из сборки, а
        переменная окружения не подменяет её.
        """
        served, _ = served_dir
        served.mkdir()
        (served / "payment.txt").write_text("payment")
        client = TestClient(reload_app())

        data = client.get("/files").json()
        assert [f["name"] for f in data["availableFiles"]] == ["payment.txt"]
        assert client.get("/files/payment.txt").content == b"payment"
        response = client.get("/files/marker.txt")
        assert response.status_code == 404

    def test_command_line_does_not_override(self, served_dir, monkeypatch):
        """Проверить: аргументы командной строки не задают директорию.

        При запуске создаётся директория из сборки, а не переданная.
        """
        served, attacker = served_dir
        other = attacker.parent / "from_argv"
        monkeypatch.setattr(
            sys, "argv", ["FilePickerAPI.exe", "--files-directory", str(other)]
        )
        reload_app()

        main_file = Path(__file__).parent.parent / "app" / "__main__.py"
        code = compile(main_file.read_text(encoding="utf-8"), str(main_file), "exec")
        namespace = {"__name__": "__main__"}
        with mock.patch("uvicorn.run") as mock_run:
            exec(code, namespace)

        mock_run.assert_called_once_with(namespace["app"], host="0.0.0.0", port=8000)
        assert served.is_dir()
        assert not other.exists()

    def test_constructor_argument_does_not_override(self, settings_module):
        """Проверить, что путь нельзя передать при создании настроек."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            settings_module.Settings(files_directory=self.ATTACKER_DIR)
