# Tasks

## 1. Зависимости

- [x] 1.1 Обновить точные версии в `pyproject.toml` (FastAPI, Uvicorn, Pydantic, pydantic-settings, pytest, pytest-cov); проверить установкой `pip install -e .[dev]` на Python 3.11, что ставится Starlette 1.x
- [x] 1.2 Добавить `.github/dependabot.yml` для pip и GitHub Actions; проверить, что файл разбирается как YAML

## 2. CORS

- [x] 2.1 В `app/__init__.py` включать `allow_credentials` только когда в списке источников нет `*`
- [x] 2.2 Описать в README, что учётные данные разрешаются только для явного списка источников
- [x] 2.3 Добавить тесты: при `*` нет `Access-Control-Allow-Credentials`, при явном списке возвращается источник запроса и `Access-Control-Allow-Credentials: true`; существующий тест на `Access-Control-Allow-Origin: *` проходит без изменений

## 3. Проверки

- [x] 3.1 Прогнать `openspec validate update-dependencies --strict`
- [x] 3.2 Прогнать `ruff check app tests`, `ruff format --check app tests` и `pytest` (покрытие 100%)

## Workflow follow-up

- Убедиться, что сборка Nuitka на Windows в CI проходит с новыми версиями.
