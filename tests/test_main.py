"""
Комплексные тесты для File Picker API.
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def reload_app():
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
    from app import app as test_app

    return test_app


def assert_sorted_by_created_at(files: list[dict]) -> None:
    """Проверить, что файлы отсортированы по дате создания
    (убывание).
    """
    if len(files) > 1:
        created_times = [item["createdAt"] for item in files]
        assert created_times == sorted(created_times, reverse=True)


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
def client(test_files_dir, monkeypatch):
    """Создать тестовый клиент с временной директорией файлов."""
    # Устанавливаем переменную окружения перед импортом
    monkeypatch.setenv("FILES_DIRECTORY", test_files_dir)
    monkeypatch.setenv("CORS_ORIGINS", "*")

    # Принудительно перезагружаем главный модуль для применения
    # новых переменных окружения
    test_app = reload_app()
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
        assert "notAvailableFiles" in data
        assert isinstance(data["availableFiles"], list)
        assert isinstance(data["notAvailableFiles"], list)

        # Все файлы из обоих списков (директории игнорируются)
        all_files = data["availableFiles"] + data["notAvailableFiles"]
        assert len(all_files) == 3  # 3 файла (директория subdir игнорируется)

        # Только .txt файлы в availableFiles
        assert len(data["availableFiles"]) == 2  # test1.txt и test2.txt
        # document.pdf в notAvailableFiles
        assert len(data["notAvailableFiles"]) == 1

        # Проверяем, что файлы отсортированы по дате создания
        # (новые первыми)
        assert_sorted_by_created_at(data["availableFiles"])
        assert_sorted_by_created_at(data["notAvailableFiles"])

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
        all_files = data["availableFiles"] + data["notAvailableFiles"]

        # Находим test1.txt (должен быть в availableFiles)
        test1 = next((item for item in all_files if item["name"] == "test1.txt"), None)
        assert test1 is not None
        assert test1["size"] == 14  # длина "Test content 1"
        assert test1["id"] == "test1.txt"
        assert "createdAt" in test1

        # Находим document.pdf (должен быть в notAvailableFiles)
        pdf_file = next(
            (item for item in all_files if item["name"] == "document.pdf"), None
        )
        assert pdf_file is not None
        assert pdf_file["size"] == 16  # длина b"PDF content here"
        assert pdf_file["id"] == "document.pdf"

        # Директории не должны присутствовать в ответе
        subdir = next((item for item in all_files if item["name"] == "subdir"), None)
        assert subdir is None

    def test_list_files_nonexistent_directory(self, monkeypatch):
        """Проверить вывод списка файлов, когда директория
        не существует.
        """
        monkeypatch.setenv("FILES_DIRECTORY", "/nonexistent/path")
        test_app = reload_app()
        client = TestClient(test_app)

        response = client.get("/files")
        assert response.status_code == 404
        assert "Files directory not found" in response.json()["detail"]

    def test_list_files_when_path_is_file(self, test_files_dir, monkeypatch):
        """Проверить вывод списка файлов, когда FILES_DIRECTORY
        указывает на файл.
        """
        file_path = Path(test_files_dir) / "test1.txt"
        monkeypatch.setenv("FILES_DIRECTORY", str(file_path))
        test_app = reload_app()
        client = TestClient(test_app)

        response = client.get("/files")
        assert response.status_code == 400
        assert "Files path is not a directory" in response.json()["detail"]

    def test_list_files_filters_by_size_and_format(self, monkeypatch):
        """Проверить, что файлы фильтруются по размеру и формату."""
        from app.handlers.files import MAX_AVAILABLE_FILE_SIZE

        with tempfile.TemporaryDirectory() as tmpdir:
            # Создаем .txt файл меньше 10 МБ
            # (должен быть в availableFiles)
            small_txt = Path(tmpdir) / "small.txt"
            small_txt.write_bytes(b"x" * 1024)

            # Создаем .pdf файл меньше 10 МБ
            # (не .txt - в notAvailableFiles)
            small_pdf = Path(tmpdir) / "small.pdf"
            small_pdf.write_bytes(b"x" * 1024)

            # Создаем .txt файл ровно 10 МБ
            # (большой - в notAvailableFiles)
            exact_10mb = Path(tmpdir) / "exact_10mb.txt"
            exact_10mb.write_bytes(b"x" * MAX_AVAILABLE_FILE_SIZE)

            # Создаем .txt файл больше 10 МБ
            # (большой - в notAvailableFiles)
            large_txt = Path(tmpdir) / "large.txt"
            large_txt.write_bytes(b"x" * int(MAX_AVAILABLE_FILE_SIZE * 1.5))

            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)

            response = client.get("/files")
            assert response.status_code == 200
            data = response.json()

            # Только маленький .txt файл в availableFiles
            available_names = [f["name"] for f in data["availableFiles"]]
            assert "small.txt" in available_names

            # Все остальные в notAvailableFiles
            not_available_names = [f["name"] for f in data["notAvailableFiles"]]
            assert "small.pdf" in not_available_names  # не .txt
            assert "exact_10mb.txt" in not_available_names  # большой
            assert "large.txt" in not_available_names  # большой

            # Проверяем количество
            assert len(data["availableFiles"]) == 1
            assert len(data["notAvailableFiles"]) == 3

    def test_list_files_txt_only_in_available(self, client):
        """Проверить, что только .txt файлы в availableFiles."""
        response = client.get("/files")
        assert response.status_code == 200
        data = response.json()

        # Только .txt файлы в availableFiles
        assert len(data["availableFiles"]) == 2  # test1.txt и test2.txt
        for file in data["availableFiles"]:
            assert file["name"].lower().endswith(".txt")

        # document.pdf в notAvailableFiles (subdir игнорируется)
        assert len(data["notAvailableFiles"]) == 1
        not_available_names = [f["name"] for f in data["notAvailableFiles"]]
        assert "document.pdf" in not_available_names
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
        """Проверить загрузку бинарного файла."""
        response = client.get("/files/document.pdf")
        assert response.status_code == 200
        assert response.content == b"PDF content here"

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


class TestSecurityDirectoryTraversal:
    """Тесты для безопасности обхода директорий."""

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
        monkeypatch.setenv("FILES_DIRECTORY", test_files_dir)
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,https://example.com")
        test_app = reload_app()
        client = TestClient(test_app)

        response = client.get("/files", headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 200

    def test_cors_empty_string_falls_back_to_default(self, monkeypatch, test_files_dir):
        """Проверить, что пустая строка CORS_ORIGINS возвращается
        к значению по умолчанию.
        """
        monkeypatch.setenv("FILES_DIRECTORY", test_files_dir)
        monkeypatch.setenv("CORS_ORIGINS", "")
        test_app = reload_app()
        client = TestClient(test_app)

        response = client.get("/files", headers={"Origin": "http://example.com"})
        assert response.status_code == 200
        # Должно вернуться к разрешению всех источников
        assert "access-control-allow-origin" in response.headers


class TestEdgeCases:
    """Тесты для граничных случаев и обработки ошибок."""

    def test_empty_directory(self, monkeypatch):
        """Проверить вывод списка файлов в пустой директории."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)

            response = client.get("/files")
            assert response.status_code == 200
            data = response.json()
            assert data == {"availableFiles": [], "notAvailableFiles": []}

    def test_filename_with_spaces(self, monkeypatch):
        """Проверить загрузку файла с пробелами в имени."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "file with spaces.txt"
            test_file.write_text("Content")

            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)

            response = client.get("/files/file with spaces.txt")
            assert response.status_code == 200
            assert response.content == b"Content"

    def test_filename_with_special_characters(self, monkeypatch):
        """Проверить загрузку файла со специальными символами."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "file-name_123.txt"
            test_file.write_text("Special content")

            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)

            response = client.get("/files/file-name_123.txt")
            assert response.status_code == 200
            assert response.content == b"Special content"

    def test_large_file_listing(self, monkeypatch):
        """Проверить вывод списка директории с большим количеством
        файлов.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Создаем 100 тестовых файлов
            for i in range(100):
                test_file = Path(tmpdir) / f"file_{i:03d}.txt"
                test_file.write_text(f"Content {i}")

            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)

            response = client.get("/files")
            assert response.status_code == 200
            data = response.json()
            all_files = data["availableFiles"] + data["notAvailableFiles"]
            assert len(all_files) == 100
            # Проверяем, что файлы отсортированы по дате создания
            # (новые первыми)
            assert_sorted_by_created_at(data["availableFiles"])
            assert_sorted_by_created_at(data["notAvailableFiles"])


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

    def test_list_files_permission_error(self, monkeypatch):
        """Проверить вывод списка файлов, когда в доступе отказано."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir) / "restricted"
            test_dir.mkdir()

            # Создаем файл в директории
            test_file = test_dir / "test.txt"
            test_file.write_text("content")

            monkeypatch.setenv("FILES_DIRECTORY", str(test_dir))
            test_app = reload_app()
            client = TestClient(test_app)

            # Убираем права на чтение
            test_dir.chmod(0o000)

            try:
                response = client.get("/files")
                # Должны получить ошибку 403 из-за отказа в доступе
                assert response.status_code == 403
                assert "Permission denied" in response.json()["detail"]
            finally:
                # Восстанавливаем права для очистки
                test_dir.chmod(0o755)

    def test_list_files_oserror(self, monkeypatch):
        """Проверить обработку OSError при чтении директории."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")

            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)

            # Мокируем iterdir для возбуждения OSError
            with mock.patch(
                "pathlib.Path.iterdir",
                side_effect=OSError("Disk error"),
            ):
                response = client.get("/files")
                # Должны получить ошибку 500 из-за OSError
                assert response.status_code == 500
                assert "OS error" in response.json()["detail"]

    def test_list_files_unexpected_error(self, monkeypatch):
        """Проверить обработку неожиданных исключений."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")

            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)

            # Мокируем iterdir для возбуждения произвольного Exception
            with mock.patch(
                "pathlib.Path.iterdir",
                side_effect=RuntimeError("Unexpected error"),
            ):
                response = client.get("/files")
                # Должны получить ошибку 500 из-за неожиданного
                # исключения
                assert response.status_code == 500
                assert "Unexpected error" in response.json()["detail"]

    def test_security_value_error_with_mock(self, monkeypatch):
        """Проверить, что ValueError в commonpath перехватывается."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")

            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)

            # Мокируем os.path.commonpath для возбуждения ValueError
            with mock.patch(
                "app.handlers.files.os.path.commonpath",
                side_effect=ValueError("Different drives"),
            ):
                response = client.get("/files/test.txt")
                # Должны получить ошибку 400 из-за ValueError
                assert response.status_code == 400
                assert "Invalid filename" in response.json()["detail"]

    def test_security_common_path_not_equal_base_dir(self, monkeypatch):
        """Проверить отклонение файлов вне базовой директории."""
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmpdir:
            # Создаем базовую директорию и файл
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")

            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)

            # Мокируем os.path.commonpath для возврата
            # родительской директории
            parent_dir = str(Path(tmpdir).parent)
            with mock.patch(
                "app.handlers.files.os.path.commonpath", return_value=parent_dir
            ):
                response = client.get("/files/test.txt")
                # Должны получить ошибку 400
                assert response.status_code == 400
                assert "Invalid filename" in response.json()["detail"]


class TestMainExecution:
    """Тесты для блока выполнения main."""

    def test_main_module_directly(self):
        """Проверить выполнение app/__main__.py с __name__,
        установленным в '__main__'.
        """
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmpdir:
            files_dir = Path(tmpdir) / "main_test_dir"

            # Мокируем uvicorn.run, чтобы предотвратить
            # фактический запуск сервера
            with mock.patch.dict(os.environ, {"FILES_DIRECTORY": str(files_dir)}):
                # Перезагружаем модули для применения новых
                # переменных окружения
                if "app.settings" in sys.modules:
                    # Очищаем кэш lru_cache перед перезагрузкой
                    from app.settings import get_settings

                    get_settings.cache_clear()
                    importlib.reload(sys.modules["app.settings"])
                if "app" in sys.modules:
                    importlib.reload(sys.modules["app"])

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
