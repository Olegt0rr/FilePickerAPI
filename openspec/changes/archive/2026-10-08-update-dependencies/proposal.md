# Proposal

## Why

Зависимости закреплены точными версиями и давно не обновлялись. `fastapi==0.124.2` разрешает только Starlette ниже 0.51, а все такие версии подвержены критической уязвимости BadHost (CVE-2026-48710, GHSA-86qp-5c8j-p5mr, исправлена в Starlette 1.0.1) и ещё нескольким advisory. Dependabot об этом не сообщает: Starlette — транзитивная зависимость, а в `pyproject.toml` её нет. Плановых обновлений в репозитории тоже нет: `.github/dependabot.yml` отсутствует, Dependabot открывает только PR по уязвимостям.

Starlette 1.x при `allow_origins=["*"]` и `allow_credentials=True` больше не отвечает `Access-Control-Allow-Origin: *`, а возвращает `Origin` запроса. С таким ответом браузер пропускает запросы с учётными данными (куки, HTTP-аутентификация) с любого сайта, а спека требует `*` по умолчанию.

## What Changes

- Обновить зависимости в `pyproject.toml`: FastAPI 0.143.0 (Starlette 1.7.0), Uvicorn 0.54.0, Pydantic 2.14.0, pydantic-settings 2.15.0, pytest 9.1.1, pytest-cov 7.1.0. httpx 0.28.1 и Nuitka 4.2.2 уже последние.
- Разрешать передачу учётных данных в CORS только для явного списка источников в `CORS_ORIGINS`. Когда разрешены все источники (`*`), ответ по-прежнему содержит `Access-Control-Allow-Origin: *` без `Access-Control-Allow-Credentials`.
- Добавить `.github/dependabot.yml`: еженедельные обновления pip и GitHub Actions, minor и patch для Python собираются в один PR.

Контракт HTTP API не меняется: поля, коды ответов и тексты `detail` остаются прежними. Меняются только заголовки CORS при явном списке источников и обработка учётных данных при `*`.

Вне рамок изменения:
- Переход тестов на `httpx2`, которого просит Starlette вместо `httpx` (пока это только предупреждение).
- Обновление версий GitHub Actions в workflow: их предложит Dependabot.
- Способы задать директорию с файлами при запуске не добавляются.

## Capabilities

### New Capabilities

### Modified Capabilities
- `service-configuration`: из требования «Настройка CORS» убрана безусловная передача учётных данных; новое требование «Учётные данные в CORS» разрешает их только для явного списка источников.

## Impact

- `pyproject.toml` — новые версии зависимостей.
- `app/__init__.py` — `allow_credentials` зависит от списка источников.
- `tests/test_main.py` — тесты заголовков учётных данных CORS.
- `.github/dependabot.yml` — новый файл.
- `openspec/specs/service-configuration/spec.md` — после архивации изменения.
