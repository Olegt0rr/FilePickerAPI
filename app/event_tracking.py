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
# Номера, last_ok_access, публикация сбоя и счётчики недоступной папки
# — под своей короткой блокировкой: без GIL next() и «прочитать,
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
# После отказа записи событие пишется снова не раньше чем через минуту:
# пока журнал событий отказывает, запросы не ждут ReportEvent каждый
UNSENT_RETRY_SECONDS = 60.0


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
    # 2000 не записалось: ошибочный ответ после retry_at пишет его снова
    unsent: bool = False
    retry_at: float = 0.0


# Обратные вызовы для незаписанных событий о папке. EventLogHandler
# вызывает их и под directory_lock этого же потока, и из стартового
# буфера без него, поэтому поля — под _access_lock, а не directory_lock


def _directory_unsent(state: _Unavailable, retry_after: float) -> None:
    retry_at = clock() + retry_after
    with _access_lock:
        state.unsent = True
        state.retry_at = retry_at


def _restore_directory_errors(
    state: _Unavailable,
    count: int,
    _retry_after: float,
) -> None:
    """2002 не записалось: ошибочные ответы уйдут следующим."""
    with _access_lock:
        state.errors_since_write += count


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


def _log(
    level: int,
    event_id: int | None,
    message: str,
    exc: BaseException | None = None,
    *,
    on_unsent: OnUnsent | None = None,
) -> None:
    """Записать строку; без кода — только в файл журнала.

    on_unsent вызывает EventLogHandler, если событие не записалось
    (отказ ReportEvent или форматтера, отброшенный стартовый буфер):
    так первое событие ключа, 2000 или число повторов не теряются.
    """
    extra = {"event_log": False} if event_id is None else {"event_id": event_id}
    if event_id is not None and on_unsent is not None:
        extra[UNSENT_ATTRIBUTE] = on_unsent
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
        if state is None:
            changed = True
            state = _state.directory
        else:
            # Поля — под _access_lock: их меняют и обратные вызовы
            # EventLogHandler для незаписанных 2000 и 2002
            with _access_lock:
                changed, message = _update_unavailable(
                    state, reason, description, access, now, message
                )
        _log(
            logging.WARNING,
            EventId.DIRECTORY_UNAVAILABLE if changed else None,
            message,
            exc,
            on_unsent=functools.partial(_directory_unsent, state),
        )
    mark_tracked(error)


def _update_unavailable(  # noqa: PLR0913, PLR0917 - поля одной ошибки
    state: _Unavailable,
    reason: Hashable,
    description: str,
    access: int,
    now: float,
    message: str,
) -> tuple[bool, str]:
    """Учесть ошибку уже недоступной папки.

    Возвращает, писать ли 2000, и текст записи.
    """
    # Ошибка обращения, начатого раньше последнего сбоя, — повтор:
    # причину она не меняет, иначе более старая вытеснила бы новую
    new_reason = state.reason != reason and access >= state.access
    # Прошлое 2000 не записалось — пишется снова, не чаще раза в минуту
    resend = state.unsent and now >= state.retry_at
    state.access = max(state.access, access)
    state.errors += 1
    if not (new_reason or resend):
        state.errors_since_write += 1
        return False, message
    if state.errors_since_write:
        message += (
            ". До смены причины ошибочных ответов с прошлой записи: "
            if new_reason
            else ". Ошибочных ответов с прошлой записи: "
        ) + f"{state.errors_since_write} ({state.description})"
    if new_reason:
        state.reason = reason
        state.description = description
    state.unsent = False
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
        if state.unsent:
            # 2000 так и не записалось: причина — здесь
            message += f". Причина недоступности: {state.description}"
        _log(logging.INFO, EventId.DIRECTORY_RESTORED, message)


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


def _remind(*, final: bool) -> None:
    """Напоминания по ключам и 2002 о недоступной папке."""
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
    with _state.directory_lock:
        state = _state.directory
        if state is not None and (
            final
            or (state.errors_since_write and now - state.last_write >= REMINDER_SECONDS)
        ):
            with _access_lock:
                count, state.errors_since_write = state.errors_since_write, 0
                state.last_write = now
            _log(
                logging.WARNING,
                EventId.DIRECTORY_STILL_UNAVAILABLE,
                f"Папка с файлами {state.path} по-прежнему недоступна "
                f"({format_duration(now - state.since)}): "
                f"{state.description}. Ошибочных ответов с прошлой "
                f"записи: {count}",
                on_unsent=functools.partial(_restore_directory_errors, state, count),
            )


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
    останавливает. Идущий такт ждёт не дольше wait секунд: при срочной
    остановке, когда процесс вот-вот завершат, — wait=0.
    """
    acquired = _state.remind_lock.acquire(timeout=wait)
    try:
        _remind(final=True)
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
