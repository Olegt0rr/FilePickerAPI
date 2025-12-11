"""
FastAPI application for file listing and downloading.
"""
import os
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# Configuration
FILES_DIRECTORY = os.getenv("FILES_DIRECTORY", "./files")
cors_origins_str = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS = [origin.strip() for origin in cors_origins_str.split(",") if origin.strip()]

app = FastAPI(
    title="File Picker API",
    description="API for listing and downloading files from a configured directory",
    version="1.0.0"
)

# Enable CORS for frontend applications
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FileInfo(BaseModel):
    """File information model."""
    name: str
    size: int
    is_file: bool


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "File Picker API",
        "endpoints": {
            "/files": "List all files",
            "/files/{filename}": "Download a specific file"
        }
    }


@app.get("/files", response_model=List[FileInfo])
async def list_files():
    """
    List all files in the configured directory.
    
    Returns:
        List of file information objects
    """
    files_path = Path(FILES_DIRECTORY)
    
    if not files_path.exists():
        raise HTTPException(status_code=404, detail="Files directory not found")
    
    if not files_path.is_dir():
        raise HTTPException(status_code=400, detail="Files path is not a directory")
    
    file_list = []
    try:
        for item in files_path.iterdir():
            file_list.append(
                FileInfo(
                    name=item.name,
                    size=item.stat().st_size if item.is_file() else 0,
                    is_file=item.is_file()
                )
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading directory: {str(e)}")
    
    return sorted(file_list, key=lambda x: x.name)


@app.get("/files/{filename}")
async def get_file(filename: str):
    """
    Download a specific file from the configured directory.
    
    Args:
        filename: Name of the file to download
        
    Returns:
        File response with the requested file
    """
    # Security: prevent directory traversal
    # Get absolute paths and ensure the file is within the allowed directory
    base_dir = os.path.abspath(FILES_DIRECTORY)
    requested_path = os.path.abspath(os.path.join(FILES_DIRECTORY, filename))
    
    # Verify the resolved path is within the base directory
    try:
        common_path = os.path.commonpath([base_dir, requested_path])
        if common_path != base_dir:
            raise HTTPException(status_code=400, detail="Invalid filename")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    file_path = Path(requested_path)
    
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")
    
    return FileResponse(
        path=file_path,
        filename=file_path.name,
        media_type="application/octet-stream"
    )


if __name__ == "__main__":
    import uvicorn
    
    # Create files directory if it doesn't exist
    Path(FILES_DIRECTORY).mkdir(parents=True, exist_ok=True)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
