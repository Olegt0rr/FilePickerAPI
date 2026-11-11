"""Запуск сервиса File Picker API: рабочий поток и исходы процесса.

Главный поток настраивает журнал и обработчики остановки, запускает
рабочий поток и ждёт исхода, раз в 0,1 с передавая остановке причину
пришедшего POSIX-сигнала (см. stop_signals). Рабочий поток проверяет, не
работает ли сервис уже, создаёт приложение и запускает Uvicorn, а при
ошибке HTTP-сервера пробует снова через 10, 30 и 60 секунд. Исходов
три, первый записанный выигрывает: остановка (событие 1001, код 0),
сбой (1002, код 1) и второй экземпляр (1005 или 1006, код 0). Процесс
завершается через os._exit: зависшее обращение к сетевой папке в
потоке пула не должно держать его. Остановку завершает главный поток;
обработчик срочного сигнала (закрытие консоли, завершение сеанса) ждёт
её конца и не возвращается раньше выхода процесса.

У собранного exe на Windows после проверки второго экземпляра сервис
регистрирует перезапуск после обновления (RegisterApplicationRestart)
и проверяет права на папку сборки в отдельном потоке (событие 1003).

Модуль не импортирует app.api и app.settings на уровне модуля:
настройки читаются только в create_app(), после настройки журнала.
"""

import errno
import getpass
import importlib
import logging
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from enum import Enum
from pathlib import Path

import uvicorn

from app import __version__, event_tracking, instance_check, stop_signals
from app.event_tracking import EventId
from app.instance_check import Probe
from app.logging_setup import (
    discard_startup_events,
    flush_handlers,
    send_startup_events,
    setup_logging,
    wait_event_log,
)
from app.stdio import ensure_std_streams, has_console_window

logger = logging.getLogger(__name__)

# Сервис слушает все интерфейсы на порту 8000; второй экземпляр
# спрашивает первый через локальный адрес
HOST = "0.0.0.0"
PORT = 8000
LOCAL_HOST = "127.0.0.1"
# Паузы перед повторными попытками; после четвёртой неудачи — код 1
RETRY_DELAYS = (10, 30, 60)
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
# Работа дольше часа сбрасывает счётчик попыток
RESET_AFTER_SECONDS = 60 * 60
# Ожидание текущих запросов Uvicorn и рабочего потока при остановке
GRACEFUL_SHUTDOWN_SECONDS = 10
STOP_WAIT_SECONDS = 20
# Срочная остановка: Windows завершит процесс после возврата из
# обработчика, а закрытие консоли ждёт его не дольше 5 секунд
URGENT_WAIT_SECONDS = 3
# Срок срочной остановки от прихода сигнала на любом этапе, включая
# создание скрытого окна и уже идущую обычную остановку: если процесс
# к этому сроку не завершился, обработчик завершает его сам
URGENT_EXIT_SECONDS = 4.5
# Ожидание потока создания директории при остановке
DIRECTORY_WAIT_SECONDS = 1
# Ожидание проверки прав перед последними событиями (1001, итоговое
# 1002): обход папки сборки локальный и обычно короче секунды
PERMISSIONS_WAIT_SECONDS = 5
# Ожидание записи 1003, начатой до закрытия журнала событий для неё
EVENTS_LOCK_SECONDS = 1
# Процесс завершается не позже этого срока после исхода, даже если
# запись в журнал зависла (спецификация: не позже 30 секунд)
EXIT_DEADLINE_SECONDS = 28
# Сколько ждать записи 1002 перед повтором: зависшая запись в журнал
# не должна останавливать повторы
REPORT_WAIT_SECONDS = 5
# Сколько ждать журнал событий перед 1001 и 1002: события о папке,
# ждущие подтверждения, ложатся раньше них
EVENT_LOG_WAIT_SECONDS = 3
# Запас срока сторожа на сброс журнала перед выходом (flush_handlers:
# замки консоли и файла по секунде, очередь журнала событий 3 секунды)
EXIT_RESERVE_SECONDS = 6
# Как часто главный поток, ожидая исход, передаёт остановке причину
# сигнала: её запоминает обработчик в stop_signals
SIGNAL_POLL_SECONDS = 0.1
# Ошибка привязки: asyncio пересоздаёт исключение без winerror, а errno
# на Windows — 10048 (WSAEADDRINUSE)
ADDRESS_IN_USE = frozenset({errno.EADDRINUSE, 10048})
# NameSamCompatible: ДОМЕН\пользователь
NAME_SAM_COMPATIBLE = 2

# Часы и выход процесса; тесты подменяют их
clock = time.monotonic
_exit = os._exit


class Outcome(Enum):
    """Исход процесса."""

    STOP = "остановка"
    FAILED = "сбой"
    SECOND_INSTANCE = "второй экземпляр"

    @property
    def exit_code(self) -> int:
        """Код выхода процесса: 1 только при сбое."""
        return 1 if self is Outcome.FAILED else 0


def _windows() -> bool:
    return sys.platform == "win32"


def _compiled() -> bool:
    """Запущен собранный exe (Nuitka), а не `python -m app`."""
    return "__compiled__" in globals()


def _built_exe_on_windows() -> bool:
    """Собранный exe на Windows: перезапуск и проверка прав — у него.

    У `python -m app` пустая командная строка перезапустила бы
    интерпретатор без аргументов, а «папка exe» — папка интерпретатора.
    """
    return _windows() and _compiled()


def ensure_files_directory() -> None:
    """Создать директорию для файлов, если её ещё нет.

    Ошибка создания не останавливает запуск: если сетевая папка
    недоступна, сервис работает, а GET /files отвечает ошибкой (404,
    403 или 500), пока папка не станет доступна. Ошибка — событие 2004.
    """
    from app.settings import Settings

    # ClassVar: путь берётся без создания настроек и чтения .env
    files_directory = Settings.files_directory
    try:
        Path(files_directory).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(
            "Не удалось создать директорию с файлами %s: %s",
            files_directory,
            event_tracking.describe_error(e),
            extra={"event_id": EventId.DIRECTORY_NOT_CREATED},
        )


def exe_path() -> Path:
    """Путь к exe: у сборки Nuitka — от Windows, иначе интерпретатор.

    В сборке sys.executable указывает на несуществующий python.exe.
    """
    if not _compiled():
        return Path(sys.executable)
    import win32api

    return Path(win32api.GetModuleFileName(None))


def register_restart() -> None:
    """Попросить Restart Manager перезапустить сервис после обновления.

    Только у собранного exe на Windows. Отказ не мешает работе: сервис
    поднимется при следующем входе.
    """
    if not _built_exe_on_windows():
        return
    from app import winapi

    try:
        winapi.register_application_restart()
    except OSError as e:
        logger.warning("Не удалось зарегистрировать перезапуск после обновления: %s", e)


def check_permissions() -> tuple[str, BaseException | None] | None:
    """Проверить права на папку сборки.

    Только у собранного exe на Windows; пишет в журнал вызывающий
    (Service), согласуя запись 1003 с последними событиями процесса.

    Returns:
        Текст события 1003 и ошибку самой проверки (её трассировка
        тоже нужна в событии) или None, если нарушений нет

    """
    if not _built_exe_on_windows():
        return None
    from app import permissions

    try:
        violations = permissions.check_build_folder(exe_path().parent)
        if not violations:
            return None
        return permissions.format_violations(violations), None
    except Exception as e:
        # Права остались непроверенными: администратор должен это видеть
        message = (
            f"Не удалось проверить права на папку сборки: "
            f"{event_tracking.describe_error(e)}"
        )
        return message, e


def _branch() -> str:
    try:
        # Через sys.modules, как и импорт в app.settings, а не атрибут
        # пакета, оставшийся от прежнего импорта
        build_config = importlib.import_module("app._build_config")
    except ModuleNotFoundError as e:
        if e.name != "app._build_config":
            raise
        return "не задан (запуск из исходного кода)"
    return str(getattr(build_config, "BRANCH_NAME", "не задан"))


def _user() -> str:
    if _windows():
        try:
            import win32api

            return str(win32api.GetUserNameEx(NAME_SAM_COMPATIBLE))
        except Exception as e:
            # Без домена, но имя всё равно нужно в тексте события
            logger.debug("GetUserNameEx не удался: %s", e)
    try:
        return getpass.getuser()
    except Exception:
        return "не определён"


def startup_message(*, console: bool) -> str:
    """Текст события 1000: что запущено, где и от чьего имени.

    Имя пользователя нужно, чтобы по 2000 с отказом в доступе было
    видно, у кого нет прав на сетевую папку.
    """
    from app.settings import Settings

    return (
        f"Сервис запущен: версия {__version__}, филиал {_branch()}, "
        f"директория {Settings.files_directory}, exe {exe_path()}, "
        f"пользователь {_user()}, "
        f"запуск {'из консоли' if console else 'без окна'}, порт {PORT}"
    )


class StartupErrors(logging.Filter):
    """Записи uvicorn.error уровня ERROR до старта слушателя.

    Они идут только в файл журнала (и консоль): их текст входит в
    событие 1002, и одна ошибка запуска даёт одно событие. Uvicorn
    пишет ошибку привязки как logger.error(exc), поэтому по записи
    видно и её.
    """

    def __init__(self) -> None:
        """Начать перехват."""
        super().__init__()
        self.active = True
        self.messages: list[str] = []
        self.address_in_use = False

    def filter(self, record: logging.LogRecord) -> bool:
        """Пометить запись «только в файл» и запомнить её текст."""
        if self.active and record.levelno >= logging.ERROR:
            record.event_log = False
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            self.messages.append(text)
            if isinstance(record.msg, OSError) and record.msg.errno in ADDRESS_IN_USE:
                self.address_in_use = True
        return True


class InstanceServer(uvicorn.Server):
    """Uvicorn, сообщающий сервису о старте своего слушателя.

    У Uvicorn нет обратного вызова на старт, а lifespan выполняется до
    привязки к порту. При ошибке привязки и неудачном lifespan
    super().startup() завершается sys.exit, и 1000 не пишется.
    """

    def __init__(self, config: uvicorn.Config, service: "Service") -> None:
        """Создать сервер для попытки запуска."""
        super().__init__(config)
        self.service = service
        self.errors = StartupErrors()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        """Запустить слушатель; после старта — буфер и событие 1000."""
        await super().startup(sockets=sockets)
        if self.started:
            self.errors.active = False
            # 1000 встаёт в конец буфера до первого запроса: события
            # запросов (2000, 4000) его не опередят. Буфер лишь встаёт в
            # очередь журнала событий, ReportEvent вызывает её писатель:
            # зависший ReportEvent не держит цикл событий
            self.service.on_started()
            send_startup_events()


class Service:
    """Состояние экземпляра сервиса: исход, остановка, потоки.

    Исход и причина остановки меняются под одним замком, первый
    записанный исход выигрывает. Рабочий поток сначала занимает исход,
    затем пишет события и только потом будит главный поток: иначе тот
    мог бы завершить процесс раньше записи.
    """

    def __init__(self, *, console: bool) -> None:
        """Создать состояние до запуска потоков."""
        self.console = console
        # request_stop вызывают главный поток (причина сигнала), поток
        # срочной остановки и консольный обработчик Windows в своём
        # потоке; повторного входа под замком нет
        self.lock = threading.Lock()
        # Будит финализацию: поток завершился или срок сократился
        self.changed = threading.Condition(self.lock)
        # decided — исход занят (с этого момента идёт срок сторожа),
        # done — его события записаны
        self.decided = threading.Event()
        self.done = threading.Event()
        # Процесс завершён: выставляется только после подменённого _exit
        # (тесты), в работе процесс к этому моменту уже завершён
        self.finished = threading.Event()
        self.stop_requested = threading.Event()
        self.outcome: Outcome | None = None
        self.stop_reason = ""
        self.stop_error: BaseException | None = None
        # Срок ожидания потоков при остановке; срочный сигнал его
        # сокращает
        self.stop_deadline = float("inf")
        # Пришёл срочный сигнал: поток директории не ждём
        self.urgent = False
        self.server: InstanceServer | None = None
        self.worker: threading.Thread | None = None
        self.worker_finished = False
        self.directory_thread: threading.Thread | None = None
        self.directory_finished = False
        self.permissions_thread: threading.Thread | None = None
        self.permissions_finished = False
        # Последние события записаны: поздний результат проверки прав
        # идёт только в файл журнала, а не после 1001 или 1002. Флаг
        # проверяется и запись 1003 делается под events_lock, и закрытие
        # ждёт уже начатую запись
        self.events_closed = False
        self.events_lock = threading.Lock()
        # Поток записи 1002 перед повтором: может не уложиться в срок
        self.report_thread: threading.Thread | None = None
        # Срок сторожа (по clock); задаёт run(), когда исход занят
        self.exit_deadline: float | None = None
        # Текст 1000; собирается в рабочем потоке до запуска Uvicorn
        self.start_message = f"Сервис запущен: версия {__version__}, порт {PORT}"

    def request_stop(self, reason: str, *, urgent: bool = False) -> None:
        """Запросить остановку; повторный запрос ничего не делает.

        Срочный запрос во время уже идущей остановки сокращает срок
        ожидания потоков до URGENT_WAIT_SECONDS от своего прихода.
        Вызывается из обработчиков сигналов, поэтому в журнал не
        пишет: причина попадёт в событие 1001.
        """
        wait = URGENT_WAIT_SECONDS if urgent else STOP_WAIT_SECONDS
        with self.lock:
            if self.outcome in {None, Outcome.STOP}:
                self.stop_deadline = min(self.stop_deadline, clock() + wait)
                self.urgent = self.urgent or urgent
                self.changed.notify_all()
            if self.outcome is not None:
                return
            self.outcome = Outcome.STOP
            self.stop_reason = reason
            self.stop_requested.set()
            if self.server is not None:
                self.server.should_exit = True
        self.decided.set()
        self.done.set()

    def urgent_stop(self, reason: str) -> None:
        """Срочная остановка; возврат — только после выхода процесса.

        Windows завершит процесс сразу после возврата из обработчика
        (закрытие консоли, WM_ENDSESSION), поэтому обработчик ждёт, пока
        главный поток закончит финализацию и вызовет _exit. Если процесс
        не завершился за URGENT_EXIT_SECONDS от сигнала (зависла запись
        в журнал, главный поток ещё создаёт скрытое окно, шла обычная
        остановка с долгим ожиданием), его завершает сам обработчик:
        закрытие консоли Windows ждёт не дольше 5 секунд. Возвращается,
        только если _exit подменён (тесты).

        Сам запрос идёт в отдельном потоке: он ждёт замок сервиса без
        срока, а отсчёт 4,5 секунды должен идти от сигнала, даже если
        замок занят.
        """
        threading.Thread(
            target=self.request_stop,
            args=(reason,),
            kwargs={"urgent": True},
            name="urgent-stop",
            daemon=True,
        ).start()
        if not self.finished.wait(URGENT_EXIT_SECONDS):
            # Без исхода запрос ещё ждёт замок: это всё равно остановка
            outcome = self.outcome or Outcome.STOP
            _exit(outcome.exit_code)

    def _wait_for(self, finished: Callable[[], bool], seconds: float) -> None:
        """Ждать finished() не дольше seconds и срока остановки.

        Срок остановки проверяется на каждом пробуждении: срочный
        сигнал может сократить его во время ожидания.
        """
        limit = clock() + seconds
        with self.changed:
            while not finished():
                remaining = min(limit, self.stop_deadline) - clock()
                if remaining <= 0:
                    return
                self.changed.wait(remaining)

    def _thread_finished(self, attribute: str) -> None:
        with self.changed:
            setattr(self, attribute, True)
            self.changed.notify_all()

    def _check_permissions(self) -> None:
        """Тело потока проверки прав: 1003 до последних событий."""
        try:
            result = check_permissions()
            if result is not None:
                message, error = result
                with self.events_lock:
                    logger.warning(
                        message,
                        exc_info=error,
                        extra={
                            "event_id": EventId.BUILD_FOLDER_PERMISSIONS,
                            "event_log": self._events_open,
                        },
                    )
        finally:
            self._thread_finished("permissions_finished")

    def _events_open(self) -> bool:
        """Можно ли 1003 в журнал событий.

        Вызывается обработчиком журнала событий при отправке под его
        замком, а последнее событие (1001, 1002) пишется после закрытия
        под тем же замком: запись, задержанная дольше срока ожидания
        _close_events(), уйдёт только в файл.
        """
        return not self.events_closed

    def _close_events(self) -> None:
        """Дождаться проверки прав и закрыть журнал событий для неё.

        Её 1003 должно попасть в журнал событий до последнего события
        (1001 или итогового 1002), а не после него: запись, начатая до
        закрытия, заканчивается раньше, поздняя идёт только в файл.
        """
        if self.permissions_thread is not None:
            self._wait_for(lambda: self.permissions_finished, PERMISSIONS_WAIT_SECONDS)
        # Без замка (запись в журнал зависла) флаг всё равно ставится:
        # процесс должен завершиться в срок
        locked = self.events_lock.acquire(timeout=EVENTS_LOCK_SECONDS)
        self.events_closed = True
        if locked:
            self.events_lock.release()

    def _create_directory(self) -> None:
        """Тело потока создания директории."""
        try:
            ensure_files_directory()
        finally:
            self._thread_finished("directory_finished")

    def claim(self, outcome: Outcome) -> bool:
        """Занять исход; False — исход уже записан (остановка)."""
        with self.lock:
            if self.outcome is not None:
                return False
            self.outcome = outcome
        self.decided.set()
        return True

    def attach_server(self, server: InstanceServer) -> bool:
        """Запомнить сервер попытки; при запрошенной остановке — нет."""
        with self.lock:
            if self.stop_requested.is_set():
                return False
            self.server = server
            return True

    def detach_server(self) -> None:
        """Забыть сервер завершившейся попытки."""
        with self.lock:
            self.server = None

    def on_started(self) -> None:
        """Слушатель запустился: 1000 в конец буфера стартовых событий.

        Буфер ставит в очередь журнала событий InstanceServer.startup.
        Запись исхода (outcome) проходит и в полный буфер.
        """
        if self.stop_requested.is_set():
            return
        message = self.start_message
        if self.report_thread is not None and self.report_thread.is_alive():
            # Зависшее 1002 прошлой попытки может лечь после этого 1000
            message += (
                ". Запись о прошлой неудачной попытке (1002) ещё не "
                "завершена и может появиться в журнале позже"
            )
        logger.info(
            message,
            extra={"event_id": EventId.SERVICE_STARTED, "outcome": True},
        )

    def _prepare_start_message(self) -> None:
        """Собрать текст 1000 вне цикла событий Uvicorn.

        Ошибка здесь не мешает запуску: 1000 пишется с версией и
        причиной.
        """
        try:
            self.start_message = startup_message(console=self.console)
        except Exception as e:
            self.start_message += f" ({e})"

    def work(self) -> None:
        """Тело рабочего потока: ошибка в нём — тоже сбой запуска."""
        try:
            self._work()
        except BaseException as e:
            self._fail("Непредвиденная ошибка запуска сервиса", e)
        finally:
            # Даже если упала сама запись исхода, главный поток не
            # должен ждать вечно; выход без исхода — тоже сбой с 1002
            if self.claim(Outcome.FAILED):
                with suppress(Exception):
                    self._report_failure(
                        "Рабочий поток завершился без исхода, сервис не работает",
                        None,
                    )
            self.done.set()
            self._thread_finished("worker_finished")

    def _work(self) -> None:
        result = instance_check.wait_for_port(
            self.stop_requested, instance_check.PROBE_SECONDS, LOCAL_HOST, PORT
        )
        if result.status is Probe.STOPPED:
            return
        if result.status is Probe.FILE_PICKER:
            self._second_instance(result.version)
            return
        # После проверки второго экземпляра: тот завершается сразу, и
        # перезапускать его после обновления незачем
        register_restart()
        # Обход сотен файлов сборки не задерживает начало приёма
        # запросов, а результат ждёт в буфере стартовых событий
        self.permissions_thread = threading.Thread(
            target=self._check_permissions, name="check-permissions", daemon=True
        )
        self.permissions_thread.start()

        from pydantic import ValidationError

        from app.api import create_app

        try:
            app = create_app()
        except ValidationError as e:
            # Например, FILES_DIRECTORY в .env: путь задаётся только
            # сборкой, и повторные попытки дали бы тот же результат
            self._fail("Ошибка настроек, сервис не запущен", e)
            return
        self._prepare_start_message()
        # mkdir на недоступной UNC-папке ждёт таймаутов SMB: сервис
        # принимает запросы, не дожидаясь его
        self.directory_thread = threading.Thread(
            target=self._create_directory, name="ensure-files-directory", daemon=True
        )
        self.directory_thread.start()
        self._serve_with_retries(app)

    def _serve_with_retries(self, app: object) -> None:
        attempt = 0
        while True:
            started_at = clock()
            server = InstanceServer(
                uvicorn.Config(
                    app,
                    host=HOST,
                    port=PORT,
                    # Uvicorn пишет через обработчики сервиса, а не
                    # переписывает их своей настройкой
                    log_config=None,
                    timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
                ),
                self,
            )
            if not self.attach_server(server):
                return
            error = self._run_server(server)
            if self.stop_requested.is_set():
                self.stop_error = error
                return
            if server.errors.address_in_use:
                # Порт мог занять File Picker API, запущенный
                # одновременно с этим процессом
                result = instance_check.wait_for_port(
                    self.stop_requested,
                    instance_check.BIND_PROBE_SECONDS,
                    LOCAL_HOST,
                    PORT,
                )
                if result.status is Probe.FILE_PICKER:
                    self._second_instance(result.version)
                    return
                if result.status is Probe.STOPPED:
                    return
            if clock() - started_at >= RESET_AFTER_SECONDS:
                attempt = 0
            attempt += 1
            reason = _failure_reason(server, error)
            if attempt >= MAX_ATTEMPTS:
                self._fail(
                    f"HTTP-сервер не работает (попытка {attempt} из "
                    f"{MAX_ATTEMPTS}, попытки исчерпаны): {reason}",
                    error,
                )
                return
            delay = RETRY_DELAYS[attempt - 1]
            reported_at = clock()
            self._report_retry(
                f"HTTP-сервер не работает (попытка {attempt} из {MAX_ATTEMPTS}, "
                f"следующая через {delay} с): {reason}",
                error,
            )
            # Пауза считается от отказа: запись 1002 (не дольше
            # REPORT_WAIT_SECONDS) входит в неё, а не удлиняет её
            if self.stop_requested.wait(max(delay - (clock() - reported_at), 0)):
                return

    def _run_server(self, server: InstanceServer) -> BaseException | None:
        uvicorn_logger = logging.getLogger("uvicorn.error")
        uvicorn_logger.addFilter(server.errors)
        try:
            server.run()
        except BaseException as e:
            # Uvicorn при ошибке запуска вызывает sys.exit: в потоке
            # это SystemExit, а threading.excepthook его не покажет
            return e
        finally:
            uvicorn_logger.removeFilter(server.errors)
            self.detach_server()
        return None

    def _report_failure(self, message: str, error: BaseException | None) -> None:
        """Напоминания о повторах, буфер стартовых событий и 1002."""
        self._final_summary()
        logger.error(
            message,
            exc_info=None if isinstance(error, SystemExit) else error,
            extra={"event_id": EventId.SERVICE_FAILED},
        )

    def _report_retry(self, message: str, error: BaseException | None) -> None:
        """1002 перед повтором: запись ждём не дольше срока.

        Исход ещё не занят, и сторож не запущен: запись, зависшая на
        замке обработчика, иначе остановила бы повторы навсегда. Ошибка
        самой записи, как и раньше, поднимается в рабочем потоке, и
        work() пишет о ней 1002.
        """
        errors: list[BaseException] = []

        def report() -> None:
            try:
                self._report_failure(message, error)
            except BaseException as e:
                errors.append(e)

        thread = threading.Thread(target=report, name="report-failure", daemon=True)
        self.report_thread = thread
        thread.start()
        thread.join(REPORT_WAIT_SECONDS)
        if errors:
            raise errors[0]

    def _fail(self, message: str, error: BaseException | None) -> None:
        """Сбой: 1002 и код 1; при остановке — пометка в 1001."""
        if not self.claim(Outcome.FAILED):
            self.stop_error = error
            return
        self._close_events()
        self._report_failure(message, error)
        self.done.set()

    def _second_instance(self, version: str) -> None:
        """Выход вторым экземпляром: только 1005 или 1006, код 0."""
        if not self.claim(Outcome.SECOND_INSTANCE):
            return
        event_id, message = instance_check.second_instance_message(
            version, LOCAL_HOST, PORT
        )
        discard_startup_events()
        logger.log(
            logging.INFO if event_id == EventId.ALREADY_RUNNING else logging.WARNING,
            message,
            extra={"event_id": event_id, "outcome": True},
        )
        self.done.set()

    def finalize(self) -> None:
        """Остановка: ожидание, напоминания и ровно одно 1001.

        Рабочий поток ждём не дольше 20 секунд от запроса остановки
        (при срочной — 3 секунд): обращения к SMB нельзя прервать
        изнутри процесса. Поток создания директории — не больше секунды
        в пределах того же срока: он демонический, а короткое ожидание
        отличает зависший mkdir от только что начатого. При срочной
        остановке его не ждём, а только проверяем: у Windows срок 5 с.
        Вместе с ожиданием такта учёта повторов (event_tracking.flush,
        до секунды), журнала событий (не позже чем за
        EXIT_RESERVE_SECONDS до срока) и сбросом журнала
        (flush_handlers) — в пределах срока сторожа. При срочной
        остановке такт и журнал событий не ждём, а процесс в срок
        завершает обработчик.
        """
        if self.worker is not None:
            self._wait_for(lambda: self.worker_finished, STOP_WAIT_SECONDS)
        if self.directory_thread is not None:
            self._wait_for(
                lambda: self.directory_finished or self.urgent, DIRECTORY_WAIT_SECONDS
            )
        hung = (self.worker is not None and not self.worker_finished) or (
            self.directory_thread is not None and not self.directory_finished
        )
        self._close_events()
        # Повторы во время мягкой остановки тоже попадают в итог
        self._final_summary()
        message = f"Сервис остановлен: {self.stop_reason}"
        if hung:
            message += (
                ". Обращение к сетевой папке не завершилось, процесс "
                "завершён без его ожидания"
            )
        if self.stop_error is not None:
            message += (
                f". Ошибка при остановке HTTP-сервера: {_describe(self.stop_error)}"
            )
        logger.info(message, extra={"event_id": EventId.SERVICE_STOPPED})

    def _final_summary(self) -> None:
        """Итог учёта перед 1001 или 1002.

        Сначала — буфер стартовых событий (остановка до старта
        слушателя) и исходы уже отправленных событий: отказ записи
        возвращает случай в счётчик, и итог его учтёт. Потом итог
        учёта и снова ожидание: итоговые 2002 и напоминания ложатся
        раньше завершающего события. При срочной остановке такт не ждём,
        а журнал событий — только до срока ожидания потоков.
        """
        send_startup_events()
        self._wait_event_log()
        # При срочной остановке такт не ждём: на всё у Windows 5 секунд
        if self.urgent:
            event_tracking.flush(wait=0)
        else:
            event_tracking.flush()
        self._wait_event_log()

    def _wait_event_log(self) -> None:
        """Дождаться журнала событий перед 1001 или 1002.

        Итоговое 2002 (или 2001), которое ждёт подтверждения 2000, иначе
        легло бы после завершающего события. Ожидание ограничено и
        оставляет запас срока сторожа на сброс журнала. При срочной
        остановке — не дольше срока ожидания потоков
        (URGENT_WAIT_SECONDS от сигнала): у Windows на всё 5 секунд.
        """
        timeout: float = EVENT_LOG_WAIT_SECONDS
        if self.exit_deadline is not None:
            timeout = min(timeout, self.exit_deadline - EXIT_RESERVE_SECONDS - clock())
        if self.urgent:
            timeout = min(timeout, self.stop_deadline - clock())
        wait_event_log(max(timeout, 0))


def _describe(error: BaseException) -> str:
    if isinstance(error, SystemExit):
        return f"Uvicorn завершился с кодом {error.code}"
    return event_tracking.describe_error(error)


def _failure_reason(server: InstanceServer, error: BaseException | None) -> str:
    """Причина ошибки HTTP-сервера для 1002."""
    if server.errors.messages:
        return "\n".join(server.errors.messages)
    if error is None:
        return "HTTP-сервер завершился без запроса остановки"
    return _describe(error)


def run() -> int:
    """Запустить сервис и завершить процесс с кодом исхода.

    Возвращает код выхода, только если _exit подменён (тесты).
    """
    # Журнал в stderr — если он пригоден; способ запуска в 1000 — по
    # окну консоли: без консоли Nuitka открывает stderr на NUL
    stderr_usable = ensure_std_streams()
    setup_logging(console=stderr_usable)
    console = has_console_window(stderr_usable=stderr_usable)
    # Напоминания о повторах приходят и без новых запросов
    event_tracking.start_timer()

    service = Service(console=console)
    # До опроса порта: он длится до 10 секунд, и остановка должна
    # работать и в это время
    stop_signals.install(service.request_stop, service.urgent_stop)
    service.worker = threading.Thread(
        target=service.work, name="service-worker", daemon=True
    )
    service.worker.start()
    # Обработчик сигнала только запоминает причину: остановку вызывает
    # этот поток, а не обработчик посреди decided.wait()
    while not service.decided.wait(SIGNAL_POLL_SECONDS):
        stop_signals.deliver()

    outcome = service.outcome
    service.exit_deadline = clock() + EXIT_DEADLINE_SECONDS
    # Сторож: запись в журнал может зависнуть на замке обработчика
    # (поток, застрявший на записи в канал консоли), а процесс должен
    # завершиться в срок при любом исходе. Поэтому срок идёт с момента,
    # когда исход занят, а не когда записаны его события
    watchdog = threading.Timer(EXIT_DEADLINE_SECONDS, lambda: _exit(outcome.exit_code))
    watchdog.daemon = True
    watchdog.start()
    service.done.wait()
    if outcome is Outcome.STOP:
        service.finalize()
    flush_handlers()
    _exit(outcome.exit_code)
    # Дальше — только при подменённом _exit (тесты): обработчик
    # срочного сигнала ждёт этого, чтобы вернуться
    service.finished.set()
    watchdog.cancel()
    return outcome.exit_code
