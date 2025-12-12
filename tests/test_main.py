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
    # Перезагружаем обработчики, чтобы они получили новые
    # переменные окружения
    if "app.handlers.files" in sys.modules:
        importlib.reload(sys.modules["app.handlers.files"])
    if "app.handlers.root" in sys.modules:
        importlib.reload(sys.modules["app.handlers.root"])
    if "app.main" in sys.modules:
        importlib.reload(sys.modules["app.main"])
    from app.main import app as test_app

    return test_app


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


class TestRootEndpoint:
    """Тесты для корневой конечной точки."""

    def test_root_returns_api_info(self, client):
        """Проверить, что корневая конечная точка возвращает
        информацию об API.
        """
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "endpoints" in data
        assert data["message"] == "File Picker API"
        assert "/files" in data["endpoints"]
        assert "/files/{filename}" in data["endpoints"]


class TestListFilesEndpoint:
    """Тесты для конечной точки списка файлов."""

    def test_list_files_success(self, client):
        """Проверить, что список файлов возвращает корректную
        информацию о файлах.
        """
        response = client.get("/files")
        assert response.status_code == 200
        data = response.json()

        # Должен вернуть список
        assert isinstance(data, list)
        assert len(data) == 4  # 3 файла + 1 поддиректория

        # Проверяем, что файлы отсортированы по имени
        names = [item["name"] for item in data]
        assert names == sorted(names)

        # Проверяем структуру файла
        for item in data:
            assert "name" in item
            assert "size" in item
            assert "is_file" in item

    def test_list_files_contains_correct_metadata(self, client):
        """Проверить, что метаданные файла корректны."""
        response = client.get("/files")
        assert response.status_code == 200
        data = response.json()

        # Находим test1.txt
        test1 = next((item for item in data if item["name"] == "test1.txt"), None)
        assert test1 is not None
        assert test1["is_file"] is True
        assert test1["size"] == 14  # длина "Test content 1"

        # Находим поддиректорию
        subdir = next((item for item in data if item["name"] == "subdir"), None)
        assert subdir is not None
        assert subdir["is_file"] is False
        assert subdir["size"] == 0

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
            assert response.json() == []

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
            assert len(data) == 100
            # Проверяем, что файлы отсортированы
            names = [item["name"] for item in data]
            assert names == sorted(names)


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
                # Должны получить ошибку 500 из-за отказа в доступе
                assert response.status_code == 500
                assert "Error reading directory" in response.json()["detail"]
            finally:
                # Восстанавливаем права для очистки
                test_dir.chmod(0o755)

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
                # Перезагружаем модуль для применения новых
                # переменных окружения
                if "app.main" in sys.modules:
                    importlib.reload(sys.modules["app.main"])

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
