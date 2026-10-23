# Tasks

## 1. CI

- [x] 1.1 Добавить `paths-ignore` (`**.md`, `openspec/**`, `.claude/**`, `.github/openspec/**`) к `push` и `pull_request` в `build-exe.yml`
- [x] 1.2 Добавить тот же список в `audit.yml`; расписание, ручной запуск и `workflow_call` не трогать
- [x] 1.3 Добавить `docs.yml`: те же пути и сам `docs.yml` в `paths`, Node 24 через `setup-node`, `npm ci` в `.github/openspec` и `openspec validate --all --strict` на Ubuntu
- [x] 1.3.1 Закрепить openspec в `.github/openspec/package-lock.json`, добавить npm в Dependabot, `node_modules/` в `.gitignore`
- [x] 1.3.2 Тест `tests/test_workflows.py`: списки путей в трёх workflow совпадают, workflow сборки и аудита в них нет
- [x] 1.4 Заархивировать `document-uncovered-behavior`, чтобы `validate --all --strict` проходил на `main`
- [x] 1.5 Убедиться, что в ruleset `main` нет обязательных проверок статуса, иначе PR только с документацией не слить

## 2. Документация

- [x] 2.1 Описать правило в README (раздел GitHub Actions) и в `openspec/config.yaml`

## 3. Проверки

- [x] 3.1 Прогнать `ruff check app tests scripts`, `ruff format --check app tests scripts` и `pytest` (Python-код не менялся; локально 32 теста для Windows пропущены, покрытие 100% проверяет CI)

## Workflow follow-up

- Убедиться, что PR только с документацией запускает только Docs, а PR с этим изменением (он меняет workflow) запускает и Build and Test, и Dependency Audit.
