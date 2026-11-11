# Tasks

## 1. Пакет MSI

- [x] 1.1 В `installer/FilePickerAPI.wxs` ставить файл сообщений под именем `FilePickerAPI.messages.$(var.Version).dll` (компонент `MessageFile` постоянный, без явного GUID) и обновить комментарии; проверка — `tests/test_installer.py`
- [x] 1.2 Добавить свойство `NEWERMESSAGEFILE` (`RegistrySearch` по `EventMessageFile` с `Type="file"`, `Bitness="always64"` и `FileSearch` с `MinVersion="$(var.Version).1"`) и условие `NOT NEWERMESSAGEFILE` у компонента `EventSource`; проверка — `tests/test_installer.py`
- [x] 1.3 Обновить `tests/test_installer.py`: имя файла с версией, источник сообщений и постоянство компонентов, поиск зарегистрированного файла и условие регистрации; `pytest tests/test_installer.py` проходит
- [x] 1.4 В `.github/scripts/build-msi.ps1` сверять файл сообщений пакета под именем с версией и падать, если версия DLL сообщений в папке сборки не `X.Y.Z.0` версии пакета; проверка — шаг сборки MSI в CI

## 2. Дымовая проверка

- [x] 2.1 В `.github/scripts/msi-smoke.ps1` проверять при каждом `msiexec`, что PID службы `EventLog` не изменился; на шаге 6 (сервис остановлен) — ещё и что в журнале `msiexec` нет строк о закрытии приложений Restart Manager; проверка — шаг `MSI smoke test` в CI
- [x] 2.2 Сверку файлов установки, `Assert-Registration`, откат и удаление (шаги 1, 6, 11–13) перевести на файлы сообщений с версией в имени: после отката и удаления источник указывает на файл второй версии, файлы обеих версий на месте; проверка — шаг `MSI smoke test` в CI
- [x] 2.3 Обновить строки «Источник событий» и «Файл сообщений» таблицы «Содержимое MSI», раздел «Откат» и шаги 1, 6 и 13 дымовой проверки в `openspec/changes/user-logon-autostart/design.md` со ссылкой на это изменение; `openspec validate --all --strict` проходит

## 3. Проверка

- [ ] 3.1 В CI (`build (5000)`, шаг `MSI smoke test`) шаг 6 проходит без перезапуска `EventLog`, и вся дымовая проверка зелёная
- [x] 3.2 `ruff check app tests scripts`, `ruff format --check app tests scripts`, `pytest` (покрытие 100 %) и `openspec validate --all --strict` проходят

## Workflow follow-up

- Архивировать после `user-logon-autostart`: дельта `user-logon-autostart` с `MODIFIED` требует, чтобы спецификация этой возможности уже была в `openspec/specs/`.
