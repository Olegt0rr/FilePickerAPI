## ADDED Requirements

### Requirement: Команды scripts/branches.py
`scripts/branches.py` SHALL предоставлять CI две команды, каждая из которых сначала проверяет `branches.toml` (см. «Список филиалов»):
- `matrix` — выводит в stdout одной строкой JSON-массив объектов `{"name": ..., "files_directory": ...}` в порядке блоков `[[branch]]`;
- `configure <филиал>` — записывает `app/_build_config.py` с константами `BRANCH_NAME` и `FILES_DIRECTORY` этого филиала и выводит строку `<филиал>: <путь>`.

#### Scenario: Матрица для CI
- **WHEN** CI выполняет `python scripts/branches.py matrix`
- **THEN** в stdout выводится JSON-массив, по элементу на каждый филиал из `branches.toml`, и по нему строится матрица сборок

#### Scenario: Запись пути филиала
- **WHEN** CI выполняет `python scripts/branches.py configure 5400`
- **THEN** в `app/_build_config.py` записываются `BRANCH_NAME = '5400'` и `FILES_DIRECTORY` с путём филиала 5400 из `branches.toml`

### Requirement: Ошибки в аргументах scripts/branches.py
Для филиала, которого нет в `branches.toml`, команда `configure` MUST завершаться с ошибкой и ненулевым кодом. При неверных аргументах `scripts/branches.py` SHALL выводить справку в stderr и завершаться с кодом 2.

#### Scenario: Неизвестный филиал
- **WHEN** выполняется `python scripts/branches.py configure no-such-branch`
- **THEN** скрипт завершается ошибкой с сообщением, что филиал не найден в `branches.toml`, и сборка этого филиала не выполняется

#### Scenario: Неверные аргументы
- **WHEN** скрипт запущен без аргументов или с неизвестной командой
- **THEN** скрипт выводит справку по использованию в stderr и завершается с кодом 2
