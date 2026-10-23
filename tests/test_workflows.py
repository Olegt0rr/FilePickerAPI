"""
Тесты workflow GitHub Actions: списки путей документации.

build-exe.yml и audit.yml не запускаются, если изменена только
документация (paths-ignore), а docs.yml запускается именно на неё
(paths). Списки должны совпадать: иначе путь, добавленный в один
файл, оставит изменения либо без проверки, либо с полным CI.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# Сам docs.yml есть только в его paths: изменение workflow проверяется
# в своём PR, а сборку и аудит оно не отменяет
DOCS_WORKFLOW_ONLY = {".github/workflows/docs.yml"}


def anchored_list(workflow: str, key: str) -> list[str]:
    """Пути из списка `key: &якорь` (у push) в workflow.

    Без PyYAML: строки списка идут сразу за ключом и начинаются с
    `- '...'`. pull_request ссылается на тот же якорь.
    """
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    match = re.search(
        rf"^ +{re.escape(key)}: &[\w-]+\n((?: +- '[^']+'\n)+)", text, re.MULTILINE
    )
    assert match, f"{workflow}: нет списка {key} с якорем"
    paths = re.findall(r"- '([^']+)'", match.group(1))
    # Оба триггера используют один и тот же список
    assert re.search(rf"^ +{re.escape(key)}: \*[\w-]+$", text, re.MULTILINE), (
        f"{workflow}: pull_request не ссылается на список push"
    )
    return paths


@pytest.mark.parametrize("workflow", ["build-exe.yml", "audit.yml"])
def test_docs_paths_match(workflow):
    docs_paths = set(anchored_list("docs.yml", "paths"))
    ignored = anchored_list(workflow, "paths-ignore")

    assert len(ignored) == len(set(ignored))
    assert set(ignored) == docs_paths - DOCS_WORKFLOW_ONLY


def test_docs_workflow_triggers_on_itself():
    assert set(anchored_list("docs.yml", "paths")) >= DOCS_WORKFLOW_ONLY


def test_workflows_are_not_ignored():
    # Изменение любого workflow, кроме docs.yml, должно запускать
    # полный CI
    for workflow in ("build-exe.yml", "audit.yml"):
        for path in anchored_list(workflow, "paths-ignore"):
            assert not path.startswith(".github/workflows"), path
