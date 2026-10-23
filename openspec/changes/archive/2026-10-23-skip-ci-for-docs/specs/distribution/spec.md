## ADDED Requirements

### Requirement: CI не запускается для изменений только в документации
Тесты, сборка (Build and Test) и проверка зависимостей (Dependency Audit) MUST NOT запускаться по push и pull request в `main`/`master`, если изменены только файлы документации: `*.md` в любой папке, всё в `openspec/` и `.claude/`. Если вместе с ними изменён любой другой файл, CI SHALL запускаться целиком. Ручной запуск, ежедневная проверка зависимостей и сборка релиза по тегу SHALL выполняться независимо от того, какие файлы изменены.

#### Scenario: Изменена только документация
- **WHEN** открыт pull request в `main`, который меняет только `README.md` и `openspec/specs/distribution/spec.md`
- **THEN** ни Build and Test, ни Dependency Audit не запускаются

#### Scenario: Документация вместе с кодом
- **WHEN** pull request меняет `README.md` и `app/handlers/files.py`
- **THEN** CI запускает тесты, сборку и проверку зависимостей, как для любого изменения кода

#### Scenario: Изменён workflow
- **WHEN** pull request меняет только `.github/workflows/build-exe.yml`
- **THEN** CI запускается: файлы workflow документацией не считаются

#### Scenario: Релиз после изменения документации
- **WHEN** последний коммит перед тегом `v2.3.0` менял только документацию
- **THEN** release.yml по тегу собирает, проверяет и публикует релиз как обычно
