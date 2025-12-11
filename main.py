"""
FastAPI приложение для просмотра списка файлов и их загрузки.
"""
import os
from pathlib import Path
from typing import List

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# Конфигурация
FILES_DIRECTORY = os.getenv("FILES_DIRECTORY", "./files")
cors_origins_str = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS = [origin.strip() for origin in cors_origins_str.split(",") if origin.strip()] or ["*"]

app = FastAPI(
    title="File Picker API",
    description="API for listing and downloading files from a configured directory",
    version="1.0.0"
)

# Включение CORS для фронтенд-приложений
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FileInfo(BaseModel):
    """Модель информации о файле."""
    name: str
    size: int
    is_file: bool


@app.get("/")
async def root():
    """Корневая конечная точка."""
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
    Получить список всех файлов в настроенной директории.
    
    Returns:
        Список объектов с информацией о файлах
    """
    files_path = Path(FILES_DIRECTORY)
    
    if not files_path.exists():
        raise HTTPException(status_code=404, detail="Files directory not found")
    
    if not files_path.is_dir():
        raise HTTPException(status_code=400, detail="Files path is not a directory")
    
    file_list = []
    try:
        for item in files_path.iterdir():
            is_file = item.is_file()
            file_list.append(
                FileInfo(
                    name=item.name,
                    size=item.stat().st_size if is_file else 0,
                    is_file=is_file
                )
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading directory: {str(e)}")
    
    return sorted(file_list, key=lambda x: x.name)


@app.get("/files/{filename}")
async def get_file(filename: str):
    """
    Скачать определенный файл из настроенной директории.
    
    Args:
        filename: Имя файла для загрузки
        
    Returns:
        Ответ с запрашиваемым файлом
    """
    # Безопасность: предотвращение обхода директорий
    # Получаем абсолютные пути и проверяем, что файл находится в разрешенной директории
    base_dir = os.path.abspath(FILES_DIRECTORY)
    requested_path = os.path.abspath(os.path.join(FILES_DIRECTORY, filename))
    
    # Проверяем, что разрешенный путь находится внутри базовой директории
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
    
    # Создаем директорию для файлов, если она не существует
    Path(FILES_DIRECTORY).mkdir(parents=True, exist_ok=True)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
