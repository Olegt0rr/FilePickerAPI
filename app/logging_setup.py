"""Журнал сервиса: консоль, файл в профиле пользователя, журнал событий.

Модули pywin32 импортируются лениво, внутри функций: модуль
импортируется на любой ОС, а вне Windows журнал пишется только в
консоль.
"""

import functools
import logging
import queue
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import TextIO

from app.event_tracking import (
    CURRENT_ATTRIBUTE,
    EVENT_ONLY_ATTRIBUTE,
    SENT_ATTRIBUTE,
    UNSENT_ATTRIBUTE,
    UNSENT_RETRY_SECONDS,
    EventId,
    IsCurrent,
    OnSent,
    OnUnsent,
    admit,
    is_tracked,
    unsent_callback,
)

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Логгеры, которые пишут с уровня INFO (uvicorn.access — журнал
# запросов); остальные — с WARNING
INFO_LOGGERS = ("app", "uvicorn.error", "uvicorn.access")

LOG_FILE_NAME = "FilePickerAPI.log"
RETENTION_SECONDS = 30 * 24 * 60 * 60
# Число архивов не ограничивается: срок хранения считает
# getFilesToDelete по возрасту архива
BACKUP_COUNT = 1000
ROTATION_RETRY_SECONDS = 60 * 60

EVENT_SOURCE = "FilePickerAPI"
# Ключ создаёт MSI; без него источника нет, а зарегистрировать его
# пользователь не может
EVENT_SOURCE_KEY = (
    "SYSTEM\\CurrentControlSet\\Services\\EventLog\\Application\\" + EVENT_SOURCE
)
# Предел длины одной вставки ReportEvent
MAX_EVENT_TEXT = 31_839
TRUNCATED_NOTE = "\n… (обрезано, полный текст в FilePickerAPI.log)"
STARTUP_BUFFER_LIMIT = 100
# Ожидание замка обработчика при сбросе журнала перед выходом
FLUSH_LOCK_SECONDS = 1.0
# Ожидание записи очереди журнала событий при сбросе перед выходом и
# при закрытии обработчика
EVENT_FLUSH_SECONDS = 3.0
# Сколько событий держать в очереди, пока ReportEvent не отвечает;
# остальные есть в файле журнала
EVENT_QUEUE_LIMIT = 1000
# Запас очереди для событий запуска и остановки (1000–1006): их не
# вытесняют накопленные события, пока журнал событий не отвечает
EVENT_QUEUE_RESERVE = 10
# Строки консоли, ждущие вывода; остальные есть в файле журнала
CONSOLE_QUEUE_LIMIT = 1000
# Как часто поток консоли без строк проверяет, не закрыт ли обработчик
CONSOLE_POLL_SECONDS = 1.0
# Сколько строк диагностики (отказ ReportEvent, ошибка учёта) ждут
# вывода в файл и консоль, пока они не отвечают; остальные отбрасываются
DIAGNOSTICS_LIMIT = 100

# Обработчики, добавленные setup_logging: повторная настройка их снимает
_handlers: list[logging.Handler] = []


def event_id_for(record: logging.LogRecord) -> int | None:
    """Вернуть код события для записи или None, если события нет.

    Журнал запросов (uvicorn.access), записи с флагом «только в файл»
    (extra={"event_log": False}) и записи с уже учтённым исключением
    событий не дают. Код берётся из extra={"event_id": …}; без него —
    4000 для ERROR и выше, 3001 для WARNING, INFO без кода не идёт.
    Флаг может быть функцией: она вызывается при обработке записи, а не
    при её создании, и ещё раз под замком обработчика журнала событий.
    """
    event_log = getattr(record, "event_log", True)
    if callable(event_log):
        event_log = event_log()
    if record.name == "uvicorn.access" or event_log is False:
        return None
    if record.exc_info and is_tracked(record.exc_info[1]):
        return None
    event_id = getattr(record, "event_id", None)
    if event_id is not None:
        return int(event_id)
    if record.levelno >= logging.ERROR:
        return EventId.UNEXPECTED_ERROR
    if record.levelno >= logging.WARNING:
        return EventId.OTHER_WARNING
    return None


def _service_record(message: str) -> logging.LogRecord:
    """Служебная запись WARNING от имени этого модуля."""
    return logging.makeLogRecord(
        {
            "name": __name__,
            "levelno": logging.WARNING,
            "levelname": "WARNING",
            "msg": message,
        }
    )


def truncate_event_text(text: str) -> str:
    """Обрезать текст события до предела ReportEvent с пометкой.

    Предел считается в единицах UTF-16 (WCHAR), а не в символах
    Python: символ вне BMP (например, эмодзи) занимает две. Срез не
    разрывает суррогатную пару: половинка отбрасывается.
    """
    data = text.encode("utf-16-le", "surrogatepass")
    if len(data) <= MAX_EVENT_TEXT * 2:
        return text
    note_units = len(TRUNCATED_NOTE.encode("utf-16-le")) // 2
    head = data[: (MAX_EVENT_TEXT - note_units) * 2]
    return head.decode("utf-16-le", "ignore") + TRUNCATED_NOTE


# Событие для ReportEvent: уровень, код, текст и что сделать, если оно
# отброшено или не записалось и если записано, и нужно ли оно ещё,
# по обратным вызовам учёта в event_tracking
_Event = tuple[int, int, str, OnUnsent | None, OnSent | None, IsCurrent | None]


class EventLogHandler(logging.Handler):
    """Записи журнала в журнал событий Application.

    До исхода запуска события копятся в буфере (не больше 100, сверх
    этого — одна пометка о числе отброшенных) и отправляются
    send_startup_events() или отбрасываются discard_startup_events().
    После отбрасывания (второй экземпляр, процесс завершается) в журнал
    событий идут только записи исхода (extra={"outcome": True}: 1000,
    1005, 1006; их не отбрасывает и лимит буфера): поток
    проверки прав или создания директории, закончивший работу до
    выхода, не оставит 1003 или 2004 без пары. Ошибка ReportEvent не
    роняет запрос: о серии отказов пишется одна строка в файл журнала,
    а сами записи в нём уже есть.

    ReportEvent вызывается только в потоке-писателе: события встают в
    очередь, и ни один другой поток записи не ждёт. Зависший ReportEvent
    (служба журнала событий не отвечает) держит только писателя, а не
    цикл событий Uvicorn, запуск и запросы. Очередь ограничена
    EVENT_QUEUE_LIMIT; о событиях сверх неё пишется одна пометка, когда
    очередь опустеет. Очередь и писатель — под своим замком, а не под
    замком обработчика.

    Писатель вызывает только ReportEvent. Обратные вызовы учёта об
    исходе (on_sent, on_unsent; они ставят следующее событие о папке)
    идут по порядку в потоке исходов, а строки диагностики (отказ
    ReportEvent, ошибка учёта; они пишутся в файл и консоль) — в своём
    потоке с ограниченной очередью. Запись в зависшую консоль не
    останавливает ни писателя, ни подтверждения событий о папке.
    """

    # Сообщает об исходе записи: события о папке ждут его ответа
    confirms_event_log = True  # event_tracking.CONFIRMS_ATTRIBUTE

    def __init__(self) -> None:
        """Открыть источник событий FilePickerAPI."""
        super().__init__()
        # До открытия источника: обработчик попадает в список logging
        # уже в super().__init__, и logging.shutdown закроет его, даже
        # если источник не открылся
        # События для ReportEvent по порядку и состояние писателя;
        # меняются под self._ready
        self._ready = threading.Condition(threading.Lock())
        self._pending: deque[_Event] = deque()
        self._lost = 0
        self._busy = False
        self._closing = False
        self._writer: threading.Thread | None = None
        # Обратные вызовы об исходе и строки диагностики — каждые в
        # своём потоке; тоже под self._ready
        self._outcomes = _Lane(self, "event-log-outcomes")
        self._diagnostics = _Lane(self, "event-log-diagnostics", DIAGNOSTICS_LIMIT)
        self._source = None
        import win32evtlog

        self._win32evtlog = win32evtlog
        self._source = win32evtlog.RegisterEventSource(None, EVENT_SOURCE)
        # Текст форматируется при записи: буфер не держит ни аргументы,
        # ни трассировки с кадрами стека. Для отброшенного или не
        # записанного события вызывается его on_unsent: первое событие
        # придёт при следующем случае, а посчитанные повторы сохранятся
        self._buffer: list[_Event] | None = []
        self._dropped = 0
        # Буфер отброшен: дальше — только записи исхода
        self._discarded = False
        # Идёт серия отказов ReportEvent: строка о ней уже в журнале
        self._report_failing = False
        # Время и уровень Event Viewer показывает сам
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def handle(self, record: logging.LogRecord) -> bool:
        """Принять запись; без события — не беря замок обработчика.

        Строку об отказе ReportEvent пишет поток исходов, а замок может
        держать поток, который ждёт очередь. Код события считается один
        раз и передаётся в _emit.
        """
        event_id = event_id_for(record)
        if event_id is None:
            return False
        if not self.filter(record):
            return False
        with self.lock:
            self._emit(record, event_id)
        return True

    def emit(self, record: logging.LogRecord) -> None:
        """Поставить событие в очередь, если у записи есть событие."""
        event_id = event_id_for(record)
        if event_id is not None:
            self._emit(record, event_id)

    def _emit(self, record: logging.LogRecord, event_id: int) -> None:
        """Поставить событие в очередь или отложить его в буфер.

        Запись без явного кода идёт через учёт повторов с ключом
        «логгер + шаблон сообщения»: иначе Uvicorn, который пишет о
        каждом некорректном HTTP-запросе из сети, давал бы событие на
        каждое соединение сканера портов.
        """
        # Флаг-функцию handle() вычислил до замка, а журнал событий мог
        # закрыться после этого: под замком она проверяется ещё раз.
        # Это не отказ, запись уже в файле журнала
        event_log = getattr(record, "event_log", True)
        if callable(event_log) and event_log() is False:
            return
        outcome = getattr(record, "outcome", False)
        on_unsent = getattr(record, UNSENT_ATTRIBUTE, None)
        on_sent = getattr(record, SENT_ATTRIBUTE, None)
        is_current = getattr(record, CURRENT_ATTRIBUTE, None)
        if self._discarded and not outcome:
            _notify(on_unsent, 0)
            return
        try:
            message = record.getMessage()
        except Exception:
            _notify(on_unsent, UNSENT_RETRY_SECONDS)
            self.handleError(record)
            return
        # Лимит буфера — до учёта повторов: отброшенная запись не
        # должна занять ключ, иначе её первое событие не придёт никогда.
        # Запись исхода (1000) лимитом не отбрасывается
        if (
            self._buffer is not None
            and len(self._buffer) >= STARTUP_BUFFER_LIMIT
            and not outcome
        ):
            self._dropped += 1
            # Повторы этой записи учтены ещё в event_tracking (track,
            # 2000): без пометки её событие не пришло бы никогда
            _notify(on_unsent, 0)
            return
        note = ""
        if getattr(record, "event_id", None) is None:
            reason = (record.name, str(record.msg))
            note = admit(
                event_id,
                reason,
                # Ключ — шаблон, а текст напоминания — первая запись
                f"{record.name}: {message}",
                record.levelno,
            )
            if note is None:
                return
            on_unsent = unsent_callback(event_id, reason, note)
        try:
            text = self.format(record) + note
        except Exception:
            # Ключ уже занят: без пометки следующий случай считался бы
            # повтором уже записанного события
            _notify(on_unsent, UNSENT_RETRY_SECONDS)
            self.handleError(record)
            return
        event = (record.levelno, event_id, text, on_unsent, on_sent, is_current)
        if self._buffer is not None:
            self._buffer.append(event)
        else:
            self._enqueue([event])

    def send_startup_events(self) -> None:
        """Отправить буфер; дальше события пишутся сразу."""
        with self.lock:
            buffered, self._buffer = self._buffer or [], None
            if self._dropped:
                note = _service_record(
                    f"Отброшено записей при запуске: {self._dropped}, "
                    "они есть в файле журнала"
                )
                buffered.append(
                    (
                        note.levelno,
                        EventId.OTHER_WARNING,
                        self.format(note),
                        None,
                        None,
                        None,
                    )
                )
                self._dropped = 0
            self._enqueue(buffered)

    def discard_startup_events(self) -> None:
        """Отбросить буфер; дальше пишутся только записи исхода.

        Каждая отброшенная запись (и каждый её повтор) уже есть в файле
        журнала; итоговых напоминаний второй экземпляр не пишет.
        """
        with self.lock:
            buffered, self._buffer = self._buffer or [], None
            self._discarded = True
            self._dropped = 0
            for _level, _id, _text, on_unsent, _on_sent, _current in buffered:
                _notify(on_unsent, 0)

    def flush(self) -> None:
        """Дождаться очереди и исходов не дольше EVENT_FLUSH_SECONDS."""
        self.wait_idle(EVENT_FLUSH_SECONDS)

    def wait_idle(self, timeout: float) -> bool:
        """Дождаться очереди, исходов и диагностики не дольше timeout.

        Исходы тоже: обратный вызов об исходе 2000 ставит в очередь
        2001. False — не дождались.
        """
        with self._ready:
            return self._ready.wait_for(
                lambda: (
                    not (self._pending or self._busy)
                    and self._outcomes.idle()
                    and self._diagnostics.idle()
                ),
                timeout,
            )

    def _enqueue(self, items: list[_Event]) -> None:
        """Поставить события в очередь и разбудить писателя.

        Событие, не вставшее в очередь (она полна или обработчик
        закрыт), не записано, как при отказе ReportEvent: очередь полна,
        только пока журнал событий не отвечает. Его on_unsent
        вызывается с UNSENT_RETRY_SECONDS, вне замка очереди. Событиям
        запуска и остановки (1000–1006) сверх лимита оставлен запас
        EVENT_QUEUE_RESERVE: причина завершения процесса не теряется
        за накопленными событиями.
        """
        with self._ready:
            rejected = []
            for item in items:
                limit = EVENT_QUEUE_LIMIT
                if item[1] in _SERVICE_EVENTS:
                    limit += EVENT_QUEUE_RESERVE
                if self._closing or len(self._pending) >= limit:
                    rejected.append(item)
                else:
                    self._pending.append(item)
            if not self._closing:
                self._lost += len(rejected)
            if self._writer is None and items and not self._closing:
                self._writer = threading.Thread(
                    target=self._write, name="event-log-writer", daemon=True
                )
                self._writer.start()
            self._ready.notify_all()
        for _level, _id, _text, on_unsent, _on_sent, _current in rejected:
            _notify(on_unsent, UNSENT_RETRY_SECONDS)

    def _defer(self, callback: Callable[..., None] | None, *args: float) -> None:
        """Сообщить учёту об исходе в потоке исходов, по порядку.

        Ошибка обратного вызова уходит строкой в поток диагностики.
        """
        if callback is not None:
            self._outcomes.put(functools.partial(self._outcome, callback, *args))

    def _outcome(self, callback: Callable[..., None], *args: float) -> None:
        try:
            callback(*args)
        except Exception as e:
            self._diagnose(
                functools.partial(
                    logger.error,
                    "Ошибка учёта исхода события журнала событий",
                    exc_info=e,
                    extra={"event_log": False},
                )
            )

    def _diagnose(self, action: Callable[[], None]) -> None:
        """Вывести строку диагностики в её потоке (сверх лимита нет)."""
        self._diagnostics.put(action)

    def _lost_note(self) -> _Event:
        note = _service_record(
            f"Отброшено событий, пока журнал событий не отвечал: {self._lost}, "
            "они есть в файле журнала"
        )
        self._lost = 0
        return note.levelno, EventId.OTHER_WARNING, self.format(note), None, None, None

    def _write(self) -> None:
        """Писать очередь до закрытия, затем закрыть источник событий.

        Источник закрывает писатель: так ReportEvent не получит
        освобождённый дескриптор, даже если закрытие его не дождалось.
        """
        while True:
            with self._ready:
                self._busy = False
                self._ready.notify_all()
                while not self._pending and not self._lost and not self._closing:
                    self._ready.wait()
                if self._pending:
                    item = self._pending.popleft()
                elif self._lost:
                    item = self._lost_note()
                else:
                    break
                self._busy = True
            self._report(item)
        self._deregister()

    def _report(self, event: _Event) -> None:
        """Записать событие; отказ ReportEvent запрос не роняет.

        Вместо трассировки handleError в stderr на каждое событие —
        одна строка на серию отказов в файл журнала и консоль (не в
        журнал событий), сама запись в файле журнала уже есть. Для
        незаписанного события вызывается on_unsent: следующий такой же
        случай снова даст событие, а число повторов не потеряется. Для
        записанного — on_sent. Обратные вызовы — в потоке исходов
        (_defer), строка — в потоке диагностики (_diagnose). Если
        is_current говорит, что событие уже не нужно (его попытку
        сменил повтор), оно не пишется. Проверка — здесь, в потоке
        писателя прямо перед ReportEvent, а не при постановке в
        очередь: событие могло устареть, пока ждало.
        """
        levelno, event_id, text, on_unsent, on_sent, is_current = event
        if not _still_current(is_current):
            return
        if levelno >= logging.ERROR:
            event_type = self._win32evtlog.EVENTLOG_ERROR_TYPE
        elif levelno >= logging.WARNING:
            event_type = self._win32evtlog.EVENTLOG_WARNING_TYPE
        else:
            event_type = self._win32evtlog.EVENTLOG_INFORMATION_TYPE
        try:
            # Трассировку (exc_info) в текст добавил форматтер
            self._win32evtlog.ReportEvent(
                self._source,
                event_type,
                0,
                event_id,
                None,
                [truncate_event_text(text)],
                None,
            )
        except Exception as e:
            self._defer(on_unsent, UNSENT_RETRY_SECONDS)
            if not self._report_failing:
                self._report_failing = True
                self._diagnose(
                    functools.partial(
                        logger.warning,
                        "Не удалось записать событие %s в журнал событий: %s",
                        event_id,
                        e,
                        extra={"event_log": False},
                    )
                )
            return
        self._report_failing = False
        self._defer(on_sent)

    def close(self) -> None:
        """Дописать очередь (не дольше EVENT_FLUSH_SECONDS) и закрыть.

        С писателем источник закрывает он сам, когда допишет очередь.
        """
        with self._ready:
            self._closing = True
            writer = self._writer
            self._ready.notify_all()
        if writer is None:
            self._deregister()
        else:
            writer.join(EVENT_FLUSH_SECONDS)
        super().close()

    def _deregister(self) -> None:
        source, self._source = self._source, None
        if source is not None:
            self._win32evtlog.DeregisterEventSource(source)


# События запуска и остановки сервиса: им оставлен запас очереди
_SERVICE_EVENTS = frozenset(
    {
        EventId.SERVICE_STARTED,
        EventId.SERVICE_STOPPED,
        EventId.SERVICE_FAILED,
        EventId.BUILD_FOLDER_PERMISSIONS,
        EventId.LOG_FILE_UNAVAILABLE,
        EventId.ALREADY_RUNNING,
        EventId.OTHER_VERSION_RUNNING,
    }
)


class ConsoleHandler(logging.Handler):
    """Консоль (stderr) через свой поток.

    Строка форматируется в вызывающем потоке, а пишет её поток
    консоли: заполненный канал или консоль Windows, где выделен текст
    (QuickEdit), задерживают только его, а не цикл событий Uvicorn,
    запросы и повторные попытки запуска. Пока консоль не отвечает, ждут
    не больше CONSOLE_QUEUE_LIMIT строк, остальные есть в файле журнала.
    Записи «только в журнал событий» сюда не попадают.
    """

    def __init__(self, stream: TextIO) -> None:
        """Запустить поток консоли для stream."""
        super().__init__()
        self._stream = stream
        self._lines: queue.Queue[str | None] = queue.Queue(CONSOLE_QUEUE_LIMIT)
        # Признак закрытия отдельно от маркера в очереди: в полную
        # очередь маркер не встанет, а поток должен выйти и так
        self._stopping = threading.Event()
        self.addFilter(not_event_only)
        self._thread = threading.Thread(
            target=self._run, name="console-log", daemon=True
        )
        self._thread.start()

    def handle(self, record: logging.LogRecord) -> bool:
        """Принять запись без замка: emit консоль не ждёт."""
        if not self.filter(record):
            return False
        self.emit(record)
        return True

    def emit(self, record: logging.LogRecord) -> None:
        """Поставить строку в очередь консоли; полная — строки нет."""
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with suppress(queue.Full):
            self._lines.put_nowait(line)

    def flush(self) -> None:
        """Дождаться вывода очереди не дольше FLUSH_LOCK_SECONDS."""
        deadline = time.monotonic() + FLUSH_LOCK_SECONDS
        while self._lines.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    def close(self) -> None:
        """Остановить поток консоли после уже принятых строк.

        Не ждёт поток: маркер будит его, а если очередь полна и маркер
        не встал, поток выйдет по признаку закрытия, когда выведет
        очередь.
        """
        self._stopping.set()
        with suppress(queue.Full):
            self._lines.put_nowait(None)
        super().close()

    def _run(self) -> None:
        while True:
            try:
                line = self._lines.get(timeout=CONSOLE_POLL_SECONDS)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            try:
                if line is None:
                    return
                with suppress(Exception):
                    self._stream.write(line + "\n")
                    self._stream.flush()
            finally:
                self._lines.task_done()


class _Lane:
    """Действия по порядку в своём демоническом потоке.

    Состояние — под замком обработчика self._ready (его же ждёт
    flush). Поток запускается при первом действии и выходит при
    закрытии обработчика, когда очередь пуста; действие после этого
    запускает новый. limit — сколько действий ждут, остальные
    отбрасываются.
    """

    def __init__(
        self, handler: "EventLogHandler", name: str, limit: int | None = None
    ) -> None:
        self._handler = handler
        self._name = name
        self._limit = limit
        self._actions: deque[Callable[[], None]] = deque()
        self._busy = False
        self._thread: threading.Thread | None = None

    def put(self, action: Callable[[], None]) -> None:
        ready = self._handler._ready  # noqa: SLF001 - общий замок
        with ready:
            if self._limit is not None and len(self._actions) >= self._limit:
                return
            self._actions.append(action)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name=self._name, daemon=True
                )
                self._thread.start()
            ready.notify_all()

    def idle(self) -> bool:
        """Очередь пуста и действие не выполняется (под замком)."""
        return not (self._actions or self._busy)

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def _run(self) -> None:
        handler = self._handler
        ready = handler._ready  # noqa: SLF001 - общий замок
        while True:
            with ready:
                self._busy = False
                ready.notify_all()
                while not self._actions and not handler._closing:  # noqa: SLF001
                    ready.wait()
                if not self._actions:
                    # Под замком: действие, отложенное позже (писатель
                    # дописывает очередь), запустит новый поток
                    self._thread = None
                    return
                action = self._actions.popleft()
                self._busy = True
            action()


def _still_current(is_current: IsCurrent | None) -> bool:
    """Нужно ли событие; ошибка учёта — писать, как и без проверки."""
    if is_current is None:
        return True
    try:
        return is_current()
    except Exception:
        logger.exception(
            "Ошибка учёта исхода события журнала событий", extra={"event_log": False}
        )
        return True


def _notify(callback: Callable[..., None] | None, *args: float) -> None:
    """Сообщить учёту об исходе события.

    Ошибка учёта не выходит из emit и не останавливает писателя: иначе
    она дошла бы до кода, который просто писал в журнал (обработчик
    запроса, отправка буфера).
    """
    if callback is None:
        return
    try:
        callback(*args)
    except Exception:
        logger.exception(
            "Ошибка учёта исхода события журнала событий", extra={"event_log": False}
        )


class LogFileHandler(TimedRotatingFileHandler):
    """Файл журнала с ротацией в полночь и хранением 30 дней.

    Срок хранения считается по возрасту архива, а не по их числу: архив
    60-дневной давности, оставшийся после отпуска, удаляется, хотя
    архивов меньше 30. Файл, открытый читателем без FILE_SHARE_DELETE
    (например, Get-Content -Wait), не переименовать: тогда запись
    продолжается в текущий файл, а ротация повторяется через час.
    """

    def __init__(self, path: Path) -> None:
        """Открыть файл журнала на дозапись."""
        super().__init__(
            path, when="midnight", backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        self._rotation_failed = False
        # Идёт серия отказов записи в файл: 1004 о ней уже записано
        self._write_failing = False
        self._write_failed = False

    def emit(self, record: logging.LogRecord) -> None:
        """Записать строку; без открытого файла — ждать новой попытки.

        Если файл не открылся при ротации, FileHandler.emit пытался бы
        открыть его на каждой записи. До следующей попытки ротации
        записи в файл не идут, консоль и журнал событий их получают.
        Запись о самом отказе файла (file_log=False) и запись «только в
        журнал событий» сюда не пишутся.
        """
        if getattr(record, "file_log", True) is False or not not_event_only(record):
            return
        if self.stream is None and time.time() < self.rolloverAt:
            return
        self._write_failed = False
        super().emit(record)
        if not self._write_failed:
            self._write_failing = False

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: ARG002, N802 - метод базового класса
        """Сообщить об отказе файла журнала одним 1004 на серию отказов.

        Без консоли трассировка handleError в stderr никому не видна,
        поэтому отказ записи, flush или открытия при ротации уходит в
        журнал событий (и консоль), но не в сам неисправный файл.
        """
        self._write_failed = True
        if self._write_failing:
            return
        self._write_failing = True
        logger.warning(
            "Не удалось записать в файл журнала %s: %s",
            self.baseFilename,
            sys.exc_info()[1],
            extra={"event_id": EventId.LOG_FILE_UNAVAILABLE, "file_log": False},
        )

    def getFilesToDelete(self) -> list[str]:  # noqa: N802 - метод базового класса
        """Вернуть архивы старше срока хранения."""
        base = Path(self.baseFilename)
        cutoff = time.time() - RETENTION_SECONDS
        old = []
        with suppress(OSError):
            for archive in base.parent.glob(base.name + ".*"):
                with suppress(OSError):
                    if archive.stat().st_mtime < cutoff:
                        old.append(str(archive))
        return old

    def delete_old_archives(self) -> None:
        """Удалить архивы старше срока хранения.

        Архив, который не удалось удалить (открыт читателем), удаляется
        при следующей очистке.
        """
        for archive in self.getFilesToDelete():
            with suppress(OSError):
                Path(archive).unlink()

    def doRollover(self) -> None:  # noqa: N802 - метод базового класса
        """Переименовать файл с датой в имени и начать новый.

        Свой вместо стандартного: тот при ошибке переименования
        оставлял поток закрытым, не сдвигая время ротации, и каждая
        следующая запись снова пыталась ротировать и терялась.
        """
        if self.stream:
            self.stream.close()
            self.stream = None
        now = int(time.time())
        day = time.localtime(self.rolloverAt - self.interval)
        archive = Path(
            self.rotation_filename(
                self.baseFilename + "." + time.strftime(self.suffix, day)
            )
        )
        # Архив с этой датой уже есть (часы перевели назад, файл
        # ротировал другой экземпляр): не затирать, а дать имя с номером
        candidate, number = archive, 0
        while candidate.exists():
            number += 1
            candidate = archive.with_name(f"{archive.name}.{number}")
        archive = candidate
        current = Path(self.baseFilename)
        try:
            if current.exists():
                current.replace(archive)
        except OSError as e:
            # Сдвиг до открытия: если и оно упадёт, следующие записи не
            # будут пытаться ротировать каждая
            self.rolloverAt = now + ROTATION_RETRY_SECONDS
            self.stream = self._open()
            if not self._rotation_failed:
                self._rotation_failed = True
                self._write_line(
                    f"Не удалось переименовать файл журнала в {archive}: {e}. "
                    "Запись продолжается в текущий файл, "
                    "следующая попытка через час"
                )
            return
        self._rotation_failed = False
        self.delete_old_archives()
        # Новый файл не открылся (квота): повтор через час, а не на
        # каждой записи
        self.rolloverAt = now + ROTATION_RETRY_SECONDS
        self.stream = self._open()
        self.rolloverAt = self.computeRollover(now)

    def _write_line(self, message: str) -> None:
        self.stream.write(self.format(_service_record(message)) + self.terminator)
        self.flush()


def _windows() -> bool:
    return sys.platform == "win32"


def log_file_path() -> Path:
    """Вернуть путь к файлу журнала в %LOCALAPPDATA% пользователя.

    Папку даёт Windows (FOLDERID_LocalAppData), а не переменная
    окружения. Вызов может отказать (E_ACCESSDENIED у процесса другого
    пользователя): тогда исключение, и сервис работает без файла.
    """
    from win32com.shell import shell

    local_appdata = shell.SHGetKnownFolderPath(shell.FOLDERID_LocalAppData, 0, 0)
    return Path(local_appdata) / "FilePickerAPI" / "logs" / LOG_FILE_NAME


def event_source_registered() -> bool:
    """Проверить, зарегистрирован ли источник событий FilePickerAPI."""
    import winreg

    try:
        winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, EVENT_SOURCE_KEY).Close()
    except OSError:
        return False
    return True


def not_event_only(record: logging.LogRecord) -> bool:
    """Фильтр файла и консоли: без записей «только в журнал событий»."""
    return not getattr(record, EVENT_ONLY_ATTRIBUTE, False)


def _add(handler: logging.Handler) -> logging.Handler:
    logging.getLogger().addHandler(handler)
    _handlers.append(handler)
    return handler


def setup_logging(*, console: bool) -> None:
    """Настроить журнал сервиса.

    console — запуск из консоли (или `python -m app`): записи идут в
    stderr. На Windows журнал всегда пишется в файл и, если источник
    FilePickerAPI зарегистрирован (его создаёт MSI), в журнал событий.
    Если файл вести нельзя, сервис работает без него и пишет 1004.
    """
    root = logging.getLogger()
    for handler in _handlers:
        root.removeHandler(handler)
        handler.close()
    _handlers.clear()
    root.setLevel(logging.WARNING)
    for name in INFO_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)

    if console:
        _add(ConsoleHandler(sys.stderr)).setFormatter(formatter)
    if not _windows():
        return
    # Журнал событий раньше файла: 1004 о недоступном файле попадает в
    # буфер стартовых событий
    event_log_error = None
    if event_source_registered():
        try:
            _add(EventLogHandler())
        except Exception as e:
            event_log_error = e
    try:
        path = log_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = LogFileHandler(path)
    except Exception as e:
        logger.warning(
            "Не удалось вести файл журнала: %s",
            e,
            extra={"event_id": EventId.LOG_FILE_UNAVAILABLE},
        )
    else:
        file_handler.setFormatter(formatter)
        file_handler.delete_old_archives()
        _add(file_handler)
    # После файла: без консоли запись об отказе видна только в нём
    if event_log_error is not None:
        logger.warning(
            "Журнал событий недоступен: %s", event_log_error, exc_info=event_log_error
        )


def send_startup_events() -> None:
    """Отправить буфер стартовых событий в журнал событий."""
    for handler in _handlers:
        if isinstance(handler, EventLogHandler):
            handler.send_startup_events()


def discard_startup_events() -> None:
    """Отбросить буфер стартовых событий (второй экземпляр)."""
    for handler in _handlers:
        if isinstance(handler, EventLogHandler):
            handler.discard_startup_events()


def wait_event_log(timeout: float) -> None:
    """Дождаться журнала событий не дольше timeout.

    Очередь, подтверждения и события, которые они ставят: итоговое
    2002, ждущее подтверждения 2000, ляжет в журнал событий раньше
    следующей записи (1001 или 1002).
    """
    deadline = time.monotonic() + timeout
    for handler in _handlers:
        if isinstance(handler, EventLogHandler):
            handler.wait_idle(max(deadline - time.monotonic(), 0))


def flush_handlers() -> None:
    """Сбросить обработчики журнала перед os._exit.

    flush() берёт замок обработчика, а его может держать зависший
    поток (например, запись в переполненный канал консоли). Поэтому
    замок ждём не дольше FLUSH_LOCK_SECONDS, и такой обработчик
    пропускаем: процесс должен завершиться в срок. Журнал событий
    ждёт свою очередь без этого замка: его берёт писатель, когда из
    обратного вызова об исходе события пишет следующее событие о папке.
    """
    for handler in _handlers:
        if isinstance(handler, EventLogHandler):
            with suppress(Exception):
                handler.flush()
            continue
        if not handler.lock.acquire(timeout=FLUSH_LOCK_SECONDS):
            continue
        try:
            with suppress(Exception):
                handler.flush()
        finally:
            handler.lock.release()
