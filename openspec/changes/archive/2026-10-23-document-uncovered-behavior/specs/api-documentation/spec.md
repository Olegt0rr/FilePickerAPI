## MODIFIED Requirements

### Requirement: OpenAPI-схема
Сервис SHALL отдавать OpenAPI-схему по адресу `GET /openapi.json`. Схема SHALL содержать заголовок `File Picker API`, версию из git-тега (см. `distribution`) и описание всех эндпоинтов `/files` с моделями ответов. Если пакет запущен без установки и версия из git-тега недоступна (нет `app/_version.py`), сервис SHALL запускаться и указывать в схеме версию `0.0.0`.

#### Scenario: Получение схемы
- **WHEN** клиент выполняет `GET /openapi.json`
- **THEN** сервис отвечает статусом 200, JSON содержит поля `openapi` и `info`, а `info.title` = `File Picker API`

#### Scenario: Запуск без установки пакета
- **WHEN** сервис запущен из исходников без `pip install`, и файла `app/_version.py` нет
- **THEN** сервис запускается, а `GET /openapi.json` возвращает `info.version` = `0.0.0`
