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

import logging
import threading
import time
from collections.abc import Hashable
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
    # Первое событие ключа отброшено вместе со стартовым буфером:
    # следующий случай пишется как первый, а счётчик сохраняется
    unsent: bool = False


@dataclass
class _Unavailable:
    """Состояние недоступной директории."""

    path: str
    reason: Hashable
    description: str
    since: float
    last_write: float
    errors: int
    errors_since_write: int = 0


class _State:
    """Состояние учёта (пул потоков).

    Ключи повторов меняются под lock, и под ним в журнал не пишется:
    EventLogHandler вызывает admit под своей блокировкой, и запись под
    lock дала бы взаимную блокировку. Состояние директории меняется
    под directory_lock, и запись о нём идёт под ним же: так 2001 не
    опередит 2000. EventLogHandler directory_lock не берёт.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.directory_lock = threading.Lock()
        self.keys: dict[int, dict[Hashable, _Entry]] = {}
        # Ключ переполнения на код: только число ошибок, без причин, —
        # память постоянна при любом потоке новых fileId
        self.overflow: dict[int, _Entry] = {}
        self.directory: _Unavailable | None = None
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
    if entry.unsent:
        entry.unsent = False
        entry.last_write = now
        return note
    entry.count += 1
    return None


def mark_unsent(event_id: int, reason: Hashable | None) -> None:
    """Отметить, что первое событие ключа так и не записано.

    Следующий случай ключа снова даст событие, а повторы, посчитанные
    до того, не теряются: они уйдут напоминанием. reason=None — ключ
    переполнения этого кода.
    """
    with _state.lock:
        if reason is None:
            entry = _state.overflow.get(event_id)
        else:
            entry = _state.keys.get(event_id, {}).get(reason)
        if entry is not None:
            entry.unsent = True


def _log(
    level: int,
    event_id: int | None,
    message: str,
    exc: BaseException | None = None,
) -> None:
    """Записать строку; без кода — только в файл журнала."""
    extra = {"event_log": False} if event_id is None else {"event_id": event_id}
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
        _log(level, event_id, message + note, exc)
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


def is_network_error(error: BaseException) -> bool:
    """Проверить, означает ли ошибка недоступность сетевой папки."""
    return getattr(error, "winerror", None) in NETWORK_WINERRORS


def _reason(error: BaseException) -> Hashable:
    return (
        getattr(error, "winerror", None)
        or getattr(error, "errno", None)
        or type(error).__name__
    )


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
    reason = (status, _reason(error))
    description = describe_error(error)
    message = (
        f"Папка с файлами {directory} недоступна: {description}. "
        f"Ответ клиенту: {status}"
    )
    # Запись — под той же блокировкой, что и решение: иначе 2001 из
    # параллельного успешного запроса мог бы опередить это 2000
    with _state.directory_lock:
        state = _state.directory
        changed = state is None or state.reason != reason
        if state is None:
            _state.directory = _Unavailable(
                str(directory), reason, description, now, now, errors=1
            )
        elif changed:
            if state.errors_since_write:
                message += (
                    f". До смены причины ошибочных ответов с прошлой записи: "
                    f"{state.errors_since_write} ({state.description})"
                )
            state.reason = reason
            state.description = description
            state.last_write = now
            state.errors += 1
            state.errors_since_write = 0
        else:
            state.errors += 1
            state.errors_since_write += 1
        _log(
            logging.WARNING,
            EventId.DIRECTORY_UNAVAILABLE if changed else None,
            message,
            _server_error(error, status),
        )
    mark_tracked(error)


def directory_ok() -> None:
    """Учесть успешный ответ: после недоступности — событие 2001."""
    # Без блокировки, пока папка доступна: успешные запросы не ждут
    # записи о чужой ошибке. Гонка с directory_error безвредна: этот
    # ответ тогда просто считается полученным раньше ошибки
    if _state.directory is None:
        return
    with _state.directory_lock:
        state, _state.directory = _state.directory, None
        if state is None:
            return
        _log(
            logging.INFO,
            EventId.DIRECTORY_RESTORED,
            f"Папка с файлами {state.path} снова доступна: была недоступна "
            f"{format_duration(clock() - state.since)}, "
            f"ошибочных ответов: {state.errors}",
        )


def file_error(
    directory: str | Path, file_id: str, error: OSError, status: int | str
) -> None:
    """Учесть ошибку доступа к скачиваемому файлу.

    Сетевой код Windows — недоступность директории (2000), прочие
    ошибки — 3000 с ключом «код ошибки + fileId». Для ответа 5xx запись
    идёт с трассировкой исходной ошибки.
    """
    if is_network_error(error):
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
    reminders: list[tuple[int, int, str]],
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
        # Напоминание записано — отброшенное первое событие уже не нужно
        entry.unsent = False
        reminders.append(
            (
                entry.level,
                event_id,
                f"Повторялось ещё {count} раз с прошлой записи: {entry.summary}",
            )
        )
        return False
    return not final


def _remind(*, final: bool) -> None:
    """Напоминания по ключам и 2002 о недоступной папке."""
    now = clock()
    reminders: list[tuple[int, int, str]] = []
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
    for level, event_id, message in reminders:
        _log(level, event_id, message)
    with _state.directory_lock:
        state = _state.directory
        if state is not None and (
            final
            or (state.errors_since_write and now - state.last_write >= REMINDER_SECONDS)
        ):
            _log(
                logging.WARNING,
                EventId.DIRECTORY_STILL_UNAVAILABLE,
                f"Папка с файлами {state.path} по-прежнему недоступна "
                f"({format_duration(now - state.since)}): "
                f"{state.description}. Ошибочных ответов с прошлой "
                f"записи: {state.errors_since_write}",
            )
            state.errors_since_write = 0
            state.last_write = now


def tick() -> None:
    """Такт таймера: напоминания через час после последней записи."""
    _remind(final=False)


def flush() -> None:
    """Итог при остановке: ненулевые счётчики и недоступная папка.

    Вызывается перед событием остановки (1001) или ошибки (1002).
    """
    _remind(final=True)


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
