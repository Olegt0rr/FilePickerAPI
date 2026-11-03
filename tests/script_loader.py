"""
Загрузка CLI-скриптов из scripts/ в тестах: scripts — не пакет.

Отдельный модуль без импорта приложения, чтобы тесты скриптов не
зависели от тестов API.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str) -> ModuleType:
    """Загрузить scripts/<name>.py как модуль."""
    spec = importlib.util.spec_from_file_location(
        f"{name}_script", ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
