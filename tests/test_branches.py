"""
Тесты сборки под филиалы: branches.toml и scripts/branches.py.
"""

import builtins
import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def load_branches_module():
    spec = importlib.util.spec_from_file_location(
        "branches_script", ROOT / "scripts" / "branches.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def branches():
    return load_branches_module()


@pytest.fixture
def write_toml(tmp_path):
    def write(content: str) -> Path:
        path = tmp_path / "branches.toml"
        path.write_text(content, encoding="utf-8")
        return path

    return write


class TestBranchesFile:
    def test_repository_branches_file_is_valid(self, branches):
        result = branches.load_branches()

        assert result
        for branch in result:
            assert branch["files_directory"].startswith("\\\\")

    def test_backslashes_are_not_escaped(self, branches, write_toml):
        path = write_toml(
            "[[branch]]\n"
            'name = "5000"\n'
            "files_directory = '\\\\host\\Payments\\5000\\CENTER\\RSB'\n"
        )

        result = branches.load_branches(path)

        assert result == [
            {
                "name": "5000",
                "files_directory": r"\\host\Payments\5000\CENTER\RSB",
            }
        ]

    def test_empty_list_rejected(self, branches, write_toml):
        with pytest.raises(ValueError, match="пуст"):
            branches.load_branches(write_toml(""))

    def test_duplicate_name_rejected(self, branches, write_toml):
        entry = '[[branch]]\nname = "5000"\nfiles_directory = "x"\n'
        with pytest.raises(ValueError, match="дважды"):
            branches.load_branches(write_toml(entry + entry))

    @pytest.mark.parametrize("name", ["", "50 00", "../x", "5000/1"])
    def test_invalid_name_rejected(self, branches, write_toml, name):
        path = write_toml(f'[[branch]]\nname = "{name}"\nfiles_directory = "x"\n')
        with pytest.raises(ValueError, match="недопустимое имя"):
            branches.load_branches(path)

    def test_name_with_trailing_newline_rejected(self, branches, write_toml):
        path = write_toml('[[branch]]\nname = "5000\\n"\nfiles_directory = "x"\n')
        with pytest.raises(ValueError, match="недопустимое имя"):
            branches.load_branches(path)

    def test_non_string_name_rejected(self, branches, write_toml):
        path = write_toml('[[branch]]\nname = 5000\nfiles_directory = "x"\n')
        with pytest.raises(ValueError, match="недопустимое имя"):
            branches.load_branches(path)

    def test_non_string_directory_rejected(self, branches, write_toml):
        path = write_toml('[[branch]]\nname = "5000"\nfiles_directory = 1\n')
        with pytest.raises(ValueError, match="не задан files_directory"):
            branches.load_branches(path)

    def test_single_table_rejected(self, branches, write_toml):
        path = write_toml('[branch]\nname = "5000"\nfiles_directory = "x"\n')
        with pytest.raises(ValueError, match=r"\[\[branch\]\]"):
            branches.load_branches(path)

    def test_missing_directory_rejected(self, branches, write_toml):
        path = write_toml('[[branch]]\nname = "5000"\nfiles_directory = " "\n')
        with pytest.raises(ValueError, match="не задан files_directory"):
            branches.load_branches(path)


class TestBranchesCli:
    def test_matrix_outputs_json(self, branches, capsys):
        branches.main(["matrix"])

        assert json.loads(capsys.readouterr().out) == branches.load_branches()

    def test_configure_writes_build_config(self, branches, tmp_path, monkeypatch):
        target = tmp_path / "_build_config.py"
        monkeypatch.setattr(branches, "BUILD_CONFIG_FILE", target)
        expected = branches.load_branches()[0]

        branches.main(["configure", expected["name"]])

        namespace = {}
        exec(target.read_text(encoding="utf-8"), namespace)
        assert namespace["BRANCH_NAME"] == expected["name"]
        assert namespace["FILES_DIRECTORY"] == expected["files_directory"]

    def test_configure_unknown_branch(self, branches, tmp_path, monkeypatch):
        monkeypatch.setattr(branches, "BUILD_CONFIG_FILE", tmp_path / "out.py")
        with pytest.raises(KeyError, match="не найден"):
            branches.main(["configure", "no-such-branch"])

    def test_usage_on_bad_arguments(self, branches):
        with pytest.raises(SystemExit) as exc_info:
            branches.main([])
        assert exc_info.value.code == 2


class TestSettingsFilesDirectory:
    @pytest.fixture(autouse=True)
    def restore_settings(self, monkeypatch):
        yield
        # Убираем подменённый app._build_config и перезагружаем
        # настройки, обработчики и приложение (как reload_app в
        # test_main): иначе они останутся привязаны к get_settings
        # из перезагруженного здесь модуля
        monkeypatch.undo()
        importlib.reload(sys.modules["app.settings"])
        importlib.reload(sys.modules["app.handlers.files"])
        importlib.reload(sys.modules["app"])

    def test_default_without_build_config(self, monkeypatch):
        # None в sys.modules заставляет импорт упасть с ImportError
        monkeypatch.setitem(sys.modules, "app._build_config", None)
        settings_module = importlib.reload(importlib.import_module("app.settings"))

        assert settings_module.Settings.files_directory == "./files"

    def test_broken_build_config_not_silenced(self, monkeypatch):
        # Модуль есть, но без FILES_DIRECTORY - ошибка, а не ./files

        build_config = types.ModuleType("app._build_config")
        monkeypatch.setitem(sys.modules, "app._build_config", build_config)

        with pytest.raises(ImportError):
            importlib.reload(importlib.import_module("app.settings"))

    def test_uses_build_config(self, monkeypatch):
        build_config = types.ModuleType("app._build_config")
        build_config.FILES_DIRECTORY = r"\\host\Payments\5400\CENTER\RSB"
        monkeypatch.setitem(sys.modules, "app._build_config", build_config)
        settings_module = importlib.reload(importlib.import_module("app.settings"))

        assert settings_module.Settings.files_directory == (
            r"\\host\Payments\5400\CENTER\RSB"
        )

    def test_broken_build_config_is_not_ignored(self, monkeypatch):
        """Сломанный конфиг сборки не подменяется на ./files."""
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "app._build_config":
                # Конфиг есть, но сам импортирует отсутствующий модуль
                raise ModuleNotFoundError(name="missing_dependency")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(ModuleNotFoundError) as exc_info:
            importlib.reload(importlib.import_module("app.settings"))
        assert exc_info.value.name == "missing_dependency"
