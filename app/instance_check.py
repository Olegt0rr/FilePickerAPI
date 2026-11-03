"""Второй экземпляр: занят ли порт и работает ли на нём File Picker API.

Сервис нужен на машине один: второй процесс (второй пользователь,
повторный запуск, одновременный запуск из Run и прежней задачи) видит,
что на порту уже отвечает File Picker API, и завершается с кодом 0.
Проверка по процессу не подходит: процесс другого пользователя обычный
пользователь открыть не может. Модули pywin32 не используются; вызовы
WinAPI — через app.winapi и только на Windows.
"""

import http.client
import json
import socket
import sys
import threading
import time
from contextlib import suppress
from enum import Enum
from typing import NamedTuple

from app import __version__
from app.event_tracking import EventId

# Заголовок OpenAPI, по которому узнаётся File Picker API
TITLE = "File Picker API"
# Опрос до запуска: первый экземпляр может ещё запускаться
PROBE_SECONDS = 10.0
# Короткая проверка после ошибки привязки к порту
BIND_PROBE_SECONDS = 3.0
PROBE_INTERVAL = 0.5
REQUEST_TIMEOUT = 2.0
CONNECT_TIMEOUT = 0.5
# Ответ /openapi.json больше этого — не наш сервис
MAX_RESPONSE_BYTES = 1024 * 1024
HTTP_OK = 200

# Часы опроса; тесты подменяют их
clock = time.monotonic


class Probe(Enum):
    """Итог опроса порта."""

    FREE = "порт свободен"
    FILE_PICKER = "отвечает File Picker API"
    OTHER = "порт занят другой программой"
    STOPPED = "запрошена остановка"


class ProbeResult(NamedTuple):
    """Итог опроса и версия File Picker API из ответа."""

    status: Probe
    version: str = ""


def _windows() -> bool:
    return sys.platform == "win32"


def _connects(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def port_in_use(host: str, port: int) -> bool:
    """Проверить, слушает ли кто-то порт.

    На Windows — по таблице TCP: отказ соединения с закрытым локальным
    портом там приходит примерно через 2 секунды (повторы SYN), и на
    столько задерживался бы каждый обычный запуск. Если таблицу
    получить не удалось — попыткой соединения, как вне Windows.
    """
    if _windows():
        from app import winapi

        try:
            return winapi.port_owner_pid(port, host) is not None
        except OSError:
            pass
    return _connects(host, port)


def _abort(connection: http.client.HTTPConnection) -> None:
    """Прервать чтение ответа из другого потока."""
    sock = connection.sock
    if sock is not None:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_RDWR)


def probe_openapi(host: str, port: int) -> str | None:
    """Запросить /openapi.json и вернуть версию File Picker API.

    http.client прокси не использует: ни переменные окружения, ни
    системные настройки. None — ответил не File Picker API или никто.
    Весь запрос ограничен REQUEST_TIMEOUT: таймаут сокета действует на
    каждую операцию, и слушатель, отдающий ответ по байту, держал бы
    опрос сколько угодно.
    """
    connection = http.client.HTTPConnection(host, port, timeout=REQUEST_TIMEOUT)
    timer = threading.Timer(REQUEST_TIMEOUT, _abort, (connection,))
    timer.daemon = True
    timer.start()
    try:
        connection.request("GET", "/openapi.json")
        response = connection.getresponse()
        if response.status != HTTP_OK:
            return None
        data = json.loads(response.read(MAX_RESPONSE_BYTES))
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        timer.cancel()
        connection.close()
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict) or info.get("title") != TITLE:
        return None
    return str(info.get("version", ""))


def wait_for_port(
    stop: threading.Event, seconds: float, host: str, port: int
) -> ProbeResult:
    """Опрашивать порт, пока не станет ясно, кто на нём.

    Опрос заканчивается, как только порт свободен или ответил File
    Picker API, прерывается остановкой, а через seconds без ответа
    File Picker API считается, что порт держит другая программа.
    """
    deadline = clock() + seconds
    while True:
        if stop.is_set():
            return ProbeResult(Probe.STOPPED)
        if not port_in_use(host, port):
            return ProbeResult(Probe.FREE)
        version = probe_openapi(host, port)
        # Остановка во время запроса важнее его ответа и срока
        if stop.is_set():
            return ProbeResult(Probe.STOPPED)
        if version is not None:
            return ProbeResult(Probe.FILE_PICKER, version)
        if clock() >= deadline:
            return ProbeResult(Probe.OTHER)
        if stop.wait(PROBE_INTERVAL):
            return ProbeResult(Probe.STOPPED)


def _owner_pid(host: str, port: int) -> int | None:
    from app import winapi

    try:
        return winapi.port_owner_pid(port, host)
    except OSError:
        return None


def second_instance_message(version: str, host: str, port: int) -> tuple[int, str]:
    """Код и текст события второго экземпляра (1005 или 1006).

    PID владельца порта (права на чужой процесс для этого не нужны)
    показывает администратору, кто держит порт, даже если это
    посторонняя программа под видом File Picker API. Вне Windows PID
    не определяется.
    """
    event_id = (
        EventId.ALREADY_RUNNING
        if version == __version__
        else EventId.OTHER_VERSION_RUNNING
    )
    details = f"версия {version or 'не указана'}"
    if _windows():
        pid = _owner_pid(host, port)
        details = f"PID {pid if pid is not None else 'не определён'}, {details}"
    text = f"Сервис уже работает в другом процессе ({details}), этот запуск завершён"
    if event_id == EventId.OTHER_VERSION_RUNNING:
        text += f"; версия этого запуска {__version__}"
    return event_id, text
