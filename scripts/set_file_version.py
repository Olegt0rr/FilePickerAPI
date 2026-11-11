"""Другая версия exe и DLL в ресурсах для проверки обновления MSI.

Использование:
    python scripts/set_file_version.py <файл> <X.Y.Z>
        Записать X.Y.Z.0 в FILEVERSION и PRODUCTVERSION ресурса
        VERSIONINFO файла (VS_FIXEDFILEINFO).

build-exe.yml правит так копию папки сборки до пробной подписи: правка
ресурсов сделала бы подпись недействительной. Windows Installer и WiX
берут версию файла из VS_FIXEDFILEINFO, строковая таблица
(StringFileInfo, VersionInfo.FileVersion в PowerShell) не меняется.
Версия приложения в app._version вшита при компиляции и тоже остаётся
прежней.
"""

import re
import struct
import sys

# VS_VERSIONINFO: wLength, wValueLength, wType, ключ "VS_VERSION_INFO"
# в UTF-16 с нулём, выравнивание до 4 байт и VS_FIXEDFILEINFO
KEY = "VS_VERSION_INFO\0".encode("utf-16-le")
KEY_OFFSET = 6
FIXED_OFFSET = 40
SIGNATURE = struct.pack("<I", 0xFEEF04BD)
# После dwSignature и dwStrucVersion: dwFileVersionMS, dwFileVersionLS,
# dwProductVersionMS, dwProductVersionLS
VERSIONS_OFFSET = FIXED_OFFSET + 8
VERSIONS_SIZE = 16
VERSION_PATTERN = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")
MAX_PART = 0xFFFF
RT_VERSION = 16
VS_VERSION_INFO = 1


def parse_version(version: str) -> tuple[int, int, int, int]:
    """Разобрать X.Y.Z в четыре части версии файла."""
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        msg = f"Версия {version!r} не вида X.Y.Z"
        raise ValueError(msg)
    numbers = [int(part) for part in match.groups()]
    if any(number > MAX_PART for number in numbers):
        msg = f"Часть версии {version!r} больше {MAX_PART}"
        raise ValueError(msg)
    major, minor, build = numbers
    return major, minor, build, 0


def with_file_version(data: bytes, version: str) -> bytes:
    """Вернуть ресурс VERSIONINFO с новой версией файла и продукта."""
    major, minor, build, private = parse_version(version)
    if (
        data[KEY_OFFSET : KEY_OFFSET + len(KEY)] != KEY
        or data[FIXED_OFFSET : FIXED_OFFSET + len(SIGNATURE)] != SIGNATURE
        or len(data) < VERSIONS_OFFSET + VERSIONS_SIZE
    ):
        msg = "Ресурс VERSIONINFO без VS_FIXEDFILEINFO"
        raise ValueError(msg)
    most = major << 16 | minor
    least = build << 16 | private
    versions = struct.pack("<IIII", most, least, most, least)
    return data[:VERSIONS_OFFSET] + versions + data[VERSIONS_OFFSET + VERSIONS_SIZE :]


def set_file_version(path: str, version: str) -> None:
    """Записать версию в ресурс VERSIONINFO через UpdateResource."""
    import win32api
    import win32con

    module = win32api.LoadLibraryEx(path, 0, win32con.LOAD_LIBRARY_AS_DATAFILE)
    try:
        languages = win32api.EnumResourceLanguages(module, RT_VERSION, VS_VERSION_INFO)
        if len(languages) != 1:
            msg = f"{path}: ожидался один язык ресурса VERSIONINFO, есть {languages}"
            raise ValueError(msg)
        [language] = languages
        data = win32api.LoadResource(module, RT_VERSION, VS_VERSION_INFO, language)
    finally:
        win32api.FreeLibrary(module)

    patched = with_file_version(data, version)
    update = win32api.BeginUpdateResource(path, 0)
    try:
        win32api.UpdateResource(update, RT_VERSION, VS_VERSION_INFO, patched, language)
    except BaseException:
        # Отменить правку: файл остаётся как был
        win32api.EndUpdateResource(update, 1)
        raise
    win32api.EndUpdateResource(update, 0)


def main(argv: list[str]) -> None:
    """Точка входа командной строки."""
    if len(argv) != 2:  # noqa: PLR2004
        sys.stderr.write(__doc__)
        sys.exit(2)
    path, version = argv
    set_file_version(path, version)
    sys.stdout.write(f"{path}: {'.'.join(map(str, parse_version(version)))}\n")


# Тесты вызывают main() напрямую
if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
