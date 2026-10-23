# Tasks

## 1. Проверка зависимостей

- [x] 1.1 Добавить extra `audit` с `pip-audit==2.10.1` в `pyproject.toml`
- [x] 1.2 Добавить `.github/workflows/audit.yml`: Windows, Python 3.11, обновление pip, setuptools и wheel, `pip install -e .[dev,build,audit]`, `pip-audit --strict` по списку из `pip freeze --all --exclude-editable` (без `--all` на Python 3.11 выпадают pip, setuptools и wheel); триггеры push, PR, ежедневное расписание, ручной запуск, `workflow_call`
- [x] 1.3 В `release.yml` вызвать проверку и сделать `sign` зависимым от неё
- [x] 1.4 Проверить локально: текущие зависимости проходят, `starlette==0.50.0` даёт ошибку со списком CVE

## 2. Документация

- [x] 2.1 Описать workflow в README (раздел GitHub Actions, бейдж) и в `openspec/config.yaml`

## 3. Проверки

- [x] 3.1 Прогнать `ruff check app tests scripts`, `ruff format --check app tests scripts` и `pytest` (покрытие 100%)

## Workflow follow-up

- Убедиться, что `audit.yml` проходит в GitHub Actions на Windows, а в релизе `sign` ждёт `audit`.
