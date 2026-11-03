"""Тесты учёта ошибок для журнала событий: повторы, лимит, папка."""

import contextvars
import errno
import logging
import sys
import threading
import types
from unittest import mock

import pytest

from app import event_tracking
from app.event_tracking import EventId
from tests.conftest import win_error


@pytest.fixture
def records(caplog):
    """Записи учёта: (код события или None, текст)."""
    caplog.set_level(logging.INFO, logger="app.event_tracking")

    def collect():
        return [
            (getattr(r, "event_id", None), r.getMessage())
            for r in caplog.records
            if r.name == "app.event_tracking"
        ]

    return collect


def events(records):
    """Только записи, которые идут в журнал событий."""
    return [(event_id, text) for event_id, text in records() if event_id]


@pytest.fixture
def format_message(monkeypatch):
    """Поддельный win32api.FormatMessage."""
    module = types.ModuleType("win32api")
    module.FormatMessage = mock.Mock(side_effect=lambda code: f"Описание {code}.\r\n")
    monkeypatch.setitem(sys.modules, "win32api", module)
    return module.FormatMessage


class TestRepeats:
    """Первое событие сразу, повторы — напоминаниями."""

    @pytest.mark.usefixtures("clock")
    def test_first_case_then_counted(self, records):
        for i in range(3):
            event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", f"ошибка {i}")

        assert events(records) == [(3000, "ошибка 0")]
        # В файл журнала — каждый случай
        assert [text for _, text in records()] == ["ошибка 0", "ошибка 1", "ошибка 2"]

    def test_timer_reminder_without_requests(self, clock, records):
        """500 ошибок за минуту, затем ни одного запроса — через час
        такт таймера даёт напоминание с числом 499.
        """
        for _ in range(500):
            event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "отказ")
        clock.advance(59 * 60)
        event_tracking.tick()
        assert len(events(records)) == 1

        clock.advance(60)
        event_tracking.tick()

        assert events(records)[1] == (
            3000,
            "Повторялось ещё 499 раз с прошлой записи: отказ",
        )

    def test_key_without_repeats_closed(self, clock, records):
        """Ключ без повторов за час закрывается без события, и
        следующая такая же ошибка снова пишется сразу.
        """
        event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "отказ")
        clock.advance(3600)
        event_tracking.tick()
        event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "отказ")

        assert events(records) == [(3000, "отказ"), (3000, "отказ")]

    def test_flush_before_stop(self, clock, records):
        """Остановка: ненулевой счётчик — итоговое напоминание."""
        event_tracking.track(EventId.DIRECTORY_SLOW, "a", "медленно", level=30)
        event_tracking.track(EventId.DIRECTORY_SLOW, "a", "медленно")
        event_tracking.track(EventId.UNEXPECTED_ERROR, "b", "сбой", level=40)
        clock.advance(10)

        event_tracking.flush()

        assert events(records)[-1] == (
            2003,
            "Повторялось ещё 1 раз с прошлой записи: медленно",
        )
        assert len(events(records)) == 3

    def test_track_marks_exception(self, records):
        error = ValueError("x")
        event_tracking.track(EventId.UNEXPECTED_ERROR, "k", "сбой", level=40, exc=error)
        assert event_tracking.is_tracked(error)
        assert events(records) == [(4000, "сбой")]


class TestKeyLimit:
    """Не больше 50 ключей на код события, остальное — переполнение."""

    @pytest.mark.usefixtures("clock")
    def test_thousand_file_ids(self, records):
        """1000 разных fileId при одном отказе — не больше 50 событий
        3000 и одно о переполнении.
        """
        for i in range(1000):
            event_tracking.track(EventId.FILE_ACCESS_ERROR, (5, f"f{i}"), f"f{i}")

        sent = events(records)
        assert len(sent) == 51
        assert sent[50][1].startswith("f50 (видов таких ошибок больше 50:")
        assert len(records()) == 1000

    @pytest.mark.usefixtures("clock")
    def test_overflow_is_one_key(self, records):
        """Коды 5 и 32 сверх лимита — один ключ и одно событие."""
        for i in range(50):
            event_tracking.track(EventId.FILE_ACCESS_ERROR, (5, f"f{i}"), "x")
        event_tracking.track(EventId.FILE_ACCESS_ERROR, (5, "more"), "x")
        event_tracking.track(EventId.FILE_ACCESS_ERROR, (32, "other"), "x")
        assert len(events(records)) == 51

    @pytest.mark.usefixtures("clock")
    def test_hundred_thousand_ids_bounded(self):
        """Поток 100 000 новых fileId — не больше 51 ключа на код."""
        for i in range(100_000):
            event_tracking.admit(EventId.FILE_ACCESS_ERROR, (5, i), "x")
        state = event_tracking._state  # noqa: SLF001 - проверка памяти
        assert len(state.keys[EventId.FILE_ACCESS_ERROR]) == 50
        assert set(state.overflow) == {EventId.FILE_ACCESS_ERROR}
        assert state.overflow[EventId.FILE_ACCESS_ERROR].count == 100_000 - 51

    def test_overflow_reminder_and_close(self, clock, records):
        for i in range(53):
            event_tracking.track(EventId.FILE_ACCESS_ERROR, i, "x")
        clock.advance(3600)
        event_tracking.tick()
        assert events(records)[-1] == (
            3000,
            (
                "Повторялось ещё 2 раз с прошлой записи: ошибки с кодом 3000 "
                "сверх 50 видов (подробности — в файле журнала)"
            ),
        )
        clock.advance(3600)
        event_tracking.tick()
        state = event_tracking._state  # noqa: SLF001 - закрытые ключи
        assert state.overflow == {}
        assert state.keys[EventId.FILE_ACCESS_ERROR] == {}


class TestDirectoryState:
    """Состояние директории: 2000, 2002, 2001."""

    def test_unavailable_reminder_restored(self, clock, records, format_message):
        """100 запросов к недоступной папке — одно 2000, 2002 через
        час, 2001 с длительностью и числом ошибочных ответов.
        """
        error = win_error(53, FileNotFoundError)
        for _ in range(100):
            event_tracking.directory_error(r"\\srv\share", error, 404)
        clock.advance(3600)
        event_tracking.tick()
        # Такт без новых ответов — 2002 нет
        clock.advance(3600)
        event_tracking.tick()
        clock.advance(60)
        event_tracking.directory_ok()

        assert events(records) == [
            (
                2000,
                (
                    r"Папка с файлами \\srv\share недоступна: \\srv\share: код "
                    "ошибки Windows 53 (Описание 53.). Ответ клиенту: 404"
                ),
            ),
            (
                2002,
                (
                    r"Папка с файлами \\srv\share по-прежнему недоступна (1 ч 0 "
                    r"мин): \\srv\share: код ошибки Windows 53 (Описание 53.). "
                    "Ошибочных ответов с прошлой записи: 99"
                ),
            ),
            (
                2001,
                (
                    r"Папка с файлами \\srv\share снова доступна: была недоступна "
                    "2 ч 1 мин, ошибочных ответов: 100"
                ),
            ),
        ]
        assert len(records()) == 102
        format_message.assert_called_with(53)
        assert event_tracking.is_tracked(error)

    @pytest.mark.usefixtures("clock")
    def test_reason_change(self, records):
        """Смена причины 404 → 403 — новое 2000."""
        missing = FileNotFoundError(errno.ENOENT, "No such file", "/srv")
        denied = PermissionError(errno.EACCES, "Permission denied", "/srv")
        event_tracking.directory_error("/srv", missing, 404)
        event_tracking.directory_error("/srv", missing, 404)
        event_tracking.directory_error("/srv", denied, 403)
        event_tracking.directory_ok()

        sent = events(records)
        assert [e for e, _ in sent] == [2000, 2000, 2001]
        assert "Ответ клиенту: 403" in sent[1][1]
        # Повторы прежней причины не теряются при смене
        assert sent[1][1].endswith(
            "До смены причины ошибочных ответов с прошлой записи: 1 "
            "([Errno 2] No such file: '/srv')"
        )
        assert sent[2][1].endswith("ошибочных ответов: 3")

    def test_reason_change_then_stop(self, clock, records):
        """100 ответов 404, затем 403 и остановка — повторы 404 в
        2000 о смене причины.
        """
        missing = FileNotFoundError(errno.ENOENT, "No such file", "/srv")
        denied = PermissionError(errno.EACCES, "Permission denied", "/srv")
        for _ in range(100):
            event_tracking.directory_error("/srv", missing, 404)
        event_tracking.directory_error("/srv", denied, 403)
        clock.advance(10)
        event_tracking.flush()

        sent = events(records)
        assert [e for e, _ in sent] == [2000, 2000, 2002]
        assert "ошибочных ответов с прошлой записи: 99 " in sent[1][1]
        assert "Ошибочных ответов с прошлой записи: 0" in sent[2][1]

    @pytest.mark.usefixtures("clock")
    @pytest.mark.parametrize(
        ("status", "traceback"), [(404, False), (500, True), ("206, оборван", True)]
    )
    def test_traceback_for_server_error(self, caplog, status, traceback):
        """Трассировка исходной ошибки — только для ответа 5xx."""
        caplog.set_level(logging.INFO, logger="app.event_tracking")
        error = OSError(errno.EIO, "I/O")
        event_tracking.directory_error("/srv", error, status)
        event_tracking.file_error("/srv", "a.txt", error, status)
        exc_infos = [r.exc_info for r in caplog.records]
        assert len(exc_infos) == 2
        for exc_info in exc_infos:
            assert (exc_info is not None) is traceback
            assert exc_info is None or exc_info[1] is error

    def test_stop_while_unavailable(self, clock, records):
        """Папка недоступна, 300 ошибочных ответов, остановка —
        итоговое 2002 до события остановки.
        """
        error = OSError(errno.EIO, "I/O error")
        for _ in range(300):
            event_tracking.directory_error("/srv", error, 500)
        clock.advance(125)

        event_tracking.flush()

        assert events(records)[-1] == (
            2002,
            (
                "Папка с файлами /srv по-прежнему недоступна (2 мин 5 с): "
                "[Errno 5] I/O error. Ошибочных ответов с прошлой записи: 299"
            ),
        )

    @pytest.mark.usefixtures("clock")
    def test_ok_when_available(self, records):
        event_tracking.directory_ok()
        assert records() == []

    @pytest.mark.usefixtures("clock")
    def test_ok_after_parallel_restore(self, monkeypatch, records):
        """Папку уже отметил доступной другой поток — 2001 не второй."""
        event_tracking.directory_error("/srv", OSError(errno.EIO, "I/O"), 500)
        state = event_tracking._state  # noqa: SLF001 - состояние модуля
        lock = state.directory_lock

        class RestoredMeanwhile:
            def __enter__(self):
                lock.acquire()
                state.directory = None

            def __exit__(self, *exc_info):
                lock.release()

        monkeypatch.setattr(state, "directory_lock", RestoredMeanwhile())
        event_tracking.directory_ok()
        assert [event_id for event_id, _ in events(records)] == [2000]


@pytest.mark.usefixtures("clock")
class TestAccessOrder:
    """Состояние папки — по началу обращения, а не по записи итога."""

    @staticmethod
    def request():
        """Контекст запроса с начатым обращением к папке."""
        context = contextvars.copy_context()
        context.run(event_tracking.begin_access)
        return context

    def test_stale_success_not_restored(self, records):
        """A прочитал папку и задержался, B позже получил ошибку: успех
        A не даёт 2001; новое успешное обращение — даёт.
        """
        first, second = self.request(), self.request()
        second.run(
            event_tracking.directory_error, "/srv", OSError(errno.EIO, "I/O"), 500
        )
        first.run(event_tracking.directory_ok)
        assert [e for e, _ in events(records)] == [2000]

        self.request().run(event_tracking.directory_ok)
        assert [e for e, _ in events(records)] == [2000, 2001]

    def test_stale_error_not_unavailable(self, records):
        """A начал раньше и получил ошибку после успеха B: 2000 нет,
        ошибка — в файле журнала.
        """
        first, second = self.request(), self.request()
        second.run(event_tracking.directory_ok)
        error = OSError(errno.EIO, "I/O")
        first.run(event_tracking.directory_error, "/srv", error, 500)
        assert events(records) == []
        assert len(records()) == 1
        assert event_tracking.is_tracked(error)

        self.request().run(event_tracking.directory_error, "/srv", error, 500)
        assert [e for e, _ in events(records)] == [2000]

    def test_without_begin_access(self, records):
        """Без begin_access — номер момента вызова (как раньше)."""
        event_tracking.directory_ok()
        event_tracking.directory_error("/srv", OSError(errno.EIO, "I/O"), 500)
        event_tracking.directory_ok()
        assert [e for e, _ in events(records)] == [2000, 2001]


class TestLockOrder:
    """В журнал не пишется под блокировкой ключей повторов.

    EventLogHandler под своей блокировкой вызывает admit: запись под
    блокировкой ключей дала бы взаимную блокировку потоков.
    """

    def test_no_logging_under_keys_lock(self, clock):
        locked = []

        class Probe(logging.Handler):
            def emit(self, _record):
                locked.append(event_tracking._state.lock.locked())  # noqa: SLF001

        probe = Probe()
        tracking_logger = logging.getLogger("app.event_tracking")
        tracking_logger.addHandler(probe)
        try:
            error = OSError(errno.EIO, "I/O")
            for _ in range(2):
                event_tracking.track(EventId.FILE_ACCESS_ERROR, "key", "ошибка")
                event_tracking.directory_error("/srv", error, 500)
            clock.advance(event_tracking.REMINDER_SECONDS)
            event_tracking.tick()
            event_tracking.flush()
            event_tracking.directory_ok()
        finally:
            tracking_logger.removeHandler(probe)
        assert locked
        assert not any(locked)


class TestFileErrors:
    """Ошибки скачивания: сетевые — 2000, прочие — 3000."""

    @pytest.mark.usefixtures("clock", "format_message")
    def test_network_error_is_directory(self, records):
        error = win_error(64)
        event_tracking.file_error(r"\\srv\share", "a.txt", error, 500)
        assert [e for e, _ in events(records)] == [2000]
        assert event_tracking.is_tracked(error)

    @pytest.mark.usefixtures("clock")
    def test_other_error_is_file_access(self, records):
        error = PermissionError(errno.EACCES, "Permission denied", "/srv/a.txt")
        event_tracking.file_error("/srv", "a.txt", error, 403)
        assert events(records) == [
            (
                3000,
                (
                    "Ошибка доступа к файлу 'a.txt' в папке /srv: [Errno 13] "
                    "Permission denied: '/srv/a.txt'. Ответ клиенту: 403"
                ),
            )
        ]
        assert event_tracking.is_tracked(error)


class TestDescribeError:
    """Текст ошибки для журнала."""

    def test_winerror_with_description(self, format_message):
        error = win_error(5, filename=None)
        assert event_tracking.describe_error(error) == (
            "код ошибки Windows 5 (Описание 5.)"
        )
        format_message.assert_called_once_with(5)

    def test_format_message_unavailable(self, monkeypatch):
        """Без pywin32 (вне Windows) — описание из самой ошибки."""
        monkeypatch.setitem(sys.modules, "win32api", None)
        assert event_tracking.describe_error(win_error(53)) == (
            r"\\srv\share: код ошибки Windows 53 (system message)"
        )
        error = win_error(53)
        error.strerror = None
        assert event_tracking.describe_error(error).endswith("53 ()")

    def test_plain_errors(self):
        assert event_tracking.describe_error(ValueError("bad")) == "ValueError: bad"
        error = OSError(errno.EIO, "I/O error")
        assert event_tracking.describe_error(error) == "[Errno 5] I/O error"

    @pytest.mark.parametrize(
        ("seconds", "text"),
        [(5, "5 с"), (125, "2 мин 5 с"), (3725, "1 ч 2 мин")],
    )
    def test_format_duration(self, seconds, text):
        assert event_tracking.format_duration(seconds) == text


class TestSlow:
    """Медленная папка: больше 10 секунд — 2003."""

    def test_fast_request(self, clock, records):
        started = clock.now
        clock.advance(10)
        event_tracking.check_slow("GET /files", started)
        assert records() == []

    def test_slow_request(self, clock, records):
        started = clock.now
        clock.advance(10.5)
        event_tracking.check_slow("GET /files", started)
        event_tracking.check_slow("GET /files/{fileId}", started, size=2048)
        assert events(records) == [
            (2003, "Медленный ответ сетевой папки: GET /files за 10.5 с"),
            (
                2003,
                (
                    "Медленный ответ сетевой папки: GET /files/{fileId} за 10.5 с, "
                    "размер файла 2048 байт"
                ),
            ),
        ]


class TestTimer:
    """Демонический такт таймера."""

    def test_timer_ticks_and_survives_errors(self, monkeypatch, caplog):
        monkeypatch.setattr(event_tracking, "TICK_SECONDS", 0.01)
        ticked = threading.Event()
        calls = []

        def fake_tick():
            calls.append(1)
            if len(calls) == 1:
                msg = "сбой такта"
                raise RuntimeError(msg)
            ticked.set()

        monkeypatch.setattr(event_tracking, "tick", fake_tick)
        event_tracking.start_timer()
        event_tracking.start_timer()
        assert ticked.wait(5)
        timer = event_tracking._state.timer  # noqa: SLF001 - поток таймера
        assert timer.daemon

        event_tracking.reset()
        timer.join(5)
        assert not timer.is_alive()
        assert "Ошибка такта учёта повторов" in caplog.text
        [record] = [r for r in caplog.records if r.name == "app.event_tracking"]
        assert record.event_log is False
