"""
Комплексные тесты для File Picker API.
"""

import errno
import importlib
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

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
    """
    if len(files) > 1:
        created_times = [item["createdAt"] for item in files]
        assert created_times == sorted(created_times, reverse=True)


def fail_for(method, name, error):
    """Вернуть патч метода Path, который падает только для файла name.

    Падать на всех путях нельзя: ошибка случилась бы уже на базовой
    директории (на Python 3.11 resolve() сам вызывает stat()).
    """
    original = getattr(Path, method)

    def fake(self, *args, **kwargs):
        if self.name == name:
            raise error
        return original(self, *args, **kwargs)

    return mock.patch.object(Path, method, autospec=True, side_effect=fake)


def fail_stat_for(name, error):
    """Вернуть патч Path.stat, который падает только для файла name."""
    return fail_for("stat", name, error)


class FailingEntry:
    """Обёртка DirEntry, у которой stat() падает с заданной ошибкой."""

    def __init__(self, entry, error):
        self._entry = entry
        self._error = error

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def stat(self, **_kwargs):
        raise self._error


def fail_entry_stat_for(name, error):
    """Вернуть патч os.scandir: DirEntry.stat() падает для файла name.

    Методы DirEntry нельзя подменить напрямую, поэтому подменяем сами
    элементы перечисления.
    """
    original = os.scandir

    @contextmanager
    def fake(path):
        with original(path) as it:
            yield [FailingEntry(e, error) if e.name == name else e for e in it]

    return mock.patch("os.scandir", side_effect=fake)


def symlink_or_skip(link, target):
    """Создать символическую ссылку или пропустить тест."""
    try:
        Path(link).symlink_to(target)
    except OSError:
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

    @pytest.mark.parametrize(
        ("target", "listed"),
        [("test1.txt", True), ("document.pdf", False)],
    )
    def test_symlink_listed_only_if_downloadable(self, test_files_dir, target, listed):
        """Проверить: ссылка в списке, только если её можно скачать."""
        try:
            (Path(test_files_dir) / "link.txt").symlink_to(target)
        except OSError:
            pytest.skip("Символические ссылки недоступны")
        client = TestClient(reload_app(files_directory=test_files_dir))

        names = [f["name"] for f in client.get("/files").json()["availableFiles"]]
        assert ("link.txt" in names) is listed
        download = client.get("/files/link.txt")
        assert (download.status_code == 200) is listed

    def test_symlink_loop_skipped_in_listing(self, test_files_dir):
        """Проверить: петля ссылок на Python 3.11 не роняет список."""
        try:
            (Path(test_files_dir) / "loop.txt").symlink_to("test1.txt")
        except OSError:
            pytest.skip("Символические ссылки недоступны")
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
        assert "access-control-allow-origin" in response.headers


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

    def test_docs_endpoint_accessible(self, client):
        """Проверить, что конечная точка /docs доступна."""
        response = client.get("/docs")
        assert response.status_code == 200
        assert "swagger" in response.text.lower() or "html" in response.text.lower()


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
            (OSError("Network path not found"), 500, "OS error"),
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
            (OSError("Network path not found"), 500, "OS error"),
        ],
    )
    def test_download_file_check_error(self, client, error, status_code, prefix):
        """Проверить ошибку ОС при проверке скачиваемого файла.

        Текст исходной ошибки (с путём к сетевой папке) в ответ не
        попадает.
        """
        with fail_stat_for("test1.txt", error):
            response = client.get("/files/test1.txt")
        assert response.status_code == status_code
        assert response.json()["detail"] == f"{prefix} when reading file"

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
        "file_id", ["test1.txt:stream", "C:x.txt", ".. ", "...", "test1.txt."]
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
        """Проверить, что директория *.txt не попадает в список."""
        (Path(test_files_dir) / "folder.txt").mkdir()
        client = TestClient(reload_app(files_directory=test_files_dir))
        response = client.get("/files")
        names = [f["name"] for f in response.json()["availableFiles"]]
        assert "folder.txt" not in names

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

    def test_directory_resolved_once(self, client, test_files_dir):
        """Проверить, что путь к директории разрешается один раз."""
        patch, calls = count_resolve(test_files_dir)
        with patch:
            for _ in range(3):
                assert client.get("/files/test1.txt").status_code == 200
        assert calls == [True]

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

    def test_stale_cache_is_refreshed(self, tmp_path):
        """Проверить: устаревший путь в кэше разрешается заново.

        Например, DFS переключил сетевую папку на другой сервер.
        """
        for name in ("a", "b"):
            (tmp_path / name).mkdir()
            (tmp_path / name / "test1.txt").write_text(name)
        link = tmp_path / "link"
        symlink_or_skip(link, tmp_path / "a")
        client = TestClient(reload_app(files_directory=str(link)))
        assert client.get("/files/test1.txt").content == b"a"

        link.unlink()
        link.symlink_to(tmp_path / "b")
        response = client.get("/files/test1.txt")
        assert response.status_code == 200
        assert response.content == b"b"

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
                with open(main_file) as f:
                    code = compile(f.read(), str(main_file), "exec")

                # Создаем пространство имен с __name__
                # как '__main__'
                namespace = {"__name__": "__main__"}
                exec(code, namespace)

                # Проверяем, что uvicorn.run был вызван
                assert mock_run.called
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

    def test_constructor_argument_does_not_override(self, settings_module):
        """Проверить, что путь нельзя передать при создании настроек."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            settings_module.Settings(files_directory=self.ATTACKER_DIR)
