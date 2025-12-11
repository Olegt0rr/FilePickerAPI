"""
Comprehensive tests for the File Picker API.
"""
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app, FILES_DIRECTORY


@pytest.fixture
def test_files_dir():
    """Create a temporary directory with test files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create test files
        test_file_1 = Path(tmpdir) / "test1.txt"
        test_file_1.write_text("Test content 1")
        
        test_file_2 = Path(tmpdir) / "test2.txt"
        test_file_2.write_text("Test content 2 with more data")
        
        test_file_3 = Path(tmpdir) / "document.pdf"
        test_file_3.write_bytes(b"PDF content here")
        
        # Create a subdirectory
        subdir = Path(tmpdir) / "subdir"
        subdir.mkdir()
        
        yield tmpdir


@pytest.fixture
def client(test_files_dir, monkeypatch):
    """Create a test client with a temporary files directory."""
    # Set environment variable before importing
    monkeypatch.setenv("FILES_DIRECTORY", test_files_dir)
    monkeypatch.setenv("CORS_ORIGINS", "*")
    
    # Force reload of the main module to pick up new env vars
    import sys
    import importlib
    if 'main' in sys.modules:
        importlib.reload(sys.modules['main'])
    
    from main import app as test_app
    return TestClient(test_app)


class TestRootEndpoint:
    """Tests for the root endpoint."""
    
    def test_root_returns_api_info(self, client):
        """Test that root endpoint returns API information."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "message" in data
        assert "endpoints" in data
        assert data["message"] == "File Picker API"
        assert "/files" in data["endpoints"]
        assert "/files/{filename}" in data["endpoints"]


class TestListFilesEndpoint:
    """Tests for the list files endpoint."""
    
    def test_list_files_success(self, client):
        """Test that list files returns correct file information."""
        response = client.get("/files")
        assert response.status_code == 200
        data = response.json()
        
        # Should return a list
        assert isinstance(data, list)
        assert len(data) == 4  # 3 files + 1 subdirectory
        
        # Check that files are sorted by name
        names = [item["name"] for item in data]
        assert names == sorted(names)
        
        # Check file structure
        for item in data:
            assert "name" in item
            assert "size" in item
            assert "is_file" in item
    
    def test_list_files_contains_correct_metadata(self, client):
        """Test that file metadata is correct."""
        response = client.get("/files")
        assert response.status_code == 200
        data = response.json()
        
        # Find test1.txt
        test1 = next((item for item in data if item["name"] == "test1.txt"), None)
        assert test1 is not None
        assert test1["is_file"] is True
        assert test1["size"] == 14  # "Test content 1" length
        
        # Find subdirectory
        subdir = next((item for item in data if item["name"] == "subdir"), None)
        assert subdir is not None
        assert subdir["is_file"] is False
        assert subdir["size"] == 0
    
    def test_list_files_nonexistent_directory(self, monkeypatch):
        """Test listing files when directory doesn't exist."""
        monkeypatch.setenv("FILES_DIRECTORY", "/nonexistent/path")
        
        import sys
        import importlib
        if 'main' in sys.modules:
            importlib.reload(sys.modules['main'])
        
        from main import app as test_app
        client = TestClient(test_app)
        
        response = client.get("/files")
        assert response.status_code == 404
        assert "Files directory not found" in response.json()["detail"]
    
    def test_list_files_when_path_is_file(self, test_files_dir, monkeypatch):
        """Test listing files when FILES_DIRECTORY points to a file."""
        file_path = Path(test_files_dir) / "test1.txt"
        monkeypatch.setenv("FILES_DIRECTORY", str(file_path))
        
        import sys
        import importlib
        if 'main' in sys.modules:
            importlib.reload(sys.modules['main'])
        
        from main import app as test_app
        client = TestClient(test_app)
        
        response = client.get("/files")
        assert response.status_code == 400
        assert "Files path is not a directory" in response.json()["detail"]


class TestDownloadFileEndpoint:
    """Tests for the download file endpoint."""
    
    def test_download_file_success(self, client):
        """Test successful file download."""
        response = client.get("/files/test1.txt")
        assert response.status_code == 200
        assert response.content == b"Test content 1"
        assert response.headers["content-type"] == "application/octet-stream"
        assert 'attachment; filename="test1.txt"' in response.headers.get("content-disposition", "")
    
    def test_download_different_file(self, client):
        """Test downloading a different file."""
        response = client.get("/files/test2.txt")
        assert response.status_code == 200
        assert response.content == b"Test content 2 with more data"
    
    def test_download_binary_file(self, client):
        """Test downloading a binary file."""
        response = client.get("/files/document.pdf")
        assert response.status_code == 200
        assert response.content == b"PDF content here"
    
    def test_download_nonexistent_file(self, client):
        """Test downloading a file that doesn't exist."""
        response = client.get("/files/nonexistent.txt")
        assert response.status_code == 404
        assert "File not found" in response.json()["detail"]
    
    def test_download_directory(self, client):
        """Test that directories cannot be downloaded."""
        response = client.get("/files/subdir")
        assert response.status_code == 400
        assert "Path is not a file" in response.json()["detail"]


class TestSecurityDirectoryTraversal:
    """Tests for directory traversal security."""
    
    def test_directory_traversal_with_dotdot(self, client):
        """Test that .. is blocked."""
        response = client.get("/files/../main.py")
        # FastAPI routing returns 404 for paths with ..
        assert response.status_code == 404
    
    def test_directory_traversal_with_absolute_path(self, client):
        """Test that absolute paths are handled correctly."""
        response = client.get("/files//etc/passwd")
        assert response.status_code in [400, 404]
    
    def test_directory_traversal_url_encoded(self, client):
        """Test that URL-encoded directory traversal is blocked."""
        # %2E%2E is URL-encoded ..
        response = client.get("/files/%2E%2E%2Fmain.py")
        # Should not be able to access files outside the directory
        assert response.status_code == 404
    
    def test_directory_traversal_complex_path(self, client):
        """Test complex directory traversal attempts."""
        response = client.get("/files/subdir/../../main.py")
        # Should not be able to access files outside the directory
        assert response.status_code == 404
    
    def test_valid_filename_works(self, client):
        """Test that valid filenames still work after security checks."""
        response = client.get("/files/test1.txt")
        assert response.status_code == 200


class TestCORSConfiguration:
    """Tests for CORS configuration."""
    
    def test_cors_headers_present(self, client):
        """Test that CORS headers are present."""
        response = client.get("/files", headers={"Origin": "http://example.com"})
        assert "access-control-allow-origin" in response.headers
    
    def test_cors_allows_all_origins_by_default(self, client):
        """Test that CORS allows all origins by default."""
        response = client.get("/files", headers={"Origin": "http://example.com"})
        assert response.headers["access-control-allow-origin"] == "*"
    
    def test_cors_custom_origins(self, monkeypatch, test_files_dir):
        """Test that CORS can be configured with specific origins."""
        monkeypatch.setenv("FILES_DIRECTORY", test_files_dir)
        monkeypatch.setenv("CORS_ORIGINS", "http://localhost:3000,https://example.com")
        
        import sys
        import importlib
        if 'main' in sys.modules:
            importlib.reload(sys.modules['main'])
        
        from main import app as test_app
        client = TestClient(test_app)
        
        response = client.get("/files", headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 200
    
    def test_cors_empty_string_falls_back_to_default(self, monkeypatch, test_files_dir):
        """Test that empty CORS_ORIGINS falls back to default."""
        monkeypatch.setenv("FILES_DIRECTORY", test_files_dir)
        monkeypatch.setenv("CORS_ORIGINS", "")
        
        import sys
        import importlib
        if 'main' in sys.modules:
            importlib.reload(sys.modules['main'])
        
        from main import app as test_app
        client = TestClient(test_app)
        
        response = client.get("/files", headers={"Origin": "http://example.com"})
        assert response.status_code == 200
        # Should fall back to allowing all origins
        assert "access-control-allow-origin" in response.headers


class TestEdgeCases:
    """Tests for edge cases and error handling."""
    
    def test_empty_directory(self, monkeypatch):
        """Test listing files in an empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            
            import sys
            import importlib
            if 'main' in sys.modules:
                importlib.reload(sys.modules['main'])
            
            from main import app as test_app
            client = TestClient(test_app)
            
            response = client.get("/files")
            assert response.status_code == 200
            assert response.json() == []
    
    def test_filename_with_spaces(self, monkeypatch):
        """Test downloading a file with spaces in the name."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "file with spaces.txt"
            test_file.write_text("Content")
            
            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            
            import sys
            import importlib
            if 'main' in sys.modules:
                importlib.reload(sys.modules['main'])
            
            from main import app as test_app
            client = TestClient(test_app)
            
            response = client.get("/files/file with spaces.txt")
            assert response.status_code == 200
            assert response.content == b"Content"
    
    def test_filename_with_special_characters(self, monkeypatch):
        """Test downloading a file with special characters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "file-name_123.txt"
            test_file.write_text("Special content")
            
            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            
            import sys
            import importlib
            if 'main' in sys.modules:
                importlib.reload(sys.modules['main'])
            
            from main import app as test_app
            client = TestClient(test_app)
            
            response = client.get("/files/file-name_123.txt")
            assert response.status_code == 200
            assert response.content == b"Special content"
    
    def test_large_file_listing(self, monkeypatch):
        """Test listing a directory with many files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create 100 test files
            for i in range(100):
                test_file = Path(tmpdir) / f"file_{i:03d}.txt"
                test_file.write_text(f"Content {i}")
            
            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            
            import sys
            import importlib
            if 'main' in sys.modules:
                importlib.reload(sys.modules['main'])
            
            from main import app as test_app
            client = TestClient(test_app)
            
            response = client.get("/files")
            assert response.status_code == 200
            data = response.json()
            assert len(data) == 100
            # Check that files are sorted
            names = [item["name"] for item in data]
            assert names == sorted(names)


class TestAPIDocumentation:
    """Tests for API documentation endpoints."""
    
    def test_openapi_schema_accessible(self, client):
        """Test that OpenAPI schema is accessible."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert "openapi" in schema
        assert "info" in schema
        assert schema["info"]["title"] == "File Picker API"
    
    def test_docs_endpoint_accessible(self, client):
        """Test that /docs endpoint is accessible."""
        response = client.get("/docs")
        assert response.status_code == 200
        assert "swagger" in response.text.lower() or "html" in response.text.lower()
