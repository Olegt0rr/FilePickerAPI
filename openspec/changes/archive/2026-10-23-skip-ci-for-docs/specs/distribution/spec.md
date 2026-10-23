## ADDED Requirements

### Requirement: Изменения только в документации проверяются без сборки
Если в push или pull request в `main`/`master` изменены только файлы документации (`*.md` в любой папке, `openspec/`, `.claude/`), Build and Test и Dependency Audit MUST NOT запускаться, а CI SHALL проверять только спецификации и изменения OpenSpec (`openspec validate --all --strict`) на Linux без зависимостей проекта. Ручной запуск, ежедневная проверка зависимостей и релиз по тегу SHALL выполняться всегда.

#### Scenario: Изменена только документация
- **WHEN** открыт pull request в `main`, который меняет только `README.md` и `openspec/specs/distribution/spec.md`
- **THEN** ни Build and Test, ни Dependency Audit не запускаются, а проверка спецификаций OpenSpec запускается

#### Scenario: Ошибка в спецификации
- **WHEN** pull request меняет только `openspec/specs/file-listing/spec.md`, и в требовании нет ни одного сценария
- **THEN** проверка спецификаций падает с указанием файла и требования

#### Scenario: Документация вместе с кодом
- **WHEN** pull request меняет `README.md` и `app/handlers/files.py`
- **THEN** CI запускает тесты, сборку и проверку зависимостей, как для любого изменения кода

#### Scenario: Изменён workflow
- **WHEN** pull request меняет только `.github/workflows/build-exe.yml`
- **THEN** CI запускается: файлы workflow документацией не считаются

#### Scenario: Релиз после изменения документации
- **WHEN** последний коммит перед тегом `v2.3.0` менял только документацию
- **THEN** release.yml по тегу собирает, проверяет и публикует релиз как обычно
