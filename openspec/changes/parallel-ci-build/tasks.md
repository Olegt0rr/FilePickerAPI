# Tasks

## 1. CI

- [x] 1.1 Обновить Nuitka до 4.3.5
- [x] 1.2 Вынести список филиалов в джоб `matrix` (sparse checkout `scripts/` и `branches.toml`), сборке оставить `needs: matrix`
- [x] 1.3 Сборка выкладывает `untested-FilePickerAPI-<филиал>` на один день; джоб `publish` после `build`, `test` и `test-windows` переупаковывает его в `FilePickerAPI-<филиал>` на 30 дней и удаляет временный (`actions/upload-artifact/merge`, `delete-merged`)
- [x] 1.4 `sign-smoke` ждёт `publish`
- [x] 1.5 Кэш pip (`actions/cache`) в `test`, `test-windows`, `build` и `audit.yml` с ключом по набору extras и коммиту; в сборке по тегу кэш не используется

## 2. Документация

- [x] 2.1 Описать порядок джобов в README (раздел GitHub Actions) и в `openspec/config.yaml`
- [x] 2.2 Перенести формулировку CI в дельты «Сборка для Windows» изменений `remove-bat-scripts` и `user-logon-autostart`

## 3. Проверки

- [x] 3.1 Прогнать `ruff check app tests scripts`, `ruff format --check app tests scripts`, `pytest` и `openspec validate --all --strict` (код `app` не меняется)

## Workflow follow-up

- В PR убедиться, что сборки стартуют одновременно с тестами, `publish` создаёт `FilePickerAPI-<филиал>`, временные артефакты удалены, а `sign-smoke` их скачивает.
- Сравнить время Build and Test со второго-третьего прогона, когда прогреты кэши pip и Nuitka.
