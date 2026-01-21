# FilePickerAPI

[![Python Version](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Code Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen.svg)](https://github.com/Olegt0rr/FilePickerAPI)
[![Build and Test](https://github.com/Olegt0rr/FilePickerAPI/actions/workflows/build-exe.yml/badge.svg)](https://github.com/Olegt0rr/FilePickerAPI/actions/workflows/build-exe.yml)

Веб-сервер на базе FastAPI для просмотра списка и загрузки файлов из настроенной директории.

## Возможности

- 🚀 Быстрый и легковесный API на базе FastAPI
- 📁 Просмотр списка файлов из настроенной директории
- ⬇️ Загрузка отдельных файлов
- 🔒 Безопасность: защита от атак обхода директорий
- 🌐 CORS включен для интеграции с фронтендом
- 🪟 Автоматическая сборка Windows исполняемого файла через GitHub Actions

## API Endpoints

### `GET /files`
Получить список всех файлов в настроенной директории, отфильтрованных по размеру и формату.

Только файлы формата .txt возвращаются в ответе. Файлы разделяются на две категории:
- **availableFiles** - файлы формата .txt размером меньше 10 МБ (доступные для импорта)
- **unavailableFiles** - файлы формата .txt размером 10 МБ и больше (не доступные для импорта)

Файлы других форматов полностью игнорируются и не возвращаются в ответе.

Файлы в каждом списке отсортированы по дате создания в порядке убывания (новые файлы первыми).

**Ответ:**
```json
{
  "availableFiles": [
    {
      "id": "example.txt",
      "name": "example.txt",
      "size": 1024,
      "createdAt": "2025-12-15T10:30:29.150000Z"
    }
  ],
  "unavailableFiles": [
    {
      "id": "large_file.txt",
      "name": "large_file.txt",
      "size": 15728640,
      "createdAt": "2025-12-15T10:30:29.100000Z"
    }
  ]
}
```

### `GET /files/{fileId}`
Скачать определенный файл по его ID.

**Параметры:**
- `fileId` - ID файла для загрузки (имя файла)

**Ответ:** Загрузка файла (application/octet-stream)

## Установка

### Запуск из исходного кода

1. Клонируйте репозиторий:
```bash
git clone https://github.com/Olegt0rr/FilePickerAPI.git
cd FilePickerAPI
```

2. Установите зависимости:
```bash
pip install -e .
```

3. Запустите приложение:
```bash
python -m app
```

API будет доступен по адресу `http://localhost:8000`

**💡 Совет:** Откройте `http://localhost:8000/docs` в браузере для доступа к интерактивной документации Swagger UI

### Запуск с Docker

1. Клонируйте репозиторий:
```bash
git clone https://github.com/Olegt0rr/FilePickerAPI.git
cd FilePickerAPI
```

2. Запустите с помощью Docker Compose:
```bash
docker-compose up -d
```

Или соберите и запустите вручную:
```bash
# Собрать образ
docker build -t filepicker-api .

# Запустить контейнер (Linux/Mac)
docker run -d -p 8000:8000 -v $(pwd)/files:/app/files filepicker-api

# Запустить контейнер (Windows PowerShell)
docker run -d -p 8000:8000 -v ${PWD}/files:/app/files filepicker-api

# Запустить контейнер (Windows CMD)
docker run -d -p 8000:8000 -v %cd%/files:/app/files filepicker-api
```

API будет доступен по адресу `http://localhost:8000`

**💡 Совет:** Откройте `http://localhost:8000/docs` в браузере для доступа к интерактивной документации Swagger UI

**Примечания по Docker:**
- Директория `./files` монтируется как том для хранения файлов
- Порт `8000` пробрасывается на хост
- Переменные окружения можно настроить в `docker-compose.yml` или передать через `-e` в `docker run`

### Использование Windows исполняемого файла

1. Скачайте последний `FilePickerAPI.exe` из [Releases](../../releases/latest)
2. Запустите исполняемый файл:
```bash
FilePickerAPI.exe
```

Также можно скачать артефакты из последних [GitHub Actions workflow runs](../../actions)

## Конфигурация

### Директория с файлами

По умолчанию приложение обслуживает файлы из директории `./files`. Вы можете изменить это, установив переменную окружения `FILES_DIRECTORY`:

**Linux/Mac:**
```bash
export FILES_DIRECTORY="/path/to/your/files"
python -m app
```

**Windows:**
```cmd
set FILES_DIRECTORY=C:\path\to\your\files
FilePickerAPI.exe
```

### CORS Origins

По умолчанию CORS включен для всех источников (`*`). Для производственного использования следует ограничить это определенными доменами, установив переменную окружения `CORS_ORIGINS` со списком источников через запятую:

**Linux/Mac:**
```bash
export CORS_ORIGINS="http://localhost:3000,https://yourdomain.com"
python -m app
```

**Windows:**
```cmd
set CORS_ORIGINS=http://localhost:3000,https://yourdomain.com
FilePickerAPI.exe
```

## Документация API (Swagger)

После запуска сервера автоматически становится доступна интерактивная документация API:

### Swagger UI
Откройте в браузере: **`http://localhost:8000/docs`**

Swagger UI предоставляет:
- 📖 Полное описание всех эндпоинтов API
- 🧪 Возможность тестировать API прямо в браузере
- 📝 Просмотр схем запросов и ответов
- 🔍 Детальную информацию о параметрах

### ReDoc
Альтернативная документация доступна по адресу: **`http://localhost:8000/redoc`**

### OpenAPI схема
JSON схема API доступна по адресу: **`http://localhost:8000/openapi.json`**

## Разработка

### Запуск тестов

Проект включает комплексное покрытие тестами с **100% покрытием кода**. Для запуска тестов:

```bash
# Установите зависимости для разработки
pip install -e .[dev]

# Запустите проверки линтера
ruff check app tests
ruff format --check app tests

# Запустите тесты
pytest

# Запустите тесты с отчетом о покрытии (уже включено в конфигурацию pytest.ini)
```

Вся конфигурация управляется через `pyproject.toml`:
- Зависимости и dev-зависимости
- Конфигурация pytest с настройками покрытия
- Конфигурация линтера ruff с правилом "ALL"

Набор тестов покрывает:
- Все конечные точки API (список файлов, загрузка файла)
- Функции безопасности (защита от обхода директорий)
- Конфигурацию CORS
- Обработку исключений (ошибки доступа, исключения безопасности)
- Обработку ошибок и граничные случаи
- Блок выполнения main
- Метаданные файлов, сортировку и специальные символы

### Локальная сборка исполняемого файла

```bash
pip install pyinstaller
pyinstaller --onefile --name FilePickerAPI --add-data "app;app" app/__main__.py
```

Исполняемый файл будет создан в директории `dist/`.

**Примечание**: На Windows используйте точку с запятой (`;`) в параметре `--add-data`, на Linux/Mac используйте двоеточие (`:`):
- Windows: `--add-data "app;app"`
- Linux/Mac: `--add-data "app:app"`

## GitHub Actions

Репозиторий включает несколько workflow GitHub Actions:

### Build and Test (`.github/workflows/build-exe.yml`)

Автоматически:
- Запускает набор тестов на Ubuntu
- Собирает Windows исполняемый файл (только если тесты прошли)
- Загружает отчеты о покрытии тестами
- Загружает Windows исполняемый файл как артефакт

Триггеры workflow:
- Push в ветку main/master
- Pull request в ветку main/master
- Ручной запуск workflow

Артефакты:
- Windows исполняемый файл: хранится 30 дней
- Отчет о покрытии: хранится 7 дней

### Release (`.github/workflows/release.yml`)

Автоматически создаёт релизы с собранными артефактами:
- Срабатывает при создании тега версии (формат: `v*.*.*`)
- Создаёт GitHub Release с описанием
- Собирает Windows исполняемый файл
- Прикрепляет исполняемый файл к релизу

**Создание нового релиза:**

```bash
# Обновите версию в pyproject.toml, если необходимо
# Создайте и отправьте тег
git tag v1.0.0
git push origin v1.0.0
```

После отправки тега workflow автоматически:
1. Создаст GitHub Release с именем "Release v1.0.0"
2. Соберёт Windows исполняемый файл
3. Прикрепит `FilePickerAPI.exe` к релизу

Пользователи смогут скачать релиз с готовым исполняемым файлом со страницы [Releases](../../releases)

## Безопасность

- Защита от обхода директорий: все пути к файлам разрешаются в абсолютные пути и проверяются на то, что они остаются в пределах настроенной директории
- Использует `os.path.commonpath()` для проверки того, что запрошенный путь к файлу не выходит за пределы базовой директории
- Только файлы (не директории) могут быть загружены
- CORS origins можно настроить через переменную окружения для производственного использования (по умолчанию разрешает все источники для разработки)

## Лицензия

MIT