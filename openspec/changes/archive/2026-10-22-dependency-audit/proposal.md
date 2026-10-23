# Proposal

## Why

Зависимости проверяются на уязвимости только через Dependabot: он видит лишь пакеты, перечисленные в `pyproject.toml`, и сообщает о проблеме алертом, который ничего не блокирует. Транзитивные зависимости (так было со Starlette и CVE-2026-48710) остаются незамеченными, пока их не закрепят вручную, а релиз можно собрать и подписать с известной уязвимостью.

## What Changes

- Новый workflow `.github/workflows/audit.yml`: на Windows с Python 3.11 ставит проект со всеми extras и проверяет каждый установленный пакет через pip-audit (`--strict`, вывод CVE/GHSA). Запускается при push и PR в `main`/`master`, ежедневно по расписанию и вручную.
- `release.yml` вызывает этот workflow, а подпись ждёт его результата: релиз с известной уязвимостью не подписывается и не публикуется.
- В `pyproject.toml` новый extra `audit` с закреплённой версией pip-audit, чтобы её обновлял Dependabot.
- README и `openspec/config.yaml` описывают новую проверку.

Контракт HTTP API не меняется: поля, коды ответов и тексты `detail` остаются прежними.

Вне рамок изменения:
- Проверка версий GitHub Actions (их обновляет Dependabot) и закрепление экшенов по SHA.
- Проверка окружения тестов на Ubuntu: там дополнительно ставится только `uvloop`, который не попадает в exe.
- Способы задать директорию с файлами при запуске не добавляются.

## Capabilities

### New Capabilities

### Modified Capabilities
- `distribution`: новое требование «Проверка зависимостей на уязвимости».

## Impact

- `.github/workflows/audit.yml` — новый файл.
- `.github/workflows/release.yml` — джоб `audit`, `sign` зависит от него.
- `pyproject.toml` — extra `audit`.
- `README.md`, `openspec/config.yaml` — описание проверки.
- `openspec/specs/distribution/spec.md` — после архивации изменения.
