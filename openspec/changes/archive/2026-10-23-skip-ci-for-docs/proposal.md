# Proposal

## Why

Build and Test и Dependency Audit запускаются на каждый push и pull request в `main`/`master`, даже если изменены только README или спецификации OpenSpec (задача #38). Такой прогон тратит минуты раннеров (тесты на Ubuntu и Windows, сборка Nuitka под каждый филиал, pip-audit на Windows), хотя код и состав exe от него не меняются.

## What Changes

- В `build-exe.yml` и `audit.yml` у триггеров `push` и `pull_request` появляется `paths-ignore` с одним и тем же списком: `**.md`, `openspec/**`, `.claude/**`. Если в коммите или PR изменены только такие файлы, workflow не запускается; если изменён хотя бы один другой файл — запускается целиком, как раньше.
- Новый лёгкий workflow `.github/workflows/docs.yml` запускается на тот же список путей (`paths`) и проверяет спецификации и незаархивированные изменения: `openspec validate --all --strict` на Ubuntu через `npx` с закреплённой версией, без зависимостей проекта и Nuitka. Для этого заархивировано оставшееся с #34 изменение `document-uncovered-behavior`, которое проверку не проходило.
- Ручной запуск, ежедневный запуск аудита и вызов обоих workflow из `release.yml` не меняются: релиз по тегу всегда собирается и проверяется.
- README и `openspec/config.yaml` описывают новое правило.

Контракт HTTP API не меняется: поля, коды ответов и тексты `detail` остаются прежними.

Вне рамок изменения:
- Автоматическое ревью Copilot в PR остаётся как есть: оно включено правилом репозитория (ruleset `main`), а не workflow.
- Проверка ссылок и оформления Markdown.
- Пропуск CI для других не влияющих на код файлов (`.gitignore`, `.gitattributes`, `.github/dependabot.yml`): они меняются редко, и ошибиться в списке дороже, чем один лишний прогон.
- Способы задать директорию с файлами при запуске не добавляются.

## Capabilities

### New Capabilities

### Modified Capabilities
- `distribution`: новое требование «Изменения только в документации проверяются без сборки».

## Impact

- `.github/workflows/build-exe.yml`, `.github/workflows/audit.yml` — `paths-ignore` у `push` и `pull_request`.
- `.github/workflows/docs.yml` — новый файл.
- `README.md`, `openspec/config.yaml` — описание правила.
- `openspec/specs/distribution/spec.md` — после архивации изменения.
- В ruleset `main` нет обязательных проверок статуса, поэтому PR только с документацией можно слить без Build and Test.
