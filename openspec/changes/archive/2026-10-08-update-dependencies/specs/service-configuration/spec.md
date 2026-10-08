## MODIFIED Requirements

### Requirement: Настройка CORS
Сервис SHALL применять политику CORS ко всем эндпоинтам. Список разрешённых источников SHALL задаваться переменной окружения `CORS_ORIGINS` (или одноимённым параметром в файле `.env` в рабочей директории) в виде строки через запятую; пробелы вокруг элементов и пустые элементы отбрасываются. Если переменная не задана или после разбора список пуст, сервис SHALL разрешать все источники (`*`). Разрешены любые методы и заголовки.

#### Scenario: Все источники по умолчанию
- **WHEN** `CORS_ORIGINS` не задана, а клиент отправляет запрос с заголовком `Origin: http://example.com`
- **THEN** ответ содержит `Access-Control-Allow-Origin: *`

#### Scenario: Ограниченный список источников
- **WHEN** `CORS_ORIGINS=http://localhost:3000,https://example.com`, а клиент отправляет запрос с `Origin: http://localhost:3000`
- **THEN** запрос обрабатывается, и ответ разрешает этот источник

#### Scenario: Пустое значение
- **WHEN** `CORS_ORIGINS` задана пустой строкой
- **THEN** сервис разрешает все источники

## ADDED Requirements

### Requirement: Учётные данные в CORS
Сервис SHALL разрешать передачу учётных данных (куки, HTTP-аутентификация) в CORS только для явного списка источников из `CORS_ORIGINS`. Если разрешены все источники (`*`), ответ MUST NOT содержать `Access-Control-Allow-Credentials` и MUST содержать `Access-Control-Allow-Origin: *`, а не источник запроса.

#### Scenario: Все источники без учётных данных
- **WHEN** `CORS_ORIGINS` не задана, а клиент отправляет запрос с заголовком `Origin: http://example.com`
- **THEN** ответ содержит `Access-Control-Allow-Origin: *` и не содержит `Access-Control-Allow-Credentials`

#### Scenario: Явный список с учётными данными
- **WHEN** `CORS_ORIGINS=http://localhost:3000,https://example.com`, а клиент отправляет запрос с `Origin: http://localhost:3000`
- **THEN** ответ содержит `Access-Control-Allow-Origin: http://localhost:3000` и `Access-Control-Allow-Credentials: true`
