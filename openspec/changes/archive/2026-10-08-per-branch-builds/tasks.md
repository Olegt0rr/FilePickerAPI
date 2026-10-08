# Tasks

## 1. Список филиалов

- [x] 1.1 Добавить `branches.toml` с филиалами 5000, 5100 и 5400; пути в одинарных кавычках TOML, без экранирования обратных слэшей
- [x] 1.2 Добавить `scripts/branches.py` с командами `matrix` и `configure <филиал>` и проверкой списка (пустой список, `[branch]` вместо `[[branch]]`, повтор имени, недопустимое или нестроковое имя, пустой или нестроковый путь)

## 2. Источник пути

- [x] 2.1 `app/settings.py`: `Settings.files_directory` из `app._build_config`, без него — `./files`; прочие ошибки импорта не подавлять; путь остаётся `ClassVar`
- [x] 2.2 Добавить `app/_build_config.py` в `.gitignore`

## 3. CI и релиз

- [x] 3.1 `build-exe.yml`: матрица по `branches.toml` из джоба `test`, шаги `configure` и проверки пути, `--include-module=app._build_config`, артефакты `FilePickerAPI-<филиал>`, `sign-smoke` по всем сборкам
- [x] 3.2 `release.yml`: подпись всех сборок, архив `FilePickerAPI-<филиал>.zip` на филиал
- [x] 3.3 Проверить воркфлоу `actionlint`

## 4. Тесты и документация

- [x] 4.1 `tests/test_branches.py`: проверка `branches.toml` репозитория, валидации, команд `matrix`/`configure`, пути из конфига сборки, `./files` без него и ошибки при сломанном конфиге; покрытие остаётся 100%
- [x] 4.2 Обновить README (сборка под филиалы, артефакты и архивы релиза) и `openspec/config.yaml`

## 5. Проверки

- [x] 5.1 Прогнать `openspec validate per-branch-builds --strict`
- [x] 5.2 Прогнать `ruff check app tests scripts`, `ruff format --check app tests scripts` и `pytest` (TOTAL 100%)

## Workflow follow-up

- Первый прогон CI на Windows: собраны три сборки, `sign-smoke` подписал все.
