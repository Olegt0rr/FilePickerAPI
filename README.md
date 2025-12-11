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
pip install -r requirements.txt
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

## Development

### API Documentation

Once the server is running, you can access:
- Interactive API docs (Swagger UI): `http://localhost:8000/docs`
- Alternative API docs (ReDoc): `http://localhost:8000/redoc`

### Building the executable locally

```bash
pip install pyinstaller
pyinstaller --onefile --name FilePickerAPI main.py
```

The executable will be created in the `dist/` directory.

## GitHub Actions

The repository includes a GitHub Action workflow that automatically builds a Windows executable on:
- Push to main/master branch
- Pull requests to main/master branch
- Manual workflow dispatch

The built executable is uploaded as an artifact and retained for 30 days.

## Security

- Directory traversal protection: filenames containing `..`, `/`, or `\` are rejected
- Only files (not directories) can be downloaded
- CORS is enabled by default (can be configured in `main.py` if needed)

## License

MIT