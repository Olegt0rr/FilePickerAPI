"""
Тесты workflow GitHub Actions: списки путей документации.

build-exe.yml и audit.yml не запускаются, если изменена только
документация (paths-ignore), а docs.yml запускается именно на неё
(paths). Списки должны совпадать: иначе путь, добавленный в один
файл, оставит изменения либо без проверки, либо с полным CI.
"""

import re
from fnmatch import fnmatch
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


# Порядок сборки и публикации (change parallel-ci-build): сборки идут
# параллельно с тестами, а сборки на подпись берутся только из
# артефактов, которые публикуются после тестов.


def workflow_jobs(workflow: str) -> dict[str, str]:
    """Текст каждого джоба workflow: от `  имя:` до следующего джоба.

    Без PyYAML: джобы — ключи с отступом в два пробела после `jobs:`.
    """
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    jobs_text = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):[ \t]*$", jobs_text, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def job_needs(job: str) -> set[str]:
    [needs] = re.findall(r"^    needs: (.+)$", job, re.MULTILINE)
    return {name.strip() for name in needs.strip("[] ").split(",")}


def uploaded_names(job: str) -> list[str]:
    return re.findall(
        r"uses: actions/upload-artifact@v\d+\n +with:\n +name: (.+)$",
        job,
        re.MULTILINE,
    )


def release_patterns() -> list[str]:
    """Шаблоны, по которым release.yml скачивает сборки на подпись."""
    sign = workflow_jobs("release.yml")["sign"]
    patterns = re.findall(r"^ +pattern: (\S+)$", sign, re.MULTILINE)
    assert patterns
    return patterns


def test_build_does_not_wait_for_tests():
    build = workflow_jobs("build-exe.yml")["build"]
    assert job_needs(build) == {"matrix"}


def test_build_artifacts_are_not_signed_by_release():
    build = workflow_jobs("build-exe.yml")["build"]
    names = uploaded_names(build)
    # Сама сборка — временный артефакт; прочие (журналы проверок) тоже
    # не должны подходить под шаблоны release.yml
    assert "untested-FilePickerAPI-${{ matrix.name }}" in names
    for name in names:
        for pattern in release_patterns():
            assert not fnmatch(name, pattern), (name, pattern)


def test_published_artifacts_wait_for_tests():
    jobs = workflow_jobs("build-exe.yml")
    publish = jobs["publish"]
    assert job_needs(publish) >= {"build", "test", "test-windows"}
    [name] = re.findall(r"^ +name: (FilePickerAPI-.+)$", publish, re.MULTILINE)
    [pattern] = re.findall(r"^ +pattern: (.+)$", publish, re.MULTILINE)
    assert pattern == "untested-" + name
    # release.yml подписывает именно опубликованные артефакты
    assert any(fnmatch(name, p) for p in release_patterns())
    # Кроме publish, под шаблоны release.yml никто ничего не выкладывает
    for job_name, job in jobs.items():
        for uploaded in uploaded_names(job):
            for p in release_patterns():
                assert not fnmatch(uploaded, p), (job_name, uploaded)


def test_sign_smoke_waits_for_tests():
    sign_smoke = workflow_jobs("build-exe.yml")["sign-smoke"]
    assert job_needs(sign_smoke) >= {"build", "test", "test-windows"}


def test_pip_cache_key_per_extras():
    # Джобы с разным набором extras не должны делить кэш pip: его
    # сохраняет первый завершившийся джоб, и в кэше не оказалось бы
    # пакетов остальных
    extras_by_suffix: dict[str, set[str]] = {}
    for workflow in ("build-exe.yml", "audit.yml"):
        for job_name, job in workflow_jobs(workflow).items():
            installs = re.findall(r"pip install -e \.\[([\w,]+)\]", job)
            if not installs:
                continue
            [extras] = installs
            keys = re.findall(r"^ +key: (pip-.+)$", job, re.MULTILINE)
            assert len(keys) == 1, f"{workflow}/{job_name}: нужен кэш pip"
            [key] = keys
            match = re.fullmatch(
                r"pip-\$\{\{ runner\.os \}\}-([\w-]+)-"
                r"\$\{\{ hashFiles\('pyproject\.toml'\) \}\}-"
                r"\$\{\{ github\.sha \}\}",
                key,
            )
            assert match, f"{workflow}/{job_name}: {key}"
            [restore] = re.findall(r"^ +restore-keys: (pip-.+)$", job, re.MULTILINE)
            assert restore == key.removesuffix("${{ github.sha }}")
            extras_by_suffix.setdefault(match[1], set()).add(extras)
    assert extras_by_suffix
    for suffix, extras in extras_by_suffix.items():
        assert len(extras) == 1, (suffix, extras)
