"""Журнал сервиса: консоль, файл в профиле пользователя, журнал событий.

Модули pywin32 импортируются лениво, внутри функций: модуль
импортируется на любой ОС, а вне Windows журнал пишется только в
консоль.
"""

import logging
import sys
import time
from contextlib import suppress
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.event_tracking import EventId, admit, is_tracked

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

# Обработчики, добавленные setup_logging: повторная настройка их снимает
_handlers: list[logging.Handler] = []


def event_id_for(record: logging.LogRecord) -> int | None:
    """Вернуть код события для записи или None, если события нет.

    Журнал запросов (uvicorn.access), записи с флагом «только в файл»
    (extra={"event_log": False}) и записи с уже учтённым исключением
    событий не дают. Код берётся из extra={"event_id": …}; без него —
    4000 для ERROR и выше, 3001 для WARNING, INFO без кода не идёт.
    Флаг может быть функцией: она вызывается при обработке записи под
    замком обработчика, а не при её создании.
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
    """

    def __init__(self) -> None:
        """Открыть источник событий FilePickerAPI."""
        super().__init__()
        import win32evtlog

        self._win32evtlog = win32evtlog
        self._source = win32evtlog.RegisterEventSource(None, EVENT_SOURCE)
        # Текст форматируется при записи: буфер не держит ни аргументы,
        # ни трассировки с кадрами стека
        self._buffer: list[tuple[int, int, str]] | None = []
        self._dropped = 0
        # Буфер отброшен: дальше — только записи исхода
        self._discarded = False
        # Идёт серия отказов ReportEvent: строка о ней уже в журнале
        self._report_failing = False
        # Время и уровень Event Viewer показывает сам
        self.setFormatter(logging.Formatter("%(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        """Записать событие или отложить его в буфер.

        Запись без явного кода идёт через учёт повторов с ключом
        «логгер + шаблон сообщения»: иначе Uvicorn, который пишет о
        каждом некорректном HTTP-запросе из сети, давал бы событие на
        каждое соединение сканера портов.
        """
        event_id = event_id_for(record)
        if event_id is None:
            return
        outcome = getattr(record, "outcome", False)
        if self._discarded and not outcome:
            return
        try:
            message = record.getMessage()
        except Exception:
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
            return
        note = ""
        if getattr(record, "event_id", None) is None:
            note = admit(
                event_id,
                (record.name, str(record.msg)),
                # Ключ — шаблон, а текст напоминания — первая запись
                f"{record.name}: {message}",
                record.levelno,
            )
            if note is None:
                return
        text = self.format(record) + note
        if self._buffer is None:
            self._report(record.levelno, event_id, text)
        else:
            self._buffer.append((record.levelno, event_id, text))

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
                    (note.levelno, EventId.OTHER_WARNING, self.format(note))
                )
                self._dropped = 0
            for levelno, event_id, text in buffered:
                self._report(levelno, event_id, text)

    def discard_startup_events(self) -> None:
        """Отбросить буфер; дальше пишутся только записи исхода.

        Каждая отброшенная запись (и каждый её повтор) уже есть в файле
        журнала; итоговых напоминаний второй экземпляр не пишет.
        """
        with self.lock:
            self._buffer = None
            self._discarded = True
            self._dropped = 0

    def _report(self, levelno: int, event_id: int, text: str) -> None:
        """Записать событие; отказ ReportEvent запрос не роняет.

        Вместо трассировки handleError в stderr на каждое событие —
        одна строка на серию отказов в файл журнала и консоль (не в
        журнал событий), сама запись в файле журнала уже есть.
        """
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
            if not self._report_failing:
                self._report_failing = True
                logger.warning(
                    "Не удалось записать событие %s в журнал событий: %s",
                    event_id,
                    e,
                    extra={"event_log": False},
                )
            return
        self._report_failing = False

    def close(self) -> None:
        """Закрыть источник событий."""
        source, self._source = self._source, None
        if source is not None:
            self._win32evtlog.DeregisterEventSource(source)
        super().close()


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
        Запись о самом отказе файла (file_log=False) сюда не пишется.
        """
        if getattr(record, "file_log", True) is False:
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
        _add(logging.StreamHandler(sys.stderr)).setFormatter(formatter)
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


def flush_handlers() -> None:
    """Сбросить обработчики журнала перед os._exit.

    flush() берёт замок обработчика, а его может держать зависший
    поток (например, запись в переполненный канал консоли). Поэтому
    замок ждём не дольше FLUSH_LOCK_SECONDS, и такой обработчик
    пропускаем: процесс должен завершиться в срок.
    """
    for handler in _handlers:
        if not handler.lock.acquire(timeout=FLUSH_LOCK_SECONDS):
            continue
        try:
            with suppress(Exception):
                handler.flush()
        finally:
            handler.lock.release()
