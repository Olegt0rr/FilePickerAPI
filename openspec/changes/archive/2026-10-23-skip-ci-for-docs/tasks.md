# Tasks

## 1. CI

- [x] 1.1 Добавить `paths-ignore` (`**.md`, `openspec/**`, `.claude/**`) к `push` и `pull_request` в `build-exe.yml`
- [x] 1.2 Добавить тот же список в `audit.yml`; расписание, ручной запуск и `workflow_call` не трогать
- [x] 1.3 Убедиться, что в ruleset `main` нет обязательных проверок статуса, иначе PR только с документацией не слить

## 2. Документация

- [x] 2.1 Описать правило в README (раздел GitHub Actions) и в `openspec/config.yaml`

## 3. Проверки

- [x] 3.1 Прогнать `ruff check app tests scripts`, `ruff format --check app tests scripts` и `pytest` (покрытие 100%)

## Workflow follow-up

- Убедиться, что PR только с документацией не запускает Build and Test и Dependency Audit, а PR с этим изменением (он меняет workflow) запускает оба.
