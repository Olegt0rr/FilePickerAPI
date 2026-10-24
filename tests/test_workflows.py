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
    key_re = re.escape(key)
    comment = r"(?: +#[^\n]*)?"
    lists = re.findall(
        rf"^ +{key_re}: &([\w-]+){comment}\n((?: +- '[^']+'{comment}\n)+)",
        text,
        re.MULTILINE,
    )
    assert len(lists) == 1, f"{workflow}: нужен ровно один список {key} с якорем"
    [(anchor, items)] = lists
    # Оба триггера используют один и тот же список: других значений
    # у ключа, кроме самого списка и ссылки на его якорь, нет.
    # Комментарий в конце строки не мешает
    values = [
        value.split("#", 1)[0].strip()
        for value in re.findall(rf"^ +{key_re}:(.*)$", text, re.MULTILINE)
    ]
    assert values == [f"&{anchor}", f"*{anchor}"], (
        f"{workflow}: pull_request должен ссылаться на список push (*{anchor})"
    )
    return re.findall(r"- '([^']+)'", items)


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
