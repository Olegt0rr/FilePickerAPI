"""Тесты журнала сервиса: консоль, файл, журнал событий Windows."""

import logging
import os
import re
import sys
import time
import types
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from unittest import mock

import pytest

from app import event_tracking, logging_setup
from app.event_tracking import EventId, is_tracked, mark_tracked
from tests.platforms import windows_only

# Настоящие функции: conftest подменяет их в каждом тесте
REAL_LOG_FILE_PATH = logging_setup.log_file_path
REAL_EVENT_SOURCE_REGISTERED = logging_setup.event_source_registered

MC_FILE = Path(__file__).parent.parent / "app" / "messages" / "FilePickerAPI.mc"
DAY = 24 * 60 * 60


def fake_win32evtlog():
    """Поддельный win32evtlog: ReportEvent запоминает вызовы."""
    module = types.ModuleType("win32evtlog")
    module.EVENTLOG_ERROR_TYPE = 1
    module.EVENTLOG_WARNING_TYPE = 2
    module.EVENTLOG_INFORMATION_TYPE = 4
    module.RegisterEventSource = mock.Mock(return_value="source-handle")
    module.DeregisterEventSource = mock.Mock()
    module.ReportEvent = mock.Mock()
    return module


def reported(evtlog):
    """События, переданные в ReportEvent: (код, тип, текст)."""
    events = []
    for call in evtlog.ReportEvent.call_args_list:
        source, event_type, category, event_id, sid, strings, data = call.args
        assert (source, category, sid, data) == ("source-handle", 0, None, None)
        [text] = strings
        events.append((event_id, event_type, text))
    return events


@pytest.fixture
def evtlog(monkeypatch):
    """Поддельный win32evtlog в sys.modules."""
    module = fake_win32evtlog()
    monkeypatch.setitem(sys.modules, "win32evtlog", module)
    return module


@pytest.fixture
def event_logger(evtlog):
    """Логгер с EventLogHandler, буфер стартовых событий отправлен."""
    handler = logging_setup.EventLogHandler()
    handler.send_startup_events()
    test_logger = logging.getLogger("app.test_event_log")
    test_logger.addHandler(handler)
    test_logger.setLevel(logging.INFO)
    yield test_logger, evtlog
    test_logger.removeHandler(handler)
    handler.close()


@pytest.fixture
def on_windows(monkeypatch):
    """Ветка Windows в setup_logging на любой ОС."""
    monkeypatch.setattr(logging_setup, "_windows", lambda: True)


def our_handlers():
    return list(logging_setup._handlers)  # noqa: SLF001 - состояние модуля


def log_dir(tmp_path):
    return tmp_path / "logs"


def touch(path, age_days):
    """Создать файл со временем изменения age_days дней назад."""
    path.write_text("old", encoding="utf-8")
    stamp = time.time() - age_days * DAY
    os.utime(path, (stamp, stamp))


class TestSetupLogging:
    """Обработчики и уровни при запуске из консоли и без неё."""

    @pytest.mark.parametrize("console", [True, False])
    def test_levels_and_console(self, monkeypatch, console):
        """Проверить: консоль только при запуске из неё, INFO — для
        app и Uvicorn, остальное — с WARNING.
        """
        monkeypatch.setattr(logging_setup, "_windows", lambda: False)
        logging_setup.setup_logging(console=console)

        handlers = our_handlers()
        assert len(handlers) == int(console)
        if console:
            [handler] = handlers
            assert type(handler) is logging.StreamHandler
            assert handler.stream is sys.stderr
            assert handler.formatter._fmt == logging_setup.LOG_FORMAT  # noqa: SLF001
        assert logging.getLogger().level == logging.WARNING
        for name in ("app", "uvicorn.error", "uvicorn.access"):
            assert logging.getLogger(name).level == logging.INFO

    def test_outside_windows_console_only(self, monkeypatch, tmp_path):
        """Проверить: вне Windows нет ни файла, ни журнала событий."""
        monkeypatch.setattr(logging_setup, "_windows", lambda: False)
        monkeypatch.setitem(sys.modules, "win32evtlog", None)
        logging_setup.setup_logging(console=True)
        logging.getLogger("app.test").warning("only console")
        assert not log_dir(tmp_path).exists()

    @pytest.mark.usefixtures("on_windows")
    @pytest.mark.parametrize("console", [True, False])
    def test_file_in_every_mode(self, tmp_path, console):
        """Проверить: на Windows файл журнала ведётся всегда."""
        logging_setup.setup_logging(console=console)
        logging.getLogger("app.test").info("строка в файле")
        logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /files" 200')
        logging.getLogger("other.library").info("не попадает")

        [file_handler] = [
            h for h in our_handlers() if isinstance(h, logging_setup.LogFileHandler)
        ]
        file_handler.flush()
        text = (log_dir(tmp_path) / "FilePickerAPI.log").read_text(encoding="utf-8")
        assert re.search(r" INFO app\.test: строка в файле$", text, re.MULTILINE)
        assert 'INFO uvicorn.access: 127.0.0.1 - "GET /files" 200' in text
        assert "не попадает" not in text

    @pytest.mark.usefixtures("on_windows")
    def test_event_only_not_in_file_or_console(self, tmp_path, capsys):
        """Проверить: повтор события о папке («только в журнал
        событий») не попадает ни в файл журнала, ни в консоль.
        """
        logging_setup.setup_logging(console=True)
        test_logger = logging.getLogger("app.test")
        test_logger.warning("повтор", extra={"event_id": 2000, "event_only": True})
        test_logger.warning("строка")

        [file_handler] = [
            h for h in our_handlers() if isinstance(h, logging_setup.LogFileHandler)
        ]
        file_handler.flush()
        text = (log_dir(tmp_path) / "FilePickerAPI.log").read_text(encoding="utf-8")
        err = capsys.readouterr().err
        assert "строка" in text
        assert "строка" in err
        assert "повтор" not in text
        assert "повтор" not in err

    @pytest.mark.usefixtures("on_windows")
    def test_file_handler_parameters(self, tmp_path):
        """Проверить параметры ротации: полночь, UTF-8, backupCount."""
        logging_setup.setup_logging(console=False)
        [handler] = our_handlers()
        assert isinstance(handler, TimedRotatingFileHandler)
        assert handler.when == "MIDNIGHT"
        assert handler.interval == DAY
        assert handler.backupCount == 1000
        assert handler.encoding == "utf-8"
        assert Path(handler.baseFilename) == log_dir(tmp_path) / "FilePickerAPI.log"

    @pytest.mark.usefixtures("on_windows")
    def test_repeated_setup_replaces_handlers(self):
        """Проверить: повторная настройка не удваивает обработчики."""
        logging_setup.setup_logging(console=True)
        logging_setup.setup_logging(console=True)
        assert len(our_handlers()) == 2
        root_handlers = logging.getLogger().handlers
        assert sum(h in root_handlers for h in our_handlers()) == 2

    @pytest.mark.usefixtures("on_windows")
    def test_old_archive_deleted_at_startup(self, tmp_path):
        """Проверить: при запуске удаляется только архив старше 30
        дней, хотя архивов меньше 30.
        """
        folder = log_dir(tmp_path)
        folder.mkdir()
        old = folder / "FilePickerAPI.log.2026-08-01"
        touch(old, 60)
        fresh = [folder / f"FilePickerAPI.log.2026-10-2{i}" for i in range(5)]
        for i, path in enumerate(fresh):
            touch(path, i + 1)
        other = folder / "other.log"
        touch(other, 60)

        logging_setup.setup_logging(console=False)

        assert not old.exists()
        assert all(path.exists() for path in fresh)
        assert other.exists()

    @pytest.mark.usefixtures("on_windows")
    @pytest.mark.parametrize("failure", ["path", "mkdir", "open"])
    def test_log_file_unavailable(self, monkeypatch, tmp_path, caplog, failure):
        """Проверить: без файла журнала сервис работает и пишет 1004.

        path — отказ SHGetKnownFolderPath, mkdir — папку журнала не
        создать, open — файл не открыть.
        """
        if failure == "path":
            error = OSError("E_ACCESSDENIED")
            monkeypatch.setattr(
                logging_setup, "log_file_path", mock.Mock(side_effect=error)
            )
        elif failure == "mkdir":
            # На месте папки журнала — файл
            log_dir(tmp_path).write_text("not a folder", encoding="utf-8")
        else:
            monkeypatch.setattr(
                logging_setup.LogFileHandler,
                "_open",
                mock.Mock(side_effect=PermissionError("locked")),
            )

        logging_setup.setup_logging(console=True)

        assert not any(
            isinstance(h, logging_setup.LogFileHandler) for h in our_handlers()
        )
        [record] = [r for r in caplog.records if r.name == "app.logging_setup"]
        assert record.levelno == logging.WARNING
        assert record.event_id == EventId.LOG_FILE_UNAVAILABLE
        assert record.getMessage().startswith("Не удалось вести файл журнала: ")

    @pytest.mark.usefixtures("on_windows")
    def test_without_event_source_no_event_log(self, monkeypatch, tmp_path):
        """Проверить: без зарегистрированного источника журнал событий
        не открывается, ошибок о нём нет, файл пишется.
        """
        monkeypatch.setitem(sys.modules, "win32evtlog", None)
        logging_setup.setup_logging(console=False)
        logging.getLogger("app.test").error("ошибка только в файле")
        logging_setup.send_startup_events()
        logging_setup.discard_startup_events()

        assert not any(
            isinstance(h, logging_setup.EventLogHandler) for h in our_handlers()
        )
        [handler] = our_handlers()
        handler.flush()
        text = (log_dir(tmp_path) / "FilePickerAPI.log").read_text(encoding="utf-8")
        assert "ERROR app.test: ошибка только в файле" in text

    @pytest.mark.usefixtures("on_windows")
    def test_event_source_registered(self, monkeypatch, evtlog):
        """Проверить: с источником события идут через буфер, 1004 о
        недоступном файле — после отправки буфера.
        """
        monkeypatch.setattr(logging_setup, "event_source_registered", lambda: True)
        monkeypatch.setattr(
            logging_setup, "log_file_path", mock.Mock(side_effect=OSError("denied"))
        )
        logging_setup.setup_logging(console=False)
        evtlog.RegisterEventSource.assert_called_once_with(None, "FilePickerAPI")
        evtlog.ReportEvent.assert_not_called()

        logging_setup.send_startup_events()

        [(event_id, event_type, text)] = reported(evtlog)
        assert event_id == 1004
        assert event_type == evtlog.EVENTLOG_WARNING_TYPE
        assert text == "app.logging_setup: Не удалось вести файл журнала: denied"

    @pytest.mark.usefixtures("on_windows")
    def test_second_instance_discards_startup_events(self, monkeypatch, evtlog):
        """Проверить: у второго экземпляра буфер отбрасывается, а его
        событие пишется сразу.
        """
        monkeypatch.setattr(logging_setup, "event_source_registered", lambda: True)
        logging_setup.setup_logging(console=False)
        logging.getLogger("app.test").warning("буферное предупреждение")

        logging_setup.discard_startup_events()
        logging.getLogger("app.test").info(
            "сервис уже работает", extra={"event_id": EventId.ALREADY_RUNNING}
        )

        assert [e[:2] for e in reported(evtlog)] == [
            (1005, evtlog.EVENTLOG_INFORMATION_TYPE)
        ]

    @pytest.mark.usefixtures("on_windows")
    def test_event_log_open_failure(self, monkeypatch, evtlog, tmp_path):
        """Проверить: отказ RegisterEventSource не мешает запуску, а
        запись о нём есть в файле журнала (консоли при входе нет).
        """
        monkeypatch.setattr(logging_setup, "event_source_registered", lambda: True)
        evtlog.RegisterEventSource.side_effect = OSError("event log service stopped")

        logging_setup.setup_logging(console=False)

        [handler] = our_handlers()
        assert type(handler) is logging_setup.LogFileHandler
        handler.flush()
        text = (log_dir(tmp_path) / "FilePickerAPI.log").read_text(encoding="utf-8")
        assert (
            "WARNING app.logging_setup: Журнал событий недоступен: "
            "event log service stopped"
        ) in text
        assert "OSError: event log service stopped" in text


class TestLogFileRotation:
    """Ротация в полночь и срок хранения 30 дней."""

    @pytest.fixture
    def handler(self, tmp_path):
        folder = log_dir(tmp_path)
        folder.mkdir()
        handler = logging_setup.LogFileHandler(folder / "FilePickerAPI.log")
        handler.setFormatter(logging.Formatter(logging_setup.LOG_FORMAT))
        yield handler
        handler.close()

    @staticmethod
    def write(handler, message):
        record = logging.makeLogRecord(
            {"name": "app.test", "levelno": 20, "levelname": "INFO", "msg": message}
        )
        handler.handle(record)

    @staticmethod
    def make_due(handler):
        """Сдвинуть время ротации: следующая запись ротирует."""
        handler.rolloverAt = int(time.time()) - 1

    @staticmethod
    def archive_name(handler):
        day = time.localtime(handler.rolloverAt - handler.interval)
        return "FilePickerAPI.log." + time.strftime("%Y-%m-%d", day)

    def test_rotation_deletes_only_old_archives(self, handler, tmp_path):
        """Проверить настоящей ротацией: файл получает дату в имени,
        запись идёт в новый файл, удаляется только архив старше 30 дней.
        """
        folder = log_dir(tmp_path)
        old = folder / "FilePickerAPI.log.2026-08-01"
        touch(old, 60)
        fresh = folder / "FilePickerAPI.log.2026-10-30"
        touch(fresh, 3)
        self.write(handler, "вчера")
        self.make_due(handler)
        archive = folder / self.archive_name(handler)

        self.write(handler, "сегодня")
        handler.flush()

        assert "вчера" in archive.read_text(encoding="utf-8")
        current = (folder / "FilePickerAPI.log").read_text(encoding="utf-8")
        assert "сегодня" in current
        assert "вчера" not in current
        assert not old.exists()
        assert fresh.exists()
        assert handler.rolloverAt > time.time()

    def test_rename_failure_keeps_writing(self, handler, tmp_path, monkeypatch):
        """Проверить: файл не переименовать — записи продолжаются в
        текущий файл, одна строка о неудаче на эпизод, повтор через час.
        """
        folder = log_dir(tmp_path)
        error = PermissionError(13, "The process cannot access the file")
        replace = mock.Mock(side_effect=error)
        monkeypatch.setattr(Path, "replace", replace)

        self.write(handler, "до полуночи")
        self.make_due(handler)
        before = time.time()
        self.write(handler, "первая после полуночи")
        assert before + 3600 - 1 <= handler.rolloverAt <= time.time() + 3600
        # Второй неудачный повтор того же эпизода строки не добавляет
        self.make_due(handler)
        self.write(handler, "вторая после полуночи")
        handler.flush()

        assert replace.call_count == 2
        text = (folder / "FilePickerAPI.log").read_text(encoding="utf-8")
        for message in ("до полуночи", "первая после полуночи", "вторая после"):
            assert message in text
        assert text.count("Не удалось переименовать файл журнала") == 1
        assert list(folder.iterdir()) == [folder / "FilePickerAPI.log"]

        # Читатель закрыл файл: следующая попытка проходит, а новая
        # неудача — уже новый эпизод со своей строкой
        monkeypatch.setattr(Path, "replace", Path.rename)
        self.make_due(handler)
        self.write(handler, "после закрытия")
        handler.flush()
        assert len(list(folder.iterdir())) == 2
        assert handler._rotation_failed is False  # noqa: SLF001

    def test_existing_archive_kept(self, handler, tmp_path):
        """Проверить: архив с той же датой не затирается, новый
        получает номер.
        """
        folder = log_dir(tmp_path)
        self.write(handler, "запись")
        self.make_due(handler)
        name = self.archive_name(handler)
        (folder / name).write_text("прежний архив", encoding="utf-8")
        (folder / f"{name}.1").write_text("второй архив", encoding="utf-8")
        self.write(handler, "после ротации")
        handler.flush()

        assert (folder / name).read_text(encoding="utf-8") == "прежний архив"
        assert (folder / f"{name}.1").read_text(encoding="utf-8") == "второй архив"
        assert "запись" in (folder / f"{name}.2").read_text(encoding="utf-8")

    def test_reopen_failure_postpones_rotation(self, handler, monkeypatch):
        """Проверить: файл не переименовать и не открыть — следующая
        попытка всё равно через час, а не на каждой записи.
        """
        monkeypatch.setattr(Path, "replace", mock.Mock(side_effect=OSError("busy")))
        monkeypatch.setattr(handler, "_open", mock.Mock(side_effect=OSError("quota")))
        self.make_due(handler)

        with pytest.raises(OSError, match="quota"):
            handler.doRollover()

        assert handler.rolloverAt > time.time() + 3600 - 5
        # До следующей попытки записи не открывают файл заново
        for message in ("a", "b"):
            self.write(handler, message)
        handler._open.assert_called_once_with()  # noqa: SLF001

    def test_new_file_open_failure_postpones_rotation(self, handler, monkeypatch):
        """Проверить: файл переименован, а новый не открылся (квота) —
        повтор через час, а не попытка открытия на каждой записи.
        """
        open_file = mock.Mock(side_effect=OSError("quota"))
        monkeypatch.setattr(handler, "_open", open_file)
        self.make_due(handler)

        with pytest.raises(OSError, match="quota"):
            handler.doRollover()

        assert handler.rolloverAt > time.time() + 3600 - 5
        for message in ("a", "b", "c"):
            self.write(handler, message)
        open_file.assert_called_once_with()

    def test_write_failure_reported_once(self, handler, caplog):
        """Проверить: отказ записи в файл — одно 1004 на серию отказов,
        не в сам файл; после удачной записи новая серия даёт новое 1004.
        """
        stream = handler.stream
        broken = mock.Mock()
        broken.write.side_effect = OSError(28, "No space left on device")
        handler.stream = broken
        self.write(handler, "первая")
        self.write(handler, "вторая")
        handler.stream = stream
        self.write(handler, "после освобождения места")
        handler.stream = broken
        self.write(handler, "снова")
        handler.stream = stream

        reports = [r for r in caplog.records if r.name == "app.logging_setup"]
        assert len(reports) == 2
        for report in reports:
            assert report.levelno == logging.WARNING
            assert report.event_id == EventId.LOG_FILE_UNAVAILABLE
            assert report.file_log is False
            assert "No space left on device" in report.getMessage()
        # Отчёт об отказе не пишется в сам файл
        handler.handle(reports[0])
        handler.flush()
        text = Path(handler.baseFilename).read_text(encoding="utf-8")
        assert "после освобождения места" in text
        assert "Не удалось записать" not in text

    def test_missing_current_file_rotates(self, handler, tmp_path):
        """Проверить: удалённый текущий файл ротации не мешает."""
        folder = log_dir(tmp_path)
        handler.close()
        (folder / "FilePickerAPI.log").unlink()
        self.make_due(handler)
        self.write(handler, "новый файл")
        handler.flush()
        assert list(folder.iterdir()) == [folder / "FilePickerAPI.log"]

    def test_undeletable_archive_ignored(self, handler, tmp_path, monkeypatch):
        """Проверить: архив, который не удалить, не мешает ротации и
        удаляется при следующей очистке.
        """
        folder = log_dir(tmp_path)
        old = folder / "FilePickerAPI.log.2026-08-01"
        touch(old, 60)
        unlink = Path.unlink
        monkeypatch.setattr(
            Path, "unlink", mock.Mock(side_effect=PermissionError("open"))
        )
        self.make_due(handler)
        self.write(handler, "запись")
        assert old.exists()

        monkeypatch.setattr(Path, "unlink", unlink)
        handler.delete_old_archives()
        assert not old.exists()

    def test_unreadable_archive_skipped(self, handler, tmp_path, monkeypatch):
        """Проверить: архив без доступа к атрибутам пропускается."""
        folder = log_dir(tmp_path)
        touch(folder / "FilePickerAPI.log.2026-08-01", 60)
        stat = Path.stat
        error = PermissionError("denied")

        def fake_stat(self, *args, **kwargs):
            if self.name.endswith("2026-08-01"):
                raise error
            return stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fake_stat)
        assert handler.getFilesToDelete() == []

    def test_missing_log_folder(self, handler, monkeypatch):
        """Проверить: ошибка обхода папки журнала — нечего удалять."""
        monkeypatch.setattr(Path, "glob", mock.Mock(side_effect=OSError("gone")))
        assert handler.getFilesToDelete() == []

    @windows_only
    def test_open_reader_on_windows(self, handler, tmp_path):
        """Проверить на Windows: файл открыт без FILE_SHARE_DELETE —
        записи продолжаются, после закрытия ротация проходит.
        """
        folder = log_dir(tmp_path)
        self.write(handler, "до полуночи")
        handler.flush()
        # open() в Python открывает файл без FILE_SHARE_DELETE
        with open(folder / "FilePickerAPI.log", encoding="utf-8") as reader:
            self.make_due(handler)
            self.write(handler, "при открытом читателе")
            handler.flush()
            assert "при открытом читателе" in reader.read()
        assert list(folder.iterdir()) == [folder / "FilePickerAPI.log"]

        self.make_due(handler)
        self.write(handler, "после закрытия")
        handler.flush()
        assert len(list(folder.iterdir())) == 2

    @windows_only
    def test_open_old_archive_on_windows(self, handler, tmp_path):
        """Проверить на Windows: открытый старый архив не мешает
        ротации и удаляется после закрытия.
        """
        folder = log_dir(tmp_path)
        old = folder / "FilePickerAPI.log.2026-08-01"
        touch(old, 60)
        with open(old, encoding="utf-8"):
            self.make_due(handler)
            self.write(handler, "новый день")
            handler.flush()
            assert old.exists()
        assert "новый день" in (folder / "FilePickerAPI.log").read_text(
            encoding="utf-8"
        )
        handler.delete_old_archives()
        assert not old.exists()


class TestEventLogHandler:
    """Журнал событий с поддельным win32evtlog."""

    CODE_CASES = (
        # Каждый код — с уровнем, которым его пишет сервис
        (logging.INFO, EventId.SERVICE_STARTED, 1000, 4),
        (logging.INFO, EventId.SERVICE_STOPPED, 1001, 4),
        (logging.ERROR, EventId.SERVICE_FAILED, 1002, 1),
        (logging.WARNING, EventId.BUILD_FOLDER_PERMISSIONS, 1003, 2),
        (logging.WARNING, EventId.LOG_FILE_UNAVAILABLE, 1004, 2),
        (logging.INFO, EventId.ALREADY_RUNNING, 1005, 4),
        (logging.WARNING, EventId.OTHER_VERSION_RUNNING, 1006, 2),
        (logging.WARNING, EventId.DIRECTORY_UNAVAILABLE, 2000, 2),
        (logging.INFO, EventId.DIRECTORY_RESTORED, 2001, 4),
        (logging.WARNING, EventId.DIRECTORY_STILL_UNAVAILABLE, 2002, 2),
        (logging.WARNING, EventId.DIRECTORY_SLOW, 2003, 2),
        (logging.WARNING, EventId.DIRECTORY_NOT_CREATED, 2004, 2),
        (logging.WARNING, EventId.FILE_ACCESS_ERROR, 3000, 2),
        (logging.WARNING, EventId.OTHER_WARNING, 3001, 2),
        (logging.ERROR, EventId.UNEXPECTED_ERROR, 4000, 1),
        # Без кода — по уровню
        (logging.WARNING, None, 3001, 2),
        (logging.ERROR, None, 4000, 1),
        (logging.CRITICAL, None, 4000, 1),
    )

    def test_every_code_checked(self):
        """Проверить: в test_code_and_type есть каждый код."""
        assert {case[1] for case in self.CODE_CASES} - {None} == set(EventId)

    @pytest.mark.parametrize(
        ("level", "event_id", "expected_id", "expected_type"), CODE_CASES
    )
    def test_code_and_type(
        self, event_logger, level, event_id, expected_id, expected_type
    ):
        """Проверить код, тип и текст события."""
        test_logger, evtlog = event_logger
        extra = {} if event_id is None else {"event_id": event_id}
        test_logger.log(level, "Папка %s: ёЁ", r"\\srv\share", extra=extra)

        assert reported(evtlog) == [
            (expected_id, expected_type, r"app.test_event_log: Папка \\srv\share: ёЁ")
        ]

    @pytest.mark.parametrize(
        ("name", "level", "extra"),
        [
            ("uvicorn.access", logging.WARNING, {}),
            ("uvicorn.access", logging.ERROR, {}),
            ("app.test_event_log", logging.INFO, {}),
            ("app.test_event_log", logging.WARNING, {"event_log": False}),
            ("app.test_event_log", logging.ERROR, {"event_log": False}),
        ],
    )
    def test_not_in_event_log(self, event_logger, name, level, extra):
        """Проверить: журнал запросов, INFO без кода и записи «только в
        файл» событий не дают.
        """
        test_logger, evtlog = event_logger
        handler = test_logger.handlers[0]
        record = logging.makeLogRecord(
            {"name": name, "levelno": level, "msg": "m", **extra}
        )
        handler.handle(record)
        evtlog.ReportEvent.assert_not_called()

    def test_traceback_in_text(self, event_logger):
        """Проверить: трассировка исключения — в тексте события."""
        test_logger, evtlog = event_logger
        error = ValueError("сломалось")
        try:
            raise error
        except ValueError:
            test_logger.exception("Непредвиденная ошибка")

        [(event_id, _, text)] = reported(evtlog)
        assert event_id == 4000
        assert text.startswith("app.test_event_log: Непредвиденная ошибка\n")
        assert "Traceback (most recent call last)" in text
        assert text.endswith("ValueError: сломалось")

    def test_tracked_exception_skipped(self, event_logger):
        """Проверить: учтённое исключение второго события не даёт."""
        test_logger, evtlog = event_logger
        error = OSError("учтено")
        mark_tracked(error)
        try:
            raise error
        except OSError:
            test_logger.exception("Exception in ASGI application")
        evtlog.ReportEvent.assert_not_called()

    def test_long_text_truncated(self, event_logger):
        """Проверить: длинный текст обрезается до 31 839 символов."""
        test_logger, evtlog = event_logger
        test_logger.error("x" * 40_000)

        [(_, _, text)] = reported(evtlog)
        assert len(text) == 31_839
        assert text.endswith("\n… (обрезано, полный текст в FilePickerAPI.log)")

    def test_text_at_limit_not_truncated(self):
        text = "x" * 31_839
        assert logging_setup.truncate_event_text(text) == text
        # 15 000 эмодзи — 30 000 единиц UTF-16, в пределе
        emoji = "😀" * 15_000
        assert logging_setup.truncate_event_text(emoji) == emoji

    @pytest.mark.parametrize("prefix", ["", "x"])
    def test_truncation_counts_utf16_units(self, prefix):
        """Проверить: предел — в единицах UTF-16, суррогатная пара не
        разрывается (оба чётных и нечётных положения среза).
        """
        text = prefix + "😀" * 20_000

        result = logging_setup.truncate_event_text(text)

        units = len(result.encode("utf-16-le")) // 2
        assert 31_838 <= units <= 31_839
        assert result.endswith("\n… (обрезано, полный текст в FilePickerAPI.log)")
        body = result.removesuffix(logging_setup.TRUNCATED_NOTE)
        assert body == prefix + "😀" * (len(body) - len(prefix))

    def test_report_error_does_not_raise(self, event_logger, caplog, capsys):
        """Проверить: отказ ReportEvent не роняет запись и не печатает
        трассировку; одна строка на серию отказов, только в файл.
        """
        test_logger, evtlog = event_logger
        evtlog.ReportEvent.side_effect = OSError("журнал событий переполнен")

        test_logger.error("ошибка 1")
        test_logger.warning("ошибка 2")
        evtlog.ReportEvent.side_effect = None
        test_logger.error("ошибка 3")
        evtlog.ReportEvent.side_effect = OSError("снова")
        test_logger.error("ошибка 4")

        failures = [
            r
            for r in caplog.records
            if r.getMessage().startswith("Не удалось записать")
        ]
        assert [r.getMessage() for r in failures] == [
            (
                "Не удалось записать событие 4000 в журнал событий: "
                "журнал событий переполнен"
            ),
            "Не удалось записать событие 4000 в журнал событий: снова",
        ]
        assert all(r.event_log is False for r in failures)
        assert "Logging error" not in capsys.readouterr().err
        assert evtlog.ReportEvent.call_count == 4

    def test_format_error_uses_handle_error(self, event_logger, monkeypatch):
        """Проверить: ошибка форматирования записи — handleError."""
        test_logger, evtlog = event_logger
        handler = test_logger.handlers[0]
        handle_error = mock.Mock()
        monkeypatch.setattr(handler, "handleError", handle_error)

        handler.handle(
            logging.makeLogRecord(
                {"name": "app.t", "levelno": 30, "msg": "%d", "args": ("не число",)}
            )
        )

        handle_error.assert_called_once()
        evtlog.ReportEvent.assert_not_called()

    def test_formatter_error_uses_handle_error(self, event_logger, clock):
        """Проверить: сбой форматтера не выходит из handle(), а ключ
        повторов не занят — такой же случай через минуту даёт событие.
        """
        test_logger, evtlog = event_logger
        handler = test_logger.handlers[0]
        with (
            mock.patch.object(handler, "handleError") as handle_error,
            mock.patch.object(handler, "format", side_effect=ValueError("формат")),
        ):
            test_logger.warning("отказ %s", 1)

        handle_error.assert_called_once()
        evtlog.ReportEvent.assert_not_called()
        clock.advance(event_tracking.UNSENT_RETRY_SECONDS)
        test_logger.warning("отказ %s", 2)
        assert [e[2] for e in reported(evtlog)] == ["app.test_event_log: отказ 2"]

    def test_report_error_resends_same_key(self, event_logger, clock):
        """Проверить: первое событие ключа не записалось — случай в
        течение минуты считается повтором без вызова ReportEvent, а
        случай после неё снова даёт событие.
        """
        test_logger, evtlog = event_logger
        evtlog.ReportEvent.side_effect = [OSError("full"), None, None]

        test_logger.warning("отказ %s", "сети")
        clock.advance(event_tracking.UNSENT_RETRY_SECONDS - 1)
        test_logger.warning("отказ %s", "сети")
        assert evtlog.ReportEvent.call_count == 1
        clock.advance(1)
        test_logger.warning("отказ %s", "сети")
        test_logger.warning("отказ %s", "сети")

        assert evtlog.ReportEvent.call_count == 2

    def test_report_error_resends_tracked_key(self, event_logger, clock):
        """Проверить: то же для ключа из event_tracking.track."""
        test_logger, evtlog = event_logger
        handler = test_logger.handlers[0]
        tracking_logger = logging.getLogger("app.event_tracking")
        tracking_logger.addHandler(handler)
        evtlog.ReportEvent.side_effect = [OSError("full"), None, None]
        try:
            event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "отказ")
            clock.advance(event_tracking.UNSENT_RETRY_SECONDS)
            for _ in range(2):
                event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "отказ")
        finally:
            tracking_logger.removeHandler(handler)

        assert evtlog.ReportEvent.call_count == 2

    @pytest.fixture
    def tracking_handler(self, event_logger):
        """EventLogHandler на логгере учёта ошибок."""
        test_logger, evtlog = event_logger
        handler = test_logger.handlers[0]
        tracking_logger = logging.getLogger("app.event_tracking")
        tracking_logger.addHandler(handler)
        yield evtlog
        tracking_logger.removeHandler(handler)

    def test_report_error_resends_directory_unavailable(
        self, tracking_handler, clock, caplog
    ):
        """Проверить: 2000 не записалось — ответы в течение минуты
        ReportEvent не вызывают, такт после неё пишет то же 2000 с
        трассировкой только в журнал событий, дальше — повторы без
        событий.
        """
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = [OSError("full"), None]
        for _ in range(2):
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        assert evtlog.ReportEvent.call_count == 1
        clock.advance(event_tracking.UNSENT_RETRY_SECONDS)
        event_tracking.tick()
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)

        sent = reported(evtlog)
        assert [e[0] for e in sent] == [2000, 2000]
        assert sent[1][2] == sent[0][2]
        assert sent[1][2].endswith("\nOSError: [Errno 5] I/O")
        # Первая попытка — одна запись, повтор — только в журнал событий
        event_only = [r for r in caplog.records if getattr(r, "event_only", False)]
        assert [r.event_id for r in event_only] == [2000]
        assert event_only[0].exc_info is None

    def test_filtered_level_does_not_block_queue(self, tracking_handler):
        """Проверить: 2001 отсечено уровнем логгера — очередь не ждёт
        его исхода, и следующее 2000 пишется.
        """
        evtlog = tracking_handler
        logging.getLogger("app.event_tracking").setLevel(logging.WARNING)
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        event_tracking.directory_ok()
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)

        assert [e[0] for e in reported(evtlog)] == [2000, 2000]

    @pytest.mark.usefixtures("clock")
    def test_not_reached_handler_does_not_block_queue(self, event_logger, caplog):
        """Проверить: обработчик журнала событий не на пути записей
        учёта — события о папке пишутся без очереди.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        _, evtlog = event_logger
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        event_tracking.directory_ok()

        evtlog.ReportEvent.assert_not_called()
        assert [r.event_id for r in caplog.records if hasattr(r, "event_id")] == [
            2000,
            2001,
        ]
        assert not event_tracking._state.pending  # noqa: SLF001

    def test_lost_outcome_resent_after_timeout(self, tracking_handler, clock, caplog):
        """Проверить: исход 2000 так и не пришёл — через срок ожидания
        такт пишет его снова, а 2001 — после него.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        evtlog = tracking_handler
        handler = logging.getLogger("app.event_tracking").handlers[0]
        with mock.patch.object(handler, "_report"):
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        event_tracking.directory_ok()
        event_tracking.tick()
        evtlog.ReportEvent.assert_not_called()
        clock.advance(event_tracking.OUTCOME_TIMEOUT_SECONDS)
        event_tracking.tick()

        assert [e[0] for e in reported(evtlog)] == [2000, 2001]

    def test_late_outcome_after_timeout(self, tracking_handler, clock, caplog):
        """Проверить: исход пришёл после срока ожидания — повтор не
        ломает очередь.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        evtlog = tracking_handler
        handler = logging.getLogger("app.event_tracking").handlers[0]
        with mock.patch.object(handler, "_report") as report:
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        clock.advance(event_tracking.OUTCOME_TIMEOUT_SECONDS)
        event_tracking.tick()
        # Исход первой попытки — уже после повтора
        *_, on_sent = report.call_args.args
        on_sent()
        event_tracking.directory_ok()

        assert [e[0] for e in reported(evtlog)] == [2000, 2001]
        assert not event_tracking._state.pending  # noqa: SLF001

    def test_outcome_callback_error_stays_in_handler(self, event_logger, caplog):
        """Проверить: ошибка обратного вызова учёта остаётся в emit."""
        test_logger, evtlog = event_logger
        test_logger.warning(
            "событие",
            extra={"event_id": 2000, "event_sent": mock.Mock(side_effect=ValueError)},
        )
        evtlog.ReportEvent.side_effect = OSError("full")
        test_logger.warning(
            "событие",
            extra={
                "event_id": 2000,
                "event_unsent": mock.Mock(side_effect=ValueError),
            },
        )

        errors = [
            r
            for r in caplog.records
            if r.getMessage().startswith("Ошибка учёта исхода")
        ]
        assert len(errors) == 2
        assert all(r.event_log is False and r.exc_info for r in errors)

    def test_handler_confirms_outcome(self):
        """Проверить: EventLogHandler сообщает об исходе записи."""
        assert getattr(logging_setup.EventLogHandler, event_tracking.CONFIRMS_ATTRIBUTE)

    @pytest.mark.usefixtures("clock")
    def test_unsent_reason_change_keeps_reason(self, tracking_handler):
        """Проверить: 2000 о смене причины не записалось — его повтор
        называет прежнюю причину и число её ответов, а 2002 — уже
        только ответы новой.
        """
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = [None, OSError("full"), None, None]
        missing = FileNotFoundError(2, "No such file", "/srv")
        for _ in range(3):
            event_tracking.directory_error("/srv", missing, 404)
        event_tracking.directory_error("/srv", PermissionError(13, "denied"), 403)
        event_tracking.flush()

        sent = reported(evtlog)
        assert [e[0] for e in sent] == [2000, 2000, 2000, 2002]
        assert sent[2][2] == sent[1][2]
        assert re.search(
            r"До смены причины ошибочных ответов с прошлой записи: 2 \(.*No such file",
            sent[2][2],
        )
        assert "denied" in sent[3][2]
        assert sent[3][2].endswith("Ошибочных ответов с прошлой записи: 0")

    def test_unsent_restored_resent_by_tick(self, tracking_handler, clock, caplog):
        """Проверить: 2001 не записалось — такт через минуту пишет его
        снова, а папка остаётся доступной.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = [None, OSError("full"), None]
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        event_tracking.directory_ok()
        event_tracking.tick()
        assert evtlog.ReportEvent.call_count == 2
        clock.advance(event_tracking.UNSENT_RETRY_SECONDS)
        event_tracking.tick()
        event_tracking.flush()

        assert [e[0] for e in reported(evtlog)] == [2000, 2001, 2001]
        assert event_tracking._state.directory is None  # noqa: SLF001

    @pytest.mark.usefixtures("clock")
    def test_flush_resends_before_retry(self, tracking_handler, caplog):
        """Проверить: flush пишет незаписанное 2001 сразу, не дожидаясь
        срока повтора.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = [None, OSError("full"), None]
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        event_tracking.directory_ok()
        event_tracking.flush()

        assert [e[0] for e in reported(evtlog)] == [2000, 2001, 2001]

    @pytest.mark.usefixtures("clock")
    def test_unsent_restored_before_new_unavailable(self, tracking_handler, caplog):
        """Проверить: пока 2001 ждёт повтора, новое 2000 ждёт за ним."""
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = [None, OSError("full"), None, None, None]
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        event_tracking.directory_ok()
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        assert evtlog.ReportEvent.call_count == 2
        event_tracking.flush()

        assert [e[0] for e in reported(evtlog)] == [2000, 2001, 2001, 2000, 2002]

    def test_unsent_unavailable_before_restored(self, tracking_handler, clock, caplog):
        """Проверить: 2000 не записалось — 2001 ждёт за ним и пишется
        после него.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = [OSError("full"), None, None]
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        event_tracking.directory_ok()
        assert evtlog.ReportEvent.call_count == 1
        clock.advance(event_tracking.UNSENT_RETRY_SECONDS)
        event_tracking.tick()

        assert [e[0] for e in reported(evtlog)] == [2000, 2000, 2001]

    def test_repeated_failure_keeps_every_event(self, tracking_handler, clock, caplog):
        """Проверить: незаписанное 2001 снова не записалось — оно не
        теряется за следующими событиями и пишется раньше них.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        evtlog = tracking_handler
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        evtlog.ReportEvent.side_effect = OSError("full")
        event_tracking.directory_ok()
        clock.advance(event_tracking.UNSENT_RETRY_SECONDS)
        event_tracking.tick()
        clock.advance(1)
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        clock.advance(5)
        event_tracking.directory_ok()
        evtlog.ReportEvent.side_effect = None
        clock.advance(event_tracking.UNSENT_RETRY_SECONDS)
        event_tracking.tick()

        sent = reported(evtlog)
        assert [e[0] for e in sent] == [2000, 2001, 2001, 2001, 2000, 2001]
        assert sent[3][2] == sent[1][2]
        assert sent[5][2] != sent[1][2]

    def test_pending_limit(self, tracking_handler, clock, caplog, monkeypatch):
        """Проверить: сверх предела очереди самые старые события о папке
        отбрасываются, а их число указывает следующее записанное.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        monkeypatch.setattr(event_tracking, "PENDING_LIMIT", 2)
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = OSError("full")
        for _ in range(2):
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
            clock.advance(1)
            event_tracking.directory_ok()
        assert evtlog.ReportEvent.call_count == 1
        evtlog.ReportEvent.side_effect = None
        event_tracking.flush()

        sent = reported(evtlog)
        assert [e[0] for e in sent] == [2000, 2000, 2001]
        assert (
            ". Более ранних событий о папке не записано в журнал событий: 2, "
            "они есть в файле журнала\nOSError: [Errno 5] I/O"
        ) in sent[1][2]

    def test_pending_limit_keeps_dropped_count(
        self, tracking_handler, clock, caplog, monkeypatch
    ):
        """Проверить: отброшенное событие передаёт следующему и число
        отброшенных до него.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        monkeypatch.setattr(event_tracking, "PENDING_LIMIT", 2)
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = OSError("full")
        for _ in range(3):
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
            clock.advance(1)
            event_tracking.directory_ok()
        evtlog.ReportEvent.side_effect = None
        event_tracking.flush()

        sent = reported(evtlog)
        assert [e[0] for e in sent] == [2000, 2000, 2001]
        assert "не записано в журнал событий: 4," in sent[1][2]

    @pytest.mark.usefixtures("clock")
    def test_next_waits_for_delayed_result(self, evtlog, caplog):
        """Проверить: исход 2000 станет известен позже (стартовый
        буфер) — 2001 ждёт его и после отказа не обгоняет 2000.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        handler = logging_setup.EventLogHandler()
        tracking_logger = logging.getLogger("app.event_tracking")
        tracking_logger.addHandler(handler)
        evtlog.ReportEvent.side_effect = [OSError("full"), None, None]
        try:
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
            event_tracking.directory_ok()
            handler.send_startup_events()
            assert evtlog.ReportEvent.call_count == 1
            event_tracking.flush()
        finally:
            tracking_logger.removeHandler(handler)
            handler.close()

        assert [e[0] for e in reported(evtlog)] == [2000, 2000, 2001]

    @pytest.mark.usefixtures("clock")
    def test_next_sent_after_delayed_success(self, evtlog, caplog):
        """Проверить: 2000 записано позже (стартовый буфер) — 2001,
        ждавшее его, пишется сразу после, без такта таймера.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        handler = logging_setup.EventLogHandler()
        tracking_logger = logging.getLogger("app.event_tracking")
        tracking_logger.addHandler(handler)
        try:
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
            event_tracking.directory_ok()
            handler.send_startup_events()
        finally:
            tracking_logger.removeHandler(handler)
            handler.close()

        assert [e[0] for e in reported(evtlog)] == [2000, 2001]

    @pytest.mark.usefixtures("clock")
    def test_event_waits_for_running_drain(self, tracking_handler):
        """Проверить: пока очередь пишет другой поток, новое событие о
        папке только встаёт в неё.
        """
        evtlog = tracking_handler
        event_tracking._state.draining = True  # noqa: SLF001
        event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        evtlog.ReportEvent.assert_not_called()
        event_tracking._state.draining = False  # noqa: SLF001
        event_tracking.tick()

        assert [e[0] for e in reported(evtlog)] == [2000]

    @pytest.mark.usefixtures("clock")
    def test_discarded_directory_events_keep_order(self, evtlog, caplog):
        """Проверить: 2000 и 2001 отброшены со стартовым буфером — такт
        пишет оба в том же порядке.
        """
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        handler = logging_setup.EventLogHandler()
        tracking_logger = logging.getLogger("app.event_tracking")
        tracking_logger.addHandler(handler)
        try:
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
            event_tracking.directory_ok()
            handler.discard_startup_events()
            event_tracking.tick()
        finally:
            tracking_logger.removeHandler(handler)
            handler.close()

        assert [e[0] for e in reported(evtlog)] == [2000, 2001]

    def test_failed_reminder_keeps_unsent(self, tracking_handler, clock):
        """Проверить: первое событие и напоминание не записались —
        следующий случай пишется как первый, а не считается повтором.
        """
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = OSError("full")
        for _ in range(2):
            event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "отказ")
        clock.advance(60 * 60)
        event_tracking.tick()
        evtlog.ReportEvent.side_effect = None
        evtlog.ReportEvent.reset_mock()
        event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "отказ")

        assert [e[2] for e in reported(evtlog)] == ["app.event_tracking: отказ"]

    @pytest.mark.usefixtures("clock")
    def test_dropped_over_limit_resends(self, evtlog):
        """Проверить: 2000, отброшенное сверх лимита буфера, пишется
        следующим тактом.
        """
        handler = logging_setup.EventLogHandler()
        tracking_logger = logging.getLogger("app.event_tracking")
        tracking_logger.addHandler(handler)
        try:
            for i in range(logging_setup.STARTUP_BUFFER_LIMIT):
                handler.handle(
                    logging.makeLogRecord(
                        {"name": "app.t", "levelno": 30, "msg": i, "event_id": 2004}
                    )
                )
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
            handler.send_startup_events()
            evtlog.ReportEvent.reset_mock()
            event_tracking.tick()
        finally:
            tracking_logger.removeHandler(handler)
            handler.close()

        assert [e[0] for e in reported(evtlog)] == [2000]

    def test_report_error_keeps_reminder_count(self, tracking_handler, clock):
        """Проверить: напоминание и 2002 не записались — их числа уходят
        следующим напоминанием, а не теряются.
        """
        evtlog = tracking_handler
        for _ in range(3):
            event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "отказ")
            event_tracking.directory_error("/srv", OSError(5, "I/O"), 500)
        clock.advance(60 * 60)
        evtlog.ReportEvent.side_effect = OSError("full")
        event_tracking.tick()
        evtlog.ReportEvent.side_effect = None
        event_tracking.flush()

        texts = [e[2] for e in reported(evtlog)]
        assert (
            "app.event_tracking: Повторялось ещё 2 раз с прошлой записи: отказ" in texts
        )
        assert texts[-2].endswith("Ошибочных ответов с прошлой записи: 2")
        assert texts[-1].endswith("Ошибочных ответов с прошлой записи: 0")

    @pytest.mark.usefixtures("clock")
    def test_persistent_report_error_counts_repeats(self, tracking_handler):
        """Проверить: пока журнал событий отказывает, случаи считаются
        и уходят напоминанием, когда он снова работает.
        """
        evtlog = tracking_handler
        evtlog.ReportEvent.side_effect = OSError("full")
        for _ in range(3):
            event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "отказ")
        evtlog.ReportEvent.side_effect = None
        event_tracking.flush()

        assert [e[2] for e in reported(evtlog)][-1] == (
            "app.event_tracking: Повторялось ещё 3 раз с прошлой записи: отказ"
        )

    def test_buffered_report_error_resends_key(self, evtlog, clock):
        """Проверить: событие из буфера не записалось при отправке —
        такой же случай через минуту снова даёт событие.
        """
        handler = logging_setup.EventLogHandler()
        test_logger = logging.getLogger("app.test_buffer")
        test_logger.addHandler(handler)
        try:
            test_logger.warning("отказ")
            evtlog.ReportEvent.side_effect = [OSError("full"), None, None]
            handler.send_startup_events()
            clock.advance(event_tracking.UNSENT_RETRY_SECONDS)
            test_logger.warning("отказ")
            test_logger.warning("отказ")
        finally:
            test_logger.removeHandler(handler)
            handler.close()

        assert evtlog.ReportEvent.call_count == 2

    def test_startup_buffer_keeps_text_of_the_moment(self, evtlog):
        """Проверить: текст события — на момент записи, а не отправки
        буфера, и запись не держит исключение до отправки.
        """
        handler = logging_setup.EventLogHandler()
        state = {"files": 1}
        error = OSError("сеть")
        record = logging.makeLogRecord(
            {
                "name": "app.t",
                "levelno": 30,
                "msg": "состояние %s",
                "args": (state,),
                "exc_info": (OSError, error, None),
            }
        )
        handler.handle(record)
        state["files"] = 2
        record.exc_info = None

        handler.send_startup_events()
        handler.close()

        [(_, _, text)] = reported(evtlog)
        assert text == "app.t: состояние {'files': 1}\nOSError: сеть"

    def test_startup_buffer(self, evtlog):
        """Проверить: до отправки буфера событий нет, затем — все по
        порядку, а дальше события пишутся сразу.
        """
        handler = logging_setup.EventLogHandler()
        test_logger = logging.getLogger("app.test_buffer")
        test_logger.addHandler(handler)
        try:
            test_logger.warning("первое")
            test_logger.warning("второе", extra={"event_id": 2004})
            evtlog.ReportEvent.assert_not_called()

            handler.send_startup_events()
            test_logger.error("после старта")
        finally:
            test_logger.removeHandler(handler)
            handler.close()

        assert [(e[0], e[2]) for e in reported(evtlog)] == [
            (3001, "app.test_buffer: первое"),
            (2004, "app.test_buffer: второе"),
            (4000, "app.test_buffer: после старта"),
        ]
        evtlog.DeregisterEventSource.assert_called_once_with("source-handle")

    def test_startup_buffer_overflow(self, evtlog):
        """Проверить: сверх 100 записей — одна пометка с числом
        отброшенных.
        """
        handler = logging_setup.EventLogHandler()
        for i in range(105):
            handler.handle(
                logging.makeLogRecord(
                    {"name": "app.t", "levelno": 30, "msg": f"w{i}", "event_id": 2000}
                )
            )
        handler.send_startup_events()
        handler.close()

        events = reported(evtlog)
        assert len(events) == 101
        assert events[99][2] == "app.t: w99"
        assert events[100] == (
            3001,
            2,
            (
                "app.logging_setup: Отброшено записей при запуске: 5, "
                "они есть в файле журнала"
            ),
        )

    def test_buffered_report_error(self, evtlog, caplog):
        """Проверить: ошибка ReportEvent при отправке буфера не мешает
        остальным событиям, а в строке о ней — код события.
        """
        handler = logging_setup.EventLogHandler()
        for event_id, message in ((2004, "a"), (1004, "b")):
            handler.handle(
                logging.makeLogRecord(
                    {
                        "name": "app.t",
                        "levelno": 30,
                        "msg": message,
                        "event_id": event_id,
                    }
                )
            )
        evtlog.ReportEvent.side_effect = [OSError("full"), None]

        handler.send_startup_events()
        handler.close()

        assert evtlog.ReportEvent.call_count == 2
        assert "Не удалось записать событие 2004 в журнал событий: full" in caplog.text

    def test_discard_startup_events(self, evtlog):
        """Проверить: отброшенный буфер не отправляется, дальше
        события пишутся сразу.
        """
        handler = logging_setup.EventLogHandler()
        for i in range(105):
            handler.handle(
                logging.makeLogRecord({"name": "app.t", "levelno": 30, "msg": i})
            )
        handler.discard_startup_events()
        handler.send_startup_events()
        handler.handle(
            logging.makeLogRecord(
                {
                    "name": "app.t",
                    "levelno": 20,
                    "msg": "уже работает",
                    "event_id": 1005,
                }
            )
        )
        handler.close()
        handler.close()

        assert [e[0] for e in reported(evtlog)] == [1005]
        evtlog.DeregisterEventSource.assert_called_once()

    @pytest.mark.parametrize("discard", [False, True])
    def test_lost_first_event_not_a_repeat(self, evtlog, discard):
        """Проверить: запись, не попавшая в журнал событий из-за лимита
        или сброса буфера, не считается потом повтором.
        """
        handler = logging_setup.EventLogHandler()
        warning = logging.makeLogRecord({"name": "app.t", "levelno": 30, "msg": "w"})
        if discard:
            handler.handle(warning)
            handler.discard_startup_events()
        else:
            for i in range(logging_setup.STARTUP_BUFFER_LIMIT):
                handler.handle(
                    logging.makeLogRecord(
                        {"name": "app.t", "levelno": 30, "msg": i, "event_id": 2000}
                    )
                )
            handler.handle(warning)
        handler.send_startup_events()
        evtlog.ReportEvent.reset_mock()

        handler.handle(warning)
        handler.close()

        assert reported(evtlog) == [(3001, 2, "app.t: w")]

    def test_discard_keeps_repeats_and_overflow(self, evtlog, caplog):
        """Проверить: после сброса буфера повторы, посчитанные до него,
        не теряются, а ключ переполнения снова даёт событие с пометкой.
        """
        handler = logging_setup.EventLogHandler()

        def warn(msg):
            handler.handle(
                logging.makeLogRecord({"name": "app.t", "levelno": 30, "msg": msg})
            )

        warn("w")
        warn("w")
        for i in range(event_tracking.MAX_KEYS_PER_EVENT):
            warn(f"вид {i}")
        handler.discard_startup_events()

        warn("w")
        warn("ещё вид")
        event_tracking.flush()
        handler.close()

        assert reported(evtlog) == [
            (3001, 2, "app.t: w"),
            (3001, 2, "app.t: ещё вид" + event_tracking.OVERFLOW_NOTE),
        ]
        # Отброшенный случай и повтор до сброса буфера — в итоговом
        # напоминании
        assert "Повторялось ещё 2 раз с прошлой записи: app.t: w" in caplog.text


class TestWindowsLookups:
    """Путь к профилю и регистрация источника событий."""

    def test_log_file_path(self, monkeypatch):
        """Проверить: путь к файлу — из FOLDERID_LocalAppData."""
        shell = types.SimpleNamespace(
            FOLDERID_LocalAppData="{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}",
            SHGetKnownFolderPath=mock.Mock(return_value=r"C:\Users\op\AppData\Local"),
        )
        package = types.ModuleType("win32com.shell")
        package.shell = shell
        monkeypatch.setitem(sys.modules, "win32com", types.ModuleType("win32com"))
        monkeypatch.setitem(sys.modules, "win32com.shell", package)

        path = REAL_LOG_FILE_PATH()

        assert path == (
            Path(r"C:\Users\op\AppData\Local")
            / "FilePickerAPI"
            / "logs"
            / "FilePickerAPI.log"
        )
        shell.SHGetKnownFolderPath.assert_called_once_with(
            shell.FOLDERID_LocalAppData, 0, 0
        )

    @pytest.mark.parametrize("registered", [True, False])
    def test_event_source_registered(self, monkeypatch, registered):
        """Проверить: источник есть, если есть ключ в HKLM."""
        winreg = types.ModuleType("winreg")
        winreg.HKEY_LOCAL_MACHINE = "HKLM"
        key = mock.Mock()
        winreg.OpenKey = mock.Mock(
            return_value=key if registered else None,
            side_effect=None if registered else FileNotFoundError(2, "missing"),
        )
        monkeypatch.setitem(sys.modules, "winreg", winreg)

        assert REAL_EVENT_SOURCE_REGISTERED() is registered
        winreg.OpenKey.assert_called_once_with(
            "HKLM",
            r"SYSTEM\CurrentControlSet\Services\EventLog\Application\FilePickerAPI",
        )
        if registered:
            key.Close.assert_called_once_with()

    @windows_only
    def test_real_lookups_on_windows(self):
        """Проверить на Windows настоящие вызовы pywin32 и реестра."""
        path = REAL_LOG_FILE_PATH()
        assert path.name == "FilePickerAPI.log"
        assert path.parent.parent.parent.is_dir()
        assert isinstance(REAL_EVENT_SOURCE_REGISTERED(), bool)


class TestEventIds:
    """Коды событий и файл сообщений."""

    def test_event_ids_match_message_file(self):
        """Проверить: каждый код есть в FilePickerAPI.mc и наоборот,
        с тем же символическим именем.
        """
        text = MC_FILE.read_text(encoding="utf-8")
        messages = dict(
            re.findall(r"^MessageId=(\d+)\s*\nSymbolicName=(\w+)", text, re.MULTILINE)
        )
        assert {int(k): v for k, v in messages.items()} == {
            int(e): e.name for e in EventId
        }
        # Текст каждого сообщения — вставка, которую передаёт сервис
        assert text.count("\n%1\n.\n") == len(EventId)

    def test_mark_tracked(self):
        error = OSError("x")
        assert not is_tracked(error)
        assert not is_tracked(None)
        mark_tracked(error)
        assert is_tracked(error)
