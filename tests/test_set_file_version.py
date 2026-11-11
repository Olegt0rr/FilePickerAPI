"""
Тесты scripts/set_file_version.py: другая версия в ресурсах копии
папки сборки для дымовой проверки обновления MSI.

Разбор ресурса проверяется на любой ОС, вызовы pywin32 — с поддельными
модулями, правка настоящего файла — только на Windows.
"""

import shutil
import struct
import sys
import types
from pathlib import Path

import pytest

from tests.platforms import windows_only
from tests.script_loader import load_script


@pytest.fixture
def script():
    return load_script("set_file_version")


def version_info(file_version=(2, 3, 1, 0), product_version=(2, 3, 1, 0), tail=b"tail"):
    """VS_VERSIONINFO: заголовок, ключ, VS_FIXEDFILEINFO и хвост."""
    key = "VS_VERSION_INFO\0".encode("utf-16-le")

    def pair(parts):
        major, minor, build, private = parts
        return major << 16 | minor, build << 16 | private

    fixed = struct.pack(
        "<II4I",
        0xFEEF04BD,
        0x00010000,
        *pair(file_version),
        *pair(product_version),
    ) + bytes(36)
    body = key + b"\0\0" + fixed + tail
    return struct.pack("<HHH", 6 + len(body), 52, 0) + body


def versions(data):
    """Версии файла и продукта из VS_FIXEDFILEINFO."""
    file_ms, file_ls, product_ms, product_ls = struct.unpack_from("<4I", data, 48)

    def parts(most, least):
        return most >> 16, most & 0xFFFF, least >> 16, least & 0xFFFF

    return parts(file_ms, file_ls), parts(product_ms, product_ls)


class TestParseVersion:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("2.3.2", (2, 3, 2, 0)),
            ("0.0.0", (0, 0, 0, 0)),
            ("65535.1.65535", (65535, 1, 65535, 0)),
        ],
    )
    def test_valid(self, script, version, expected):
        assert script.parse_version(version) == expected

    @pytest.mark.parametrize(
        "version", ["", "2.3", "2.3.1.0", "v2.3.1", "2.3.1\n", "２.3.1"]
    )
    def test_invalid(self, script, version):
        with pytest.raises(ValueError, match="не вида"):
            script.parse_version(version)

    def test_out_of_limits(self, script):
        with pytest.raises(ValueError, match="больше 65535"):
            script.parse_version("1.65536.0")


class TestWithFileVersion:
    def test_replaces_file_and_product_version(self, script):
        data = version_info()

        patched = script.with_file_version(data, "2.3.2")

        assert versions(patched) == ((2, 3, 2, 0), (2, 3, 2, 0))
        # Всё, кроме четырёх чисел версий, на месте
        assert len(patched) == len(data)
        assert patched[:48] == data[:48]
        assert patched[64:] == data[64:]

    def test_private_part_is_reset(self, script):
        data = version_info(file_version=(1, 2, 3, 4), product_version=(1, 2, 3, 4))

        patched = script.with_file_version(data, "1.2.4")

        assert versions(patched) == ((1, 2, 4, 0), (1, 2, 4, 0))

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            # Другой ключ
            version_info().replace(
                "VS_VERSION_INFO".encode("utf-16-le"),
                "VS_VERSION_INFX".encode("utf-16-le"),
            ),
            # Нет подписи VS_FIXEDFILEINFO
            version_info().replace(struct.pack("<I", 0xFEEF04BD), bytes(4)),
            # Обрезанный VS_FIXEDFILEINFO
            version_info()[:60],
        ],
    )
    def test_rejects_unexpected_layout(self, script, data):
        with pytest.raises(ValueError, match="VS_FIXEDFILEINFO"):
            script.with_file_version(data, "2.3.2")


class FakeWin32:
    """Поддельные win32api и win32con: ресурс и журнал вызовов."""

    def __init__(self, data, languages=(1049,), update_error=None):
        self.data = data
        self.languages = list(languages)
        self.update_error = update_error
        self.calls = []
        self.written = None
        self.api = types.ModuleType("win32api")
        for name in (
            "LoadLibraryEx",
            "EnumResourceLanguages",
            "LoadResource",
            "FreeLibrary",
            "BeginUpdateResource",
            "UpdateResource",
            "EndUpdateResource",
        ):
            setattr(self.api, name, getattr(self, name))
        self.con = types.ModuleType("win32con")
        self.con.LOAD_LIBRARY_AS_DATAFILE = 2

    def LoadLibraryEx(self, path, handle, flags):  # noqa: N802
        self.calls.append(("LoadLibraryEx", path, handle, flags))
        return "module"

    def EnumResourceLanguages(self, module, kind, name):  # noqa: N802
        self.calls.append(("EnumResourceLanguages", module, kind, name))
        return self.languages

    def LoadResource(self, module, kind, name, language):  # noqa: N802
        self.calls.append(("LoadResource", module, kind, name, language))
        return self.data

    def FreeLibrary(self, module):  # noqa: N802
        self.calls.append(("FreeLibrary", module))

    def BeginUpdateResource(self, path, delete):  # noqa: N802
        self.calls.append(("BeginUpdateResource", path, delete))
        return "update"

    def UpdateResource(self, update, kind, name, data, language):  # noqa: N802
        self.calls.append(("UpdateResource", update, kind, name, language))
        if self.update_error is not None:
            raise self.update_error
        self.written = data

    def EndUpdateResource(self, update, discard):  # noqa: N802
        self.calls.append(("EndUpdateResource", update, discard))

    def install(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "win32api", self.api)
        monkeypatch.setitem(sys.modules, "win32con", self.con)
        return self


class TestSetFileVersion:
    def test_updates_version_resource(self, script, monkeypatch):
        fake = FakeWin32(version_info()).install(monkeypatch)

        script.set_file_version("FilePickerAPI.exe", "2.3.2")

        assert versions(fake.written) == ((2, 3, 2, 0), (2, 3, 2, 0))
        assert fake.calls == [
            ("LoadLibraryEx", "FilePickerAPI.exe", 0, 2),
            ("EnumResourceLanguages", "module", 16, 1),
            ("LoadResource", "module", 16, 1, 1049),
            ("FreeLibrary", "module"),
            ("BeginUpdateResource", "FilePickerAPI.exe", 0),
            ("UpdateResource", "update", 16, 1, 1049),
            ("EndUpdateResource", "update", 0),
        ]

    @pytest.mark.parametrize("languages", [(), (1033, 1049)])
    def test_requires_single_language(self, script, monkeypatch, languages):
        fake = FakeWin32(version_info(), languages=languages).install(monkeypatch)

        with pytest.raises(ValueError, match="один язык"):
            script.set_file_version("FilePickerAPI.exe", "2.3.2")

        # Модуль освобождён, правка не начата
        assert fake.calls[-1] == ("FreeLibrary", "module")

    def test_bad_resource_is_not_written(self, script, monkeypatch):
        fake = FakeWin32(b"not a version resource").install(monkeypatch)

        with pytest.raises(ValueError, match="VS_FIXEDFILEINFO"):
            script.set_file_version("FilePickerAPI.exe", "2.3.2")

        assert fake.calls[-1] == ("FreeLibrary", "module")

    def test_update_error_discards_changes(self, script, monkeypatch):
        error = OSError("UpdateResource failed")
        fake = FakeWin32(version_info(), update_error=error).install(monkeypatch)

        with pytest.raises(OSError, match="UpdateResource failed"):
            script.set_file_version("FilePickerAPI.exe", "2.3.2")

        assert fake.calls[-1] == ("EndUpdateResource", "update", 1)


class TestCli:
    def test_sets_version(self, script, monkeypatch, capsys):
        fake = FakeWin32(version_info()).install(monkeypatch)

        script.main(["FilePickerAPI.exe", "2.3.2"])

        assert versions(fake.written) == ((2, 3, 2, 0), (2, 3, 2, 0))
        assert capsys.readouterr().out == "FilePickerAPI.exe: 2.3.2.0\n"

    @pytest.mark.parametrize("argv", [[], ["FilePickerAPI.exe"], ["a", "2.3.2", "b"]])
    def test_usage_on_bad_arguments(self, script, capsys, argv):
        with pytest.raises(SystemExit) as exc_info:
            script.main(argv)

        assert exc_info.value.code == 2
        assert "Использование" in capsys.readouterr().err


@windows_only
def test_real_file(script, tmp_path):
    # Настоящая DLL с VERSIONINFO: python3.dll рядом с интерпретатором
    import win32api

    source = Path(sys.base_prefix) / "python3.dll"
    target = tmp_path / "copy.dll"
    shutil.copyfile(source, target)
    before = win32api.GetFileVersionInfo(str(source), "\\")

    script.set_file_version(str(target), "7.8.9")

    info = win32api.GetFileVersionInfo(str(target), "\\")
    assert (info["FileVersionMS"], info["FileVersionLS"]) == (7 << 16 | 8, 9 << 16)
    assert (info["ProductVersionMS"], info["ProductVersionLS"]) == (
        7 << 16 | 8,
        9 << 16,
    )
    # Остальные поля VS_FIXEDFILEINFO не тронуты
    for field in (
        "Signature",
        "StrucVersion",
        "FileFlagsMask",
        "FileFlags",
        "FileOS",
        "FileType",
    ):
        assert info[field] == before[field]
