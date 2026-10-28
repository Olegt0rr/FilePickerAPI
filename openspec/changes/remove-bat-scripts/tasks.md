# Tasks

Выполняется, только когда `user-logon-autostart` реализовано и заархивировано, а все машины филиалов переведены на запуск при входе.

## 1. Подготовка

- [ ] 1.1 Убедиться, что на всех машинах филиалов сервис запускается задачей `FilePickerAPI-<SID>` после `deploy`, а задачи и ярлыки с `run_production.bat` удалены
- [ ] 1.2 Сверить требования с пометкой `MODIFIED` в дельте `distribution` с текущей спецификацией: при архивации они заменяются целиком

## 2. Удаление скриптов

- [ ] 2.1 Удалить `run_test.bat` и `run_production.bat`; убрать правило `*.bat` из `.gitattributes`, если других bat-файлов не осталось
- [ ] 2.2 Убрать копирование bat-скриптов в папку сборки из `.github/workflows/build-exe.yml`
- [ ] 2.3 Добавить в `.github/scripts/autostart-smoke.ps1` проверку, что в папке сборки нет `*.bat`, и убрать запуск `run_test.bat`

## 3. Документация

- [ ] 3.1 Убрать из `README.md` и текста релиза в `.github/workflows/release.yml` bat-скрипты как способ запуска; оставить раздел о переводе машины со старой схемы
- [ ] 3.2 Обновить Purpose в `openspec/specs/distribution/spec.md` (архивация его не обновит: «сборка Nuitka с запуском при входе пользователя», без bat-скриптов), `openspec/config.yaml` и `CLAUDE.md` (bat-скрипты больше не упоминаются как способ запуска; запрет задавать путь параметрами по-прежнему перечисляет аргументы exe и задачи Планировщика)

## 4. Проверки

- [ ] 4.1 Прогнать `openspec validate remove-bat-scripts --strict`
- [ ] 4.2 Прогнать `ruff check app tests scripts`, `ruff format --check app tests scripts` и `pytest`; CI со сборкой и дымовой проверкой проходит

## Workflow follow-up

- Выпустить релиз `v3.0.0` (BREAKING) и в описании релиза указать, что bat-скрипты удалены.
- Заархивировать изменение (`openspec archive remove-bat-scripts`) и прогнать `openspec validate --specs --strict`.
