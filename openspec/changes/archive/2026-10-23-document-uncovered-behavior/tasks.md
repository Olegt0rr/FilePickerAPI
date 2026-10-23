# Tasks

## 1. Тесты для описанного поведения

- [x] 1.1 Сверить сценарии delta-спецификаций с существующими тестами (`TestCreatedAt`, `TestListingScandir`, `test_symlink_loop_skipped_in_listing`, `test_list_files_skips_invalid_name`, `TestFilesDirectoryCache`, `TestBranchesCli`) и для каждого сценария без теста добавить тест; в частности проверить через `GET /files`, что `createdAt` повреждённой метки сериализуется как `9999-12-31T23:59:59.999999Z`, а дата 1601 года — как `1601-01-01T00:00:00Z`
- [x] 1.2 Добавить в `tests/test_main.py` тест: при отсутствии `app._version` (подмена в `sys.modules` и перезагрузка `app`) `GET /openapi.json` возвращает `info.version` = `0.0.0`
- [x] 1.3 Если тест из 1.2 покрывает ветку `except ImportError` в `app/__init__.py`, снять с неё `# pragma: no cover`; поведение не менять; проверить, что TOTAL в `pytest --cov-report=term-missing` остался 100%

## 2. Проверки

- [x] 2.1 Прогнать `openspec validate document-uncovered-behavior --strict` и убедиться, что изменение валидно
- [x] 2.2 Прогнать `ruff check app tests scripts`, `ruff format --check app tests scripts` и `pytest` (покрытие должно остаться 100%)

## Workflow follow-up

- Заархивировать изменение (`openspec archive document-uncovered-behavior`) и прогнать `openspec validate --specs --strict`.
