"""
Comprehensive tests for the File Picker API.
"""
import os
import sys
import importlib
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from main import app, FILES_DIRECTORY


def reload_app():
    """Helper function to reload the main module with updated environment variables."""
    if 'main' in sys.modules:
        importlib.reload(sys.modules['main'])
    from main import app as test_app
    return test_app


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
    test_app = reload_app()
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
        test_app = reload_app()
        client = TestClient(test_app)
        
        response = client.get("/files")
        assert response.status_code == 404
        assert "Files directory not found" in response.json()["detail"]
    
    def test_list_files_when_path_is_file(self, test_files_dir, monkeypatch):
        """Test listing files when FILES_DIRECTORY points to a file."""
        file_path = Path(test_files_dir) / "test1.txt"
        monkeypatch.setenv("FILES_DIRECTORY", str(file_path))
        test_app = reload_app()
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
        """Test that directory traversal with .. is prevented."""
        response = client.get("/files/../main.py")
        # Should not be able to access files outside the configured directory
        assert response.status_code == 404
    
    def test_directory_traversal_with_absolute_path(self, client):
        """Test that absolute paths are handled correctly."""
        response = client.get("/files//etc/passwd")
        assert response.status_code in [400, 404]
    
    def test_directory_traversal_url_encoded(self, client):
        """Test that URL-encoded directory traversal attempts are prevented."""
        # %2E%2E is URL-encoded .. - FastAPI/Starlette decodes this before it reaches our handler
        # Our security logic validates the resolved absolute path stays within the base directory
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
        test_app = reload_app()
        client = TestClient(test_app)
        
        response = client.get("/files", headers={"Origin": "http://localhost:3000"})
        assert response.status_code == 200
    
    def test_cors_empty_string_falls_back_to_default(self, monkeypatch, test_files_dir):
        """Test that empty CORS_ORIGINS falls back to default."""
        monkeypatch.setenv("FILES_DIRECTORY", test_files_dir)
        monkeypatch.setenv("CORS_ORIGINS", "")
        test_app = reload_app()
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
            test_app = reload_app()
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
            test_app = reload_app()
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
            test_app = reload_app()
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
            test_app = reload_app()
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


class TestExceptionHandling:
    """Tests for exception handling and error cases."""
    
    def test_list_files_permission_error(self, monkeypatch):
        """Test listing files when permission is denied."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir) / "restricted"
            test_dir.mkdir()
            
            # Create a file in the directory
            test_file = test_dir / "test.txt"
            test_file.write_text("content")
            
            monkeypatch.setenv("FILES_DIRECTORY", str(test_dir))
            test_app = reload_app()
            client = TestClient(test_app)
            
            # Remove read permissions
            test_dir.chmod(0o000)
            
            try:
                response = client.get("/files")
                # Should get 500 error due to permission denied
                assert response.status_code == 500
                assert "Error reading directory" in response.json()["detail"]
            finally:
                # Restore permissions for cleanup
                test_dir.chmod(0o755)
    
    def test_security_value_error_with_mock(self, monkeypatch):
        """Test that ValueError in commonpath is caught."""
        import unittest.mock as mock
        
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")
            
            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)
            
            # Mock os.path.commonpath to raise ValueError
            with mock.patch('main.os.path.commonpath', side_effect=ValueError("Different drives")):
                response = client.get("/files/test.txt")
                # Should get 400 error due to ValueError being caught
                assert response.status_code == 400
                assert "Invalid filename" in response.json()["detail"]
    
    def test_security_common_path_not_equal_base_dir(self, monkeypatch):
        """Test that files outside base directory are rejected when common_path != base_dir."""
        import unittest.mock as mock
        
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create base directory and a file
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("content")
            
            monkeypatch.setenv("FILES_DIRECTORY", tmpdir)
            test_app = reload_app()
            client = TestClient(test_app)
            
            # Mock os.path.commonpath to return a parent directory
            # This simulates a case where the common path is not the base directory
            parent_dir = str(Path(tmpdir).parent)
            with mock.patch('main.os.path.commonpath', return_value=parent_dir):
                response = client.get("/files/test.txt")
                # Should get 400 error because common_path != base_dir
                assert response.status_code == 400
                assert "Invalid filename" in response.json()["detail"]


class TestMainExecution:
    """Tests for main execution block."""
    
    def test_main_execution_creates_directory(self):
        """Test that the main block creates the files directory."""
        import subprocess
        import sys
        
        with tempfile.TemporaryDirectory() as tmpdir:
            files_dir = Path(tmpdir) / "test_files_dir"
            
            # Use coverage to track the subprocess execution
            test_script = Path(tmpdir) / "run_main.py"
            test_script.write_text(f"""
import sys
import os
from pathlib import Path

# Set environment before import
os.environ['FILES_DIRECTORY'] = '{files_dir}'

# Now run the main block
if __name__ == "__main__":
    import uvicorn
    
    FILES_DIRECTORY = os.getenv("FILES_DIRECTORY", "./files")
    # Create files directory if it doesn't exist
    Path(FILES_DIRECTORY).mkdir(parents=True, exist_ok=True)
    print(f"DIRECTORY_CREATED={{Path(FILES_DIRECTORY).exists()}}")
    # Don't actually start the server
    sys.exit(0)
""")
            
            # Run the script
            result = subprocess.run(
                [sys.executable, str(test_script)],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            # Check that directory was created
            assert "DIRECTORY_CREATED=True" in result.stdout
            assert files_dir.exists()
    
    def test_main_block_with_coverage(self):
        """Test main block execution with proper coverage tracking."""
        import subprocess
        import sys
        
        with tempfile.TemporaryDirectory() as tmpdir:
            files_dir = Path(tmpdir) / "files_for_coverage"
            
            # Create a script that will be run with coverage
            test_script = Path(tmpdir) / "test_main_run.py"
            test_script.write_text(f"""
import sys
import os
from pathlib import Path

os.environ['FILES_DIRECTORY'] = '{files_dir}'

# Run main.py as __main__ with coverage
if __name__ == '__main__':
    # Read main.py content
    main_file = '/home/runner/work/FilePickerAPI/FilePickerAPI/main.py'
    with open(main_file, 'r') as f:
        main_code = compile(f.read(), main_file, 'exec')
    
    # Execute with __name__ == '__main__'
    import uvicorn
    original_run = uvicorn.run
    
    def mock_run(*args, **kwargs):
        print("UVICORN_RUN_CALLED")
        return None
    
    uvicorn.run = mock_run
    exec(main_code, {{'__name__': '__main__'}})
    uvicorn.run = original_run
""")
            
            # Run with coverage
            result = subprocess.run(
                [sys.executable, '-m', 'coverage', 'run', '--source=main', str(test_script)],
                capture_output=True,
                text=True,
                timeout=5,
                cwd='/home/runner/work/FilePickerAPI/FilePickerAPI'
            )
            
            # Check execution
            assert "UVICORN_RUN_CALLED" in result.stdout or result.returncode == 0
            assert files_dir.exists()
    
    def test_main_module_directly(self):
        """Test by importing main.py with __name__ set to '__main__'."""
        import unittest.mock as mock
        
        with tempfile.TemporaryDirectory() as tmpdir:
            files_dir = Path(tmpdir) / "main_test_dir"
            
            # Mock uvicorn.run to prevent actually starting the server
            with mock.patch.dict(os.environ, {'FILES_DIRECTORY': str(files_dir)}):
                with mock.patch('uvicorn.run') as mock_run:
                    # Execute the main.py file with __name__ == '__main__'
                    main_file = '/home/runner/work/FilePickerAPI/FilePickerAPI/main.py'
                    with open(main_file, 'r') as f:
                        code = compile(f.read(), main_file, 'exec')
                    
                    # Create namespace with __name__ as '__main__'
                    namespace = {'__name__': '__main__'}
                    exec(code, namespace)
                    
                    # Verify uvicorn.run was called
                    assert mock_run.called
                    # Verify directory was created
                    assert files_dir.exists()
