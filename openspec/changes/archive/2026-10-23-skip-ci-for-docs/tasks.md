# Tasks

## 1. CI

- [x] 1.1 Добавить `paths-ignore` (`**.md`, `openspec/**`, `.claude/**`, `.github/openspec/**`) к `push` и `pull_request` в `build-exe.yml`
- [x] 1.2 Добавить тот же список в `audit.yml`; расписание, ручной запуск и `workflow_call` не трогать
- [x] 1.3 Добавить `docs.yml`: те же пути и сам `docs.yml` в `paths`, Node 24 через `setup-node`, `npm ci` в `.github/openspec` и `openspec validate --all --strict` на Ubuntu
- [x] 1.4 Закрепить openspec в `.github/openspec/package-lock.json`, добавить npm в Dependabot, `node_modules/` в `.gitignore`
- [x] 1.5 Отдельный джоб `npm-audit` в `docs.yml`: `npm audit` по `package-lock.json` раз в три дня и вручную; проверка спецификаций по расписанию не запускается
- [x] 1.6 Тест `tests/test_workflows.py`: списки путей в трёх workflow совпадают, `pull_request` ссылается на список `push`, workflow сборки и аудита в них нет
- [x] 1.7 Заархивировать `document-uncovered-behavior`, чтобы `validate --all --strict` проходил на `main`
- [x] 1.8 Убедиться, что в ruleset `main` нет обязательных проверок статуса, иначе PR только с документацией не слить

## 2. Документация

- [x] 2.1 Описать правило в README (раздел GitHub Actions) и в `openspec/config.yaml`
- [x] 2.2 В README запускать openspec той же версии, что в CI (`npm ci --prefix .github/openspec`), в «Проверках перед коммитом» `openspec/config.yaml` — `openspec validate --all --strict`

## 3. Проверки

- [x] 3.1 Прогнать `ruff check app tests scripts`, `ruff format --check app tests scripts`, `pytest` (код `app` не менялся; локально 32 теста для Windows пропущены, покрытие 100% проверяет CI) и `openspec validate --all --strict`

## Workflow follow-up

- Убедиться, что PR только с документацией запускает только Docs, а PR с этим изменением (он меняет workflow) запускает и Build and Test, и Dependency Audit.
