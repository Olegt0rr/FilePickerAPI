# Tasks

## 1. Проверка запуска в run_production.bat

- [x] 1.1 Вынести в начало скрипта переменные (`LOG_FILE`, `CURL`, `HEALTH_URL`, число попыток и пауза) и общую метку `:fail`, которая пишет `FAILED` с причиной в журнал, выводит сообщение, ждёт не больше 30 секунд через `timeout` и выходит через `popd` и `exit /b 1`; проверить чтением скрипта, что `pause` в нём не осталось
- [x] 1.2 Перевести отсутствие `FilePickerAPI.exe` на `:fail` с сохранением сообщения `Error: FilePickerAPI.exe not found!`; проверить, что текст сообщения не изменился
- [x] 1.3 До `start` проверить наличие `%SystemRoot%\System32\curl.exe` и то, что порт 8000 ещё не отвечает по HTTP; при ошибке — `:fail` без запуска exe; проверить чтением скрипта, что `start` не выполняется ни в одной из этих веток (ручная проверка на Windows — в «Workflow follow-up»)
- [x] 1.4 После `start` добавить цикл попыток: HTTP-запрос к `/openapi.json`, при успехе — строка `started` и код 0; при отсутствии процесса в `tasklist` — ранний `:fail`; по исчерпании попыток — `:fail` с пометкой, что процесс ещё работает; проверить чтением скрипта, что строка `started` пишется только после успешного HTTP-ответа (ручная проверка на Windows — в «Workflow follow-up»)
- [x] 1.5 Добавить `.gitattributes` с `*.bat text eol=crlf`; проверить `git ls-files --eol`, что в рабочей копии у bat-файлов CRLF
- [x] 1.6 Описать в README запуск через bat-скрипты: что пишется в журнал, код выхода, время ожидания, требование `curl.exe`; проверить, что README не предлагает способов задать директорию с файлами

## 2. Проверки

- [x] 2.1 Прогнать `openspec validate verify-production-startup --strict` и убедиться, что изменение валидно
- [x] 2.2 Прогнать `ruff check app tests`, `ruff format --check app tests` и `pytest` (Python-код не меняется, покрытие должно остаться 100%)

## Workflow follow-up

- Проверить вручную на Windows: успешный старт, `FILES_DIRECTORY` в `.env`, занятый порт 8000, запуск из Планировщика заданий, запуск из UNC-папки.
- Заархивировать изменение (`openspec archive verify-production-startup`) и прогнать `openspec validate --specs --strict`.
