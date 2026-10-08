"""Работа со списком филиалов из branches.toml для сборки в CI.

Использование:
    python scripts/branches.py matrix
        Вывести JSON-матрицу филиалов для GitHub Actions.
    python scripts/branches.py configure <name>
        Записать путь филиала в app/_build_config.py перед сборкой exe.
"""

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRANCHES_FILE = ROOT / "branches.toml"
BUILD_CONFIG_FILE = ROOT / "app" / "_build_config.py"
NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def load_branches(path: Path = BRANCHES_FILE) -> list[dict[str, str]]:
    """Прочитать и проверить список филиалов."""
    with path.open("rb") as f:
        branches = tomllib.load(f).get("branch", [])

    if not isinstance(branches, list) or not all(isinstance(b, dict) for b in branches):
        msg = f"{path.name}: филиалы задаются блоками [[branch]]"
        raise ValueError(msg)
    if not branches:
        msg = f"{path.name}: список филиалов пуст"
        raise ValueError(msg)

    names = set()
    for branch in branches:
        name = branch.get("name")
        directory = branch.get("files_directory")
        if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
            msg = f"{path.name}: недопустимое имя филиала {name!r}"
            raise ValueError(msg)
        if name in names:
            msg = f"{path.name}: филиал {name!r} указан дважды"
            raise ValueError(msg)
        if not isinstance(directory, str) or not directory.strip():
            msg = f"{path.name}: у филиала {name!r} не задан files_directory"
            raise ValueError(msg)
        names.add(name)

    return [
        {"name": b["name"], "files_directory": b["files_directory"]} for b in branches
    ]


def write_build_config(name: str) -> str:
    """Записать путь филиала в app/_build_config.py."""
    branches = {b["name"]: b for b in load_branches()}
    if name not in branches:
        msg = f"Филиал {name!r} не найден в {BRANCHES_FILE.name}"
        raise KeyError(msg)

    directory = branches[name]["files_directory"]
    BUILD_CONFIG_FILE.write_text(
        '"""Сгенерировано scripts/branches.py при сборке. Не редактировать."""\n\n'
        f"BRANCH_NAME = {name!r}\n"
        f"FILES_DIRECTORY = {directory!r}\n",
        encoding="utf-8",
    )
    return directory


def main(argv: list[str]) -> None:
    """Точка входа командной строки."""
    if argv == ["matrix"]:
        sys.stdout.write(json.dumps(load_branches()) + "\n")
    elif len(argv) == 2 and argv[0] == "configure":  # noqa: PLR2004
        directory = write_build_config(argv[1])
        sys.stdout.write(f"{argv[1]}: {directory}\n")
    else:
        sys.stderr.write(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
