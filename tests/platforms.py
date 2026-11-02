"""Пометки тестов, которые имеют смысл только на одной ОС."""

import sys

import pytest

# Проверки, которые имеют смысл только на Windows: на Linux обратный
# слэш — обычный символ имени, время создания файла недоступно, а WinAPI
# и права Windows есть только там
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Только Windows")
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Только Linux"
)
