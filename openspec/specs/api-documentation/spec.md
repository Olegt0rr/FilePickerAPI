# api-documentation Specification

## Purpose
Автоматически генерируемая документация HTTP API, по которой разработчики фронтенда изучают и проверяют эндпоинты без чтения исходного кода.

## Requirements

### Requirement: OpenAPI-схема
Сервис SHALL отдавать OpenAPI-схему по адресу `GET /openapi.json`. Схема SHALL содержать заголовок `File Picker API`, версию из git-тега (см. `distribution`) и описание всех эндпоинтов `/files` с моделями ответов. Если пакет запущен без установки и версия из git-тега недоступна (нет `app/_version.py`), сервис SHALL запускаться и указывать в схеме версию `0.0.0`.

#### Scenario: Получение схемы
- **WHEN** клиент выполняет `GET /openapi.json`
- **THEN** сервис отвечает статусом 200, JSON содержит поля `openapi` и `info`, а `info.title` = `File Picker API`

#### Scenario: Запуск без установки пакета
- **WHEN** сервис запущен из исходников без `pip install`, и файла `app/_version.py` нет
- **THEN** сервис запускается, а `GET /openapi.json` возвращает `info.version` = `0.0.0`

### Requirement: Интерактивная документация
Сервис SHALL предоставлять интерактивную документацию Swagger UI по адресу `/docs` и документацию ReDoc по адресу `/redoc`.

#### Scenario: Открытие Swagger UI
- **WHEN** пользователь открывает `http://<host>:8000/docs` в браузере
- **THEN** сервис отвечает статусом 200 и HTML-страницей Swagger UI

#### Scenario: Открытие ReDoc
- **WHEN** пользователь открывает `http://<host>:8000/redoc` в браузере
- **THEN** сервис отвечает статусом 200 и HTML-страницей ReDoc
