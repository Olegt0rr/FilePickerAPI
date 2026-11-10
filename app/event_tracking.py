"""Учёт ошибок для журнала событий Windows: коды, повторы, папка.

Модуль решает, что из ошибок отправить в журнал событий, и сам к
файловой системе не обращается: обработчики передают ему результат уже
сделанного обращения. В файл журнала пишется каждый случай, а в журнал
событий — первый случай каждого ключа. Повторы считаются и уходят
напоминаниями: такт таймера раз в минуту пишет напоминание через час
после последней записи по ключу, а при остановке ненулевые счётчики
сбрасываются итоговыми напоминаниями. Так фронтенд, опрашивающий список
каждые несколько секунд, не заполняет журнал Application тысячами
одинаковых событий, а ни один повтор не теряется.

Коды постоянные: для каждого в app/messages/FilePickerAPI.mc есть
сообщение, иначе Event Viewer показал бы «Не найдено описание». Коды
только добавляются, смысл существующих не меняется.
"""

import contextvars
import functools
import itertools
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

logger = logging.getLogger(__name__)

# Атрибут исключения, уже записанного в журнал событий: запись с таким
# исключением повторного события не даёт
TRACKED_ATTRIBUTE = "_filepicker_event_tracked"

REMINDER_SECONDS = 60 * 60
TICK_SECONDS = 60
# Не больше 50 активных ключей на код события: иначе отказ доступа к
# папке давал бы своё событие 3000 на каждый новый fileId
MAX_KEYS_PER_EVENT = 50
# Пометка к событию, открывающему ключ переполнения
OVERFLOW_NOTE = (
    f" (видов таких ошибок больше {MAX_KEYS_PER_EVENT}: дальнейшие "
    "объединяются, подробности — в файле журнала)"
)
SLOW_SECONDS = 10.0

# ERROR_PATH_NOT_FOUND: fileId — имя без подпапок, поэтому у пути к
# файлу не найдена сама папка (удалена, отключён диск)
_ERROR_PATH_NOT_FOUND = 3
# Коды Windows, означающие недоступность сетевой папки, а не ошибку
# отдельного файла
NETWORK_WINERRORS = frozenset(
    {
        51,  # ERROR_REM_NOT_LIST
        53,  # ERROR_BAD_NETPATH
        54,  # ERROR_NETWORK_BUSY
        59,  # ERROR_UNEXP_NET_ERR
        64,  # ERROR_NETNAME_DELETED
        65,  # ERROR_NETWORK_ACCESS_DENIED
        67,  # ERROR_BAD_NET_NAME
        121,  # ERROR_SEM_TIMEOUT
        1203,  # ERROR_NO_NET_OR_BAD_PATH
        1219,  # ERROR_SESSION_CREDENTIAL_CONFLICT
        1222,  # ERROR_NO_NETWORK
        1231,  # ERROR_NETWORK_UNREACHABLE
        1232,  # ERROR_HOST_UNREACHABLE
        1311,  # ERROR_NO_LOGON_SERVERS
        2250,  # ERROR_NOT_CONNECTED
    }
)

# Часы учёта; тесты подменяют их вместе с тактом таймера
clock = time.monotonic

# Номер обращения к папке: растёт с каждым begin_access(). Состояние
# папки упорядочивается по началу обращения, а не по моменту записи
# результата: успех обращения, начатого до сбоя, не даёт 2001, а сбой
# обращения, начатого до успеха, не даёт 2000
_access_numbers = itertools.count(1)
# Номера, last_ok_access, публикация сбоя и очередь незаписанных событий
# о папке — под своей короткой блокировкой: без GIL next() и «прочитать,
# изменить, записать» не атомарны. Под ней ничего не логируется, поэтому
# её берут и обратные вызовы EventLogHandler, где бы он их ни вызвал
_access_lock = threading.Lock()
_access: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "directory_access", default=None
)
# Сколько flush по умолчанию ждёт идущий такт таймера: напоминание,
# которое такт уже снял со счётчика, должно успеть записаться до 1001
REMIND_WAIT_SECONDS = 1.0

# Атрибут записи журнала: что сделать, если её событие не записалось.
# Аргумент — через сколько секунд писать событие снова
UNSENT_ATTRIBUTE = "event_unsent"
OnUnsent = Callable[[float], None]
# Атрибут записи журнала: что сделать, когда её событие записано
SENT_ATTRIBUTE = "event_sent"
OnSent = Callable[[], None]
# Атрибут записи журнала: нужно ли её событие до сих пор. Обработчик
# спрашивает непосредственно перед ReportEvent, в том потоке, который
# пишет (не при постановке в буфер или очередь): попытка, которую уже
# сменил повтор, не пишется
CURRENT_ATTRIBUTE = "event_current"
IsCurrent = Callable[[], bool]
# Атрибут записи журнала: только в журнал событий — строка о ней в
# файле журнала и консоли уже есть
EVENT_ONLY_ATTRIBUTE = "event_only"
# Атрибут обработчика: он сообщает об исходе записи (SENT_ATTRIBUTE,
# UNSENT_ATTRIBUTE) — только тогда события о папке ждут его ответа
CONFIRMS_ATTRIBUTE = "confirms_event_log"
# Сколько ждать исхода переданного события о папке, прежде чем писать
# его снова: обработчик мог потерять запись
OUTCOME_TIMEOUT_SECONDS = 5 * 60.0
# После отказа записи событие пишется снова не раньше чем через минуту:
# пока журнал событий отказывает, запросы не ждут ReportEvent каждый
UNSENT_RETRY_SECONDS = 60.0
# Не больше 100 незаписанных событий о папке: пока журнал событий
# отказывает, папка может пропадать и возвращаться на каждом запросе
PENDING_LIMIT = 100


class EventId(IntEnum):
    """Код события в журнале Application (источник FilePickerAPI)."""

    SERVICE_STARTED = 1000
    SERVICE_STOPPED = 1001
    SERVICE_FAILED = 1002
    BUILD_FOLDER_PERMISSIONS = 1003
    LOG_FILE_UNAVAILABLE = 1004
    ALREADY_RUNNING = 1005
    OTHER_VERSION_RUNNING = 1006
    DIRECTORY_UNAVAILABLE = 2000
    DIRECTORY_RESTORED = 2001
    DIRECTORY_STILL_UNAVAILABLE = 2002
    DIRECTORY_SLOW = 2003
    DIRECTORY_NOT_CREATED = 2004
    FILE_ACCESS_ERROR = 3000
    OTHER_WARNING = 3001
    UNEXPECTED_ERROR = 4000


def mark_tracked(exc: BaseException) -> None:
    """Пометить исключение как уже записанное в журнал событий."""
    setattr(exc, TRACKED_ATTRIBUTE, True)


def is_tracked(exc: BaseException | None) -> bool:
    """Проверить, записано ли исключение в журнал событий."""
    return bool(getattr(exc, TRACKED_ATTRIBUTE, False))


@dataclass
class _Entry:
    """Ключ повторов: первое событие уже записано, повторы считаются."""

    level: int
    summary: str
    last_write: float
    count: int = 0
    # Событие ключа не записано (отброшено со стартовым буфером, отказ
    # ReportEvent): случай после retry_at пишется как первый, а счётчик
    # сохраняется
    unsent: bool = False
    retry_at: float = 0.0


@dataclass
class _Unavailable:
    """Состояние недоступной директории."""

    path: str
    reason: Hashable
    description: str
    since: float
    last_write: float
    errors: int
    # Номер самого позднего обращения, закончившегося сбоем
    access: int
    errors_since_write: int = 0


@dataclass(eq=False)
class _Pending:
    """Событие о папке (2000, 2001, 2002), которое ещё не записано."""

    level: int
    event_id: int
    message: str
    # Исключение ответа 5xx до первой попытки записи; после неё —
    # трассировка текстом, без кадров стека в памяти
    exc: BaseException | None = None
    traceback: str = ""
    # Строка уже есть в файле журнала и консоли: дальше — только в
    # журнал событий
    logged: bool = False
    retry_at: float = 0.0
    # Когда передано обработчику (пока исход не известен)
    sent_at: float = 0.0
    # Сколько более ранних событий отброшено сверх PENDING_LIMIT
    dropped_before: int = 0
    # Номер последней попытки: исход и запись прежней не учитываются
    attempt: int = 0

    def detach_exc(self) -> None:
        """Заменить исключение текстом трассировки."""
        if self.exc is not None:
            self.traceback = _format_traceback(self.exc)
            self.exc = None

    def text(self) -> str:
        """Текст события: сообщение и число отброшенных до него."""
        if not self.dropped_before:
            return self.message
        return (
            f"{self.message}. Более ранних событий о папке не записано в "
            f"журнал событий: {self.dropped_before}, они есть в файле журнала"
        )


# События о папке пишутся в журнал событий через одну очередь по
# порядку появления и со своим текстом: так 2000 о смене причины
# сохраняет число ответов прежней причины, а 2001 не опередит своё
# 2000. Обработчику передаётся одно событие за раз: следующее — только
# когда он сообщил, что это записано (_pending_sent). Так порядок
# держится, даже если обработчик пишет позже и в другом потоке
# (стартовый буфер, поток записи). Обратные вызовы приходят и без
# directory_lock, поэтому очередь — под _access_lock


def _confirmed(level: int) -> bool:
    """Проверить, дойдёт ли запись до обработчика с ответом об исходе.

    Иначе исхода не будет и очередь встала бы навсегда: событие о папке
    пишется как обычная запись.
    """
    if not logger.isEnabledFor(level):
        return False
    current: logging.Logger | None = logger
    while current is not None:
        if any(
            getattr(handler, CONFIRMS_ATTRIBUTE, False) and level >= handler.level
            for handler in current.handlers
        ):
            return True
        current = current.parent if current.propagate else None
    return False


def _enqueue(item: _Pending) -> None:
    """Поставить событие в конец очереди (под _access_lock)."""
    pending = _state.pending
    pending.append(item)
    if len(pending) > PENDING_LIMIT:
        # Переданное обработчику не отбрасывается: его исход ещё придёт
        first = 1 if pending[0] is _state.in_flight else 0
        # Самое старое остаётся только в файле журнала, а число таких
        # укажет следующее. Срок повтора переходит к нему же: отброс не
        # ускоряет вызовы ReportEvent
        dropped = pending[first]
        del pending[first]
        following = pending[first]
        following.dropped_before += dropped.dropped_before + 1
        following.retry_at = max(following.retry_at, dropped.retry_at)


def _pending_current(item: _Pending, attempt: int) -> bool:
    """Нужна ли ещё эта попытка записи события о папке.

    Попытку сменяет повтор после OUTCOME_TIMEOUT_SECONDS: если обе
    дождутся записи (например, в стартовом буфере), прежняя не пишется,
    иначе её копия легла бы после следующего события.
    """
    with _access_lock:
        return item.attempt == attempt and item in _state.pending


def _pending_sent(item: _Pending, attempt: int) -> None:
    """Событие о папке записано: передать обработчику следующее.

    Засчитывается только последняя попытка: иначе следующее событие
    могло бы записаться раньше неё, если она уже прошла проверку
    _pending_current в другом потоке. Если записалась и прежняя, 2000
    будет в журнале событий дважды подряд, но порядок не нарушится.
    """
    with _access_lock:
        if item.attempt != attempt:
            return
        item.exc = None
        if item in _state.pending:
            _state.pending.remove(item)
        if _state.in_flight is item:
            _state.in_flight = None
        # Идущий _drain (запись в этом же потоке) продолжит сам
        idle = not _state.draining
    if idle:
        _drain(clock(), force=False)


def _pending_unsent(item: _Pending, attempt: int, retry_after: float) -> None:
    """Событие о папке не записалось: первое в очереди до повтора.

    Отказ прежней попытки не учитывается: идёт уже новая.
    """
    retry_at = clock() + retry_after
    item.detach_exc()
    with _access_lock:
        if item.attempt != attempt:
            return
        item.retry_at = retry_at
        if _state.in_flight is item:
            _state.in_flight = None


def _next_pending(now: float, *, force: bool) -> _Pending | None:
    """Первое событие, если его пора передать (под _access_lock)."""
    pending = _state.pending
    in_flight = _state.in_flight
    if in_flight is not None:
        if now - in_flight.sent_at < OUTCOME_TIMEOUT_SECONDS:
            return None
        # Исход так и не пришёл (например, поток записи завис): событие
        # пишется снова, иначе очередь встала бы навсегда
        _state.in_flight = None
    if not pending or (not force and now < pending[0].retry_at):
        return None
    head = _state.in_flight = pending[0]
    head.sent_at = now
    head.attempt += 1
    return head


def _drain(now: float, *, force: bool) -> None:
    """Передать обработчику ждущие события о папке по порядку.

    Передаёт первое, если обработчик не занят предыдущим и срок повтора
    наступил (с force — без срока). Если исход ещё не известен или
    событие не записалось, остальные ждут за ним. Одновременно идёт
    один _drain: другой поток только ставит событие в очередь.
    """
    with _access_lock:
        if _state.draining:
            return
        _state.draining = True
    while True:
        with _access_lock:
            head = _next_pending(now, force=force)
            if head is None:
                _state.draining = False
                return
            first_attempt = not head.logged
            head.logged = True
            outcome = {
                "on_unsent": functools.partial(_pending_unsent, head, head.attempt),
                "on_sent": functools.partial(_pending_sent, head, head.attempt),
                "is_current": functools.partial(_pending_current, head, head.attempt),
            }
        if first_attempt:
            # Одна запись: файл журнала, консоль и журнал событий
            _log(
                head.level,
                head.event_id,
                head.text(),
                head.exc,
                **outcome,
            )
        else:
            head.detach_exc()
            text = head.text()
            if head.traceback:
                text += "\n" + head.traceback
            _log(
                head.level,
                head.event_id,
                text,
                **outcome,
                event_only=True,
            )
        with _access_lock:
            if _state.in_flight is head or head in _state.pending:
                # Исход ещё не известен — _pending_sent продолжит; не
                # записалось — такт после срока повтора
                _state.draining = False
                return


def _format_traceback(exc: BaseException) -> str:
    return logging.Formatter().formatException((type(exc), exc, exc.__traceback__))


def _directory_event(
    level: int,
    event_id: int,
    message: str,
    exc: BaseException | None = None,
) -> None:
    """Записать событие о папке (под directory_lock).

    Если ждущих нет, это одна запись. Иначе строка в файл журнала и
    консоль идёт сразу, а в журнал событий — за ждущими.
    """
    if not _confirmed(level):
        _log(level, event_id, message, exc)
        return
    item = _Pending(level, event_id, message, exc)
    with _access_lock:
        behind = bool(_state.pending) or _state.draining
        item.logged = behind
        _enqueue(item)
    if behind:
        _log(level, None, message, exc)
        item.detach_exc()
    _drain(clock(), force=False)


class _State:
    """Состояние учёта (пул потоков).

    Ключи повторов меняются под lock, и под ним в журнал не пишется:
    EventLogHandler вызывает admit под своей блокировкой, и запись под
    lock дала бы взаимную блокировку. Состояние директории меняется
    под directory_lock, и запись о нём идёт под ним же: так 2001 не
    опередит 2000. EventLogHandler directory_lock не берёт. Сбой
    публикуется в directory ещё и под блокировкой номеров обращений.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.directory_lock = threading.Lock()
        # Напоминания такта и итог flush не пишутся одновременно
        self.remind_lock = threading.Lock()
        self.keys: dict[int, dict[Hashable, _Entry]] = {}
        # Ключ переполнения на код: только число ошибок, без причин, —
        # память постоянна при любом потоке новых fileId
        self.overflow: dict[int, _Entry] = {}
        self.directory: _Unavailable | None = None
        # Незаписанные события о папке: такт таймера пишет их снова не
        # раньше срока повтора, flush — сразу
        self.pending: deque[_Pending] = deque()
        # Событие о папке, переданное обработчику, исход которого ещё
        # не известен; идёт ли _drain
        self.in_flight: _Pending | None = None
        self.draining = False
        # Номер самого позднего успешного обращения к папке
        self.last_ok_access = 0
        self.timer: threading.Thread | None = None
        self.timer_stop = threading.Event()


_state = _State()


def admit(
    event_id: int, reason: Hashable, summary: str, level: int = logging.WARNING
) -> str | None:
    """Учесть случай ошибки и решить, писать ли событие.

    Returns:
        None — повтор, событие не пишется; "" — событие пишется как
        есть; иначе — пометка к событию о том, что видов ошибок этого
        кода больше лимита и дальнейшие объединяются

    """
    now = clock()
    with _state.lock:
        keys = _state.keys.setdefault(event_id, {})
        entry = keys.get(reason)
        if entry is not None:
            return _repeat(entry, "", now)
        if len(keys) < MAX_KEYS_PER_EVENT:
            keys[reason] = _Entry(level, summary, now)
            return ""
        overflow = _state.overflow.get(event_id)
        if overflow is not None:
            return _repeat(overflow, OVERFLOW_NOTE, now)
        _state.overflow[event_id] = _Entry(
            level,
            f"ошибки с кодом {event_id} сверх {MAX_KEYS_PER_EVENT} видов "
            "(подробности — в файле журнала)",
            now,
        )
    return OVERFLOW_NOTE


def _repeat(entry: _Entry, note: str, now: float) -> str | None:
    """Повтор ключа: счёт, а если первое событие отброшено — оно."""
    if entry.unsent and now >= entry.retry_at:
        entry.unsent = False
        entry.last_write = now
        return note
    entry.count += 1
    return None


def mark_unsent(
    event_id: int, reason: Hashable | None, retry_after: float = 0.0
) -> None:
    """Отметить, что событие ключа так и не записано.

    Следующий случай ключа снова даст событие, а этот случай и
    повторы, посчитанные до того, не теряются: они уйдут напоминанием.
    reason=None — ключ переполнения этого кода. retry_after — через
    сколько секунд случай снова даст событие.
    """
    retry_at = clock() + retry_after
    with _state.lock:
        if reason is None:
            entry = _state.overflow.get(event_id)
        else:
            entry = _state.keys.get(event_id, {}).get(reason)
        if entry is not None:
            entry.unsent = True
            entry.retry_at = retry_at
            entry.count += 1


def unsent_callback(event_id: int, reason: Hashable, note: str) -> OnUnsent:
    """Что сделать, если событие, пропущенное admit, не запишется."""
    return functools.partial(
        mark_unsent, event_id, None if note == OVERFLOW_NOTE else reason
    )


def _restore_count(
    entry: _Entry, count: int, _retry_after: float, *, unsent: bool
) -> None:
    """Напоминание не записано: повторы вернуть в счётчик.

    Если и первое событие ключа не записано, оно снова ждёт записи.
    """
    with _state.lock:
        entry.count += count
        entry.unsent = entry.unsent or unsent


def _log(  # noqa: PLR0913 - поля одной записи
    level: int,
    event_id: int | None,
    message: str,
    exc: BaseException | None = None,
    *,
    on_unsent: OnUnsent | None = None,
    on_sent: OnSent | None = None,
    is_current: IsCurrent | None = None,
    event_only: bool = False,
) -> None:
    """Записать строку; без кода — только в файл журнала.

    on_unsent вызывает EventLogHandler, если событие не записалось
    (отказ ReportEvent или форматтера, отброшенный стартовый буфер):
    так первое событие ключа, событие о папке или число повторов не
    теряются; on_sent — когда событие записано. event_only — только в
    журнал событий: строка в файле журнала и консоли уже есть.
    is_current — нужно ли событие до сих пор (спрашивается перед
    ReportEvent).
    """
    extra: dict[str, object] = (
        {"event_log": False} if event_id is None else {"event_id": event_id}
    )
    if event_id is not None and on_unsent is not None:
        extra[UNSENT_ATTRIBUTE] = on_unsent
    if event_id is not None and on_sent is not None:
        extra[SENT_ATTRIBUTE] = on_sent
    if event_id is not None and is_current is not None:
        extra[CURRENT_ATTRIBUTE] = is_current
    if event_only:
        extra[EVENT_ONLY_ATTRIBUTE] = True
    logger.log(level, "%s", message, exc_info=exc, extra=extra)


def track(
    event_id: int,
    reason: Hashable,
    message: str,
    *,
    level: int = logging.WARNING,
    exc: BaseException | None = None,
) -> None:
    """Записать случай ошибки с учётом повторов.

    В файл журнала — всегда, в журнал событий — по учёту повторов.
    Исключение помечается как учтённое.
    """
    note = admit(event_id, reason, message, level)
    if note is None:
        _log(level, None, message, exc)
    else:
        _log(
            level,
            event_id,
            message + note,
            exc,
            on_unsent=unsent_callback(event_id, reason, note),
        )
    if exc is not None:
        mark_tracked(exc)


def _winerror_description(error: OSError) -> str:
    """Системное описание кода ошибки Windows (FormatMessage)."""
    try:
        import win32api

        return win32api.FormatMessage(error.winerror).strip()
    except Exception:
        return error.strerror or ""


def describe_error(error: BaseException) -> str:
    """Описать ошибку для журнала.

    Для ошибки ОС с кодом Windows — путь, код и его системное описание:
    по коду 53 администратор сразу видит «Не найден сетевой путь».
    """
    winerror = getattr(error, "winerror", None)
    if not isinstance(error, OSError):
        return f"{type(error).__name__}: {error}"
    if winerror is None:
        return str(error)
    text = f"код ошибки Windows {winerror} ({_winerror_description(error)})"
    return f"{error.filename}: {text}" if error.filename else text


def is_directory_error(error: BaseException) -> bool:
    """Проверить, означает ли ошибка недоступность папки с файлами.

    Это сетевой код Windows или ERROR_PATH_NOT_FOUND.
    """
    winerror = getattr(error, "winerror", None)
    return winerror in NETWORK_WINERRORS or winerror == _ERROR_PATH_NOT_FOUND


def _reason(error: BaseException) -> Hashable:
    # Коды Windows и errno — разные пространства: WinError 5 (отказ в
    # доступе) и errno 5 (EIO) — разные причины
    winerror = getattr(error, "winerror", None)
    if winerror:
        return ("winerror", winerror)
    errno = getattr(error, "errno", None)
    if errno:
        return ("errno", errno)
    return type(error).__name__


def _server_error(error: BaseException, status: int | str) -> BaseException | None:
    """Исключение для трассировки в журнале: только при ответе 5xx.

    Строковый статус — оборванный ответ, это тоже сбой сервера.
    """
    if isinstance(status, str) or status >= 500:  # noqa: PLR2004 - класс 5xx
        return error
    return None


def directory_error(
    directory: str | Path, error: BaseException, status: int | str
) -> None:
    """Учесть недоступность директории (2000 при переходе).

    В файл журнала пишется каждый ошибочный ответ, в журнал событий —
    только переход в «недоступна» и смена причины; дальше — 2002 по
    такту таймера и 2001 при восстановлении. При смене причины в 2000
    указаны повторы прежней с прошлой записи. Для ответа 5xx запись
    идёт с трассировкой исходной ошибки.
    """
    now = clock()
    access = _current_access()
    # Причина — код ошибки ОС, а код ответа только в тексте: иначе
    # чередование ошибки списка (500) и обрыва скачивания, а также 200 и
    # 206 (докачка по Range) при одной ошибке давало бы 2000 на каждое
    # переключение
    reason = _reason(error)
    description = describe_error(error)
    message = (
        f"Папка с файлами {directory} недоступна: {description}. "
        f"Ответ клиенту: {status}"
    )
    exc = _server_error(error, status)
    # Запись — под той же блокировкой, что и решение: иначе 2001 из
    # параллельного успешного запроса мог бы опередить это 2000
    with _state.directory_lock:
        # Проверка last_ok_access и публикация сбоя — под той же
        # блокировкой номеров, под которой directory_ok обновляет
        # last_ok_access и смотрит на состояние: иначе успех, пришедший
        # между ними, не увидел бы сбоя, и папка осталась бы недоступной
        with _access_lock:
            state = _state.directory
            stale = state is None and access < _state.last_ok_access
            if state is None and not stale:
                _state.directory = _Unavailable(
                    str(directory),
                    reason,
                    description,
                    now,
                    now,
                    errors=1,
                    access=access,
                )
        if stale:
            # Обращение начато до другого, уже успешного: папка
            # доступна, ошибка — только в файл журнала
            _log(logging.WARNING, None, message, exc)
            mark_tracked(error)
            return
        changed = state is None
        if state is not None:
            changed, message = _update_unavailable(
                state, reason, description, access, now, message
            )
        if changed:
            _directory_event(
                logging.WARNING, EventId.DIRECTORY_UNAVAILABLE, message, exc
            )
        else:
            _log(logging.WARNING, None, message, exc)
    mark_tracked(error)


def _update_unavailable(  # noqa: PLR0913, PLR0917 - поля одной ошибки
    state: _Unavailable,
    reason: Hashable,
    description: str,
    access: int,
    now: float,
    message: str,
) -> tuple[bool, str]:
    """Учесть ошибку уже недоступной папки (под directory_lock).

    Возвращает, писать ли 2000 о смене причины, и текст записи.
    """
    # Ошибка обращения, начатого раньше последнего сбоя, — повтор:
    # причину она не меняет, иначе более старая вытеснила бы новую
    new_reason = state.reason != reason and access >= state.access
    state.access = max(state.access, access)
    state.errors += 1
    if not new_reason:
        state.errors_since_write += 1
        return False, message
    if state.errors_since_write:
        # Текст фиксируется сейчас: если 2000 не запишется, оно ждёт
        # повтора с этим числом и прежней причиной
        message += (
            ". До смены причины ошибочных ответов с прошлой записи: "
            f"{state.errors_since_write} ({state.description})"
        )
    state.reason = reason
    state.description = description
    state.last_write = now
    state.errors_since_write = 0
    return True, message


def begin_access() -> int:
    """Начать обращение к папке: номер для текущего запроса.

    Вызывается в начале запроса (middleware в app.api) и перед чтением
    тела файла: directory_ok и directory_error в этом контексте берут
    номер отсюда. Без неё — номер момента вызова.
    """
    with _access_lock:
        access = next(_access_numbers)
    _access.set(access)
    return access


def _current_access() -> int:
    access = _access.get()
    if access is not None:
        return access
    with _access_lock:
        return next(_access_numbers)


def directory_ok() -> None:
    """Учесть успешное обращение: после недоступности — событие 2001.

    Успех обращения, начатого до последнего сбоя, папку доступной не
    делает: сбой мог случиться уже после него.
    """
    access = _current_access()
    # Под короткой блокировкой номеров, а не directory_lock: пока папка
    # доступна, успешные запросы не ждут записи о чужой ошибке. Сбой,
    # опубликованный до этого, виден здесь; опубликованный после — уже
    # видит новый last_ok_access
    with _access_lock:
        _state.last_ok_access = max(_state.last_ok_access, access)
        unavailable = _state.directory is not None
    if not unavailable:
        return
    with _state.directory_lock:
        state = _state.directory
        if state is None or access < state.access:
            return
        _state.directory = None
        message = (
            f"Папка с файлами {state.path} снова доступна: была недоступна "
            f"{format_duration(clock() - state.since)}, "
            f"ошибочных ответов: {state.errors}"
        )
        # Незаписанное 2000 этой недоступности ждёт в очереди раньше
        _directory_event(logging.INFO, EventId.DIRECTORY_RESTORED, message)


def file_error(
    directory: str | Path, file_id: str, error: OSError, status: int | str
) -> None:
    """Учесть ошибку доступа к скачиваемому файлу.

    Сетевой код Windows или ERROR_PATH_NOT_FOUND — недоступность
    директории (2000), прочие ошибки — 3000 с ключом «код ошибки +
    fileId». Для ответа 5xx запись идёт с трассировкой исходной ошибки.
    """
    if is_directory_error(error):
        directory_error(directory, error, status)
        return
    track(
        EventId.FILE_ACCESS_ERROR,
        (_reason(error), file_id),
        f"Ошибка доступа к файлу {file_id!r} в папке {directory}: "
        f"{describe_error(error)}. Ответ клиенту: {status}",
        exc=_server_error(error, status),
    )
    mark_tracked(error)


def check_slow(route: str, started: float, *, size: int | None = None) -> None:
    """Записать 2003, если обработка заняла больше 10 секунд."""
    elapsed = clock() - started
    if elapsed <= SLOW_SECONDS:
        return
    message = f"Медленный ответ сетевой папки: {route} за {elapsed:.1f} с"
    if size is not None:
        # Время передачи медленному клиенту тоже входит в замер: по
        # размеру видно, сеть клиента это или папка
        message += f", размер файла {size} байт"
    track(EventId.DIRECTORY_SLOW, route, message)


def format_duration(seconds: float) -> str:
    """Длительность для текста события: «1 ч 5 мин», «3 мин 20 с»."""
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs} с"
    return f"{secs} с"


def _remind_or_close(
    entry: _Entry,
    event_id: int,
    now: float,
    reminders: list[tuple[int, int, str, OnUnsent]],
    *,
    final: bool,
) -> bool:
    """Добавить напоминание по ключу в reminders, если пора.

    Возвращает True, если ключ закрывается: час без повторов.
    """
    if not final and now - entry.last_write < REMINDER_SECONDS:
        return False
    if entry.count:
        count, entry.count, entry.last_write = entry.count, 0, now
        # Напоминание записано — отброшенное первое событие уже не
        # нужно; если не запишется, _restore_count вернёт и его
        unsent, entry.unsent = entry.unsent, False
        reminders.append(
            (
                entry.level,
                event_id,
                f"Повторялось ещё {count} раз с прошлой записи: {entry.summary}",
                functools.partial(_restore_count, entry, count, unsent=unsent),
            )
        )
        return False
    return not final


def _still_unavailable(state: _Unavailable, now: float, count: int) -> str:
    """Текст 2002 о недоступной папке."""
    return (
        f"Папка с файлами {state.path} по-прежнему недоступна "
        f"({format_duration(now - state.since)}): {state.description}. "
        f"Ошибочных ответов с прошлой записи: {count}"
    )


def _remind(*, final: bool, wait: float | None = None) -> None:
    """Напоминания по ключам и 2002 о недоступной папке.

    wait — сколько ждать состояние папки, если его держит другой поток
    (такт, запрос), который пишет событие о ней; None — без предела.
    Не дождались при остановке — итоговое 2002 пишется по состоянию без
    блокировки и мимо очереди: дописать его будет уже некому. Ждущие в
    очереди события о папке тогда остаются только в файле журнала.
    """
    now = clock()
    reminders: list[tuple[int, int, str, OnUnsent]] = []
    with _state.lock:
        for event_id, entries in _state.keys.items():
            closed = [
                reason
                for reason, entry in entries.items()
                if _remind_or_close(entry, event_id, now, reminders, final=final)
            ]
            for reason in closed:
                del entries[reason]
        closed = [
            event_id
            for event_id, entry in _state.overflow.items()
            if _remind_or_close(entry, event_id, now, reminders, final=final)
        ]
        for event_id in closed:
            del _state.overflow[event_id]
    for level, event_id, message, on_unsent in reminders:
        _log(level, event_id, message, on_unsent=on_unsent)
    if not _state.directory_lock.acquire(timeout=-1 if wait is None else wait):
        # Чтение без блокировки: процесс останавливается, а поток,
        # который её держит, только пишет событие о той же папке
        state = _state.directory
        if final and state is not None:
            _log(
                logging.WARNING,
                EventId.DIRECTORY_STILL_UNAVAILABLE,
                _still_unavailable(state, now, state.errors_since_write),
            )
        return
    try:
        # Незаписанные события о папке — после срока повтора, при
        # остановке все сразу
        _drain(now, force=final)
        state = _state.directory
        if state is not None and (
            final
            or (state.errors_since_write and now - state.last_write >= REMINDER_SECONDS)
        ):
            count, state.errors_since_write = state.errors_since_write, 0
            state.last_write = now
            _directory_event(
                logging.WARNING,
                EventId.DIRECTORY_STILL_UNAVAILABLE,
                _still_unavailable(state, now, count),
            )
    finally:
        _state.directory_lock.release()


def tick() -> None:
    """Такт таймера: напоминания через час после последней записи.

    Под remind_lock до конца записи: flush ждёт такт, который уже снял
    счётчики, но ещё не записал напоминания.
    """
    with _state.remind_lock:
        _remind(final=False)


def flush(*, wait: float = REMIND_WAIT_SECONDS) -> None:
    """Итог при остановке: ненулевые счётчики и недоступная папка.

    Вызывается перед событием остановки (1001) или ошибки (1002), в
    том числе перед повтором запуска, поэтому таймер не
    останавливает. Идущий такт и запись события о папке другим потоком
    в целом ждёт не дольше wait секунд: при срочной остановке, когда
    процесс вот-вот завершат, — wait=0. Сам EventLogHandler здесь пишет
    синхронно под своей блокировкой: зависший ReportEvent задержит
    любую запись, и flush, и следующую за ним 1001.
    """
    deadline = time.monotonic() + wait
    acquired = _state.remind_lock.acquire(timeout=wait)
    try:
        _remind(final=True, wait=max(0.0, deadline - time.monotonic()))
    finally:
        if acquired:
            _state.remind_lock.release()


def _run_timer(stop: threading.Event) -> None:
    while not stop.wait(TICK_SECONDS):
        try:
            tick()
        except Exception:
            logger.exception("Ошибка такта учёта повторов", extra={"event_log": False})


def start_timer() -> None:
    """Запустить демонический такт таймера раз в минуту (один раз)."""
    with _state.lock:
        if _state.timer is not None:
            return
        _state.timer = threading.Thread(
            target=_run_timer,
            args=(_state.timer_stop,),
            name="event-tracking-timer",
            daemon=True,
        )
        _state.timer.start()


def reset() -> None:
    """Сбросить учёт и остановить таймер (тесты)."""
    global _state  # noqa: PLW0603 - новое состояние вместо старого
    old = _state
    old.timer_stop.set()
    _state = _State()
    _access.set(None)
