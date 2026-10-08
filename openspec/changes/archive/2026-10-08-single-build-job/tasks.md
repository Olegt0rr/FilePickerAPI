# Tasks

## 1. CI и релиз

- [x] 1.1 `build-exe.yml`: один джоб `build` с циклом по филиалам (`configure`, сверка пути, сборка Nuitka в `dist/FilePickerAPI-<филиал>/FilePickerAPI`), выход `branches` джоба `test`, один артефакт `FilePickerAPI`
- [x] 1.2 `sign-smoke` и `release.yml`: скачивание артефакта `FilePickerAPI`
- [x] 1.3 Проверить воркфлоу `actionlint`

## 2. Документация

- [x] 2.1 Обновить README (сборка в одном джобе, артефакт `FilePickerAPI`) и `openspec/config.yaml`

## 3. Проверки

- [x] 3.1 Прогнать `openspec validate single-build-job --strict`
- [x] 3.2 Прогнать `ruff check app tests scripts`, `ruff format --check app tests scripts` и `pytest` (TOTAL 100%)
