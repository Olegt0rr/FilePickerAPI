# FilePickerAPI

FastAPI-based web server for listing and downloading files from a configured directory.

## Features

- 🚀 Fast and lightweight API built with FastAPI
- 📁 List files from a configured directory
- ⬇️ Download specific files
- 🔒 Security: prevents directory traversal attacks
- 🌐 CORS enabled for frontend integration
- 🪟 Automatic Windows executable build via GitHub Actions

## API Endpoints

### `GET /`
Root endpoint with API information.

### `GET /files`
List all files in the configured directory.

**Response:**
```json
[
  {
    "name": "example.txt",
    "size": 1024,
    "is_file": true
  }
]
```

### `GET /files/{filename}`
Download a specific file.

**Parameters:**
- `filename` - Name of the file to download

**Response:** File download (application/octet-stream)

## Installation

### Running from source

1. Clone the repository:
```bash
git clone https://github.com/Olegt0rr/FilePickerAPI.git
cd FilePickerAPI
```

2. Install dependencies:
```bash
pip install fastapi==0.104.1 uvicorn[standard]==0.24.0 pydantic==2.5.0
```

Or using the project configuration:
```bash
pip install -e .
```

3. Run the application:
```bash
python main.py
```

The API will be available at `http://localhost:8000`

### Using the Windows executable

1. Download the latest `FilePickerAPI.exe` from the [GitHub Actions artifacts](../../actions)
2. Run the executable:
```bash
FilePickerAPI.exe
```

## Configuration

### Files Directory

By default, the application serves files from the `./files` directory. You can change this by setting the `FILES_DIRECTORY` environment variable:

**Linux/Mac:**
```bash
export FILES_DIRECTORY="/path/to/your/files"
python main.py
```

**Windows:**
```cmd
set FILES_DIRECTORY=C:\path\to\your\files
FilePickerAPI.exe
```

### CORS Origins

By default, CORS is enabled for all origins (`*`). For production use, you should restrict this to specific domains by setting the `CORS_ORIGINS` environment variable with comma-separated origins:

**Linux/Mac:**
```bash
export CORS_ORIGINS="http://localhost:3000,https://yourdomain.com"
python main.py
```

**Windows:**
```cmd
set CORS_ORIGINS=http://localhost:3000,https://yourdomain.com
FilePickerAPI.exe
```

## Development

### API Documentation

Once the server is running, you can access:
- Interactive API docs (Swagger UI): `http://localhost:8000/docs`
- Alternative API docs (ReDoc): `http://localhost:8000/redoc`

### Running Tests

The project includes comprehensive test coverage with **100% code coverage**. To run tests:

```bash
# Install development dependencies
pip install pytest==7.4.3 httpx==0.25.2 pytest-cov==4.1.0 ruff==0.1.9

# Run linting checks
ruff check main.py test_main.py
ruff format --check main.py test_main.py

# Run tests
pytest

# Run tests with coverage report (already included in pytest.ini config)
```

All configuration is managed through `pyproject.toml`:
- Dependencies and dev dependencies
- pytest configuration with coverage settings
- ruff linter configuration with "ALL" rules

The test suite includes 29 tests covering:
- All API endpoints (root, list files, download file)
- Security features (directory traversal protection)
- CORS configuration
- Exception handling (permission errors, security exceptions)
- Error handling and edge cases
- Main execution block
- File metadata, sorting, and special characters

### Building the executable locally

```bash
pip install pyinstaller
pyinstaller --onefile --name FilePickerAPI main.py
```

The executable will be created in the `dist/` directory.

## GitHub Actions

The repository includes a GitHub Action workflow that automatically:
- Runs the test suite on Ubuntu
- Builds a Windows executable (only if tests pass)
- Uploads test coverage reports
- Uploads the Windows executable as an artifact

Workflow triggers:
- Push to main/master branch
- Pull requests to main/master branch
- Manual workflow dispatch

Artifacts:
- Windows executable: retained for 30 days
- Coverage report: retained for 7 days

## Security

- Directory traversal protection: all file paths are resolved to absolute paths and validated to ensure they remain within the configured directory
- Uses `os.path.commonpath()` to verify the requested file path doesn't escape the base directory
- Only files (not directories) can be downloaded
- CORS origins can be configured via environment variable for production use (defaults to allowing all origins for development)

## License

MIT