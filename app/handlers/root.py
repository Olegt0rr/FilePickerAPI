"""Обработчик корневой конечной точки."""

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
async def root() -> dict[str, str | dict[str, str]]:
    """Корневая конечная точка."""
    return {
        "message": "File Picker API",
        "endpoints": {
            "/files": "List all files",
            "/files/{filename}": "Download a specific file",
        },
    }
