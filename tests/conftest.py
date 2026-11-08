"""Общие фикстуры и помощники тестов."""

import errno
import logging
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest import mock

import pytest

from app import event_tracking, logging_setup


class FakeClock:
    """Поддельные часы учёта ошибок: время двигает тест."""

    def __init__(self) -> None:
        """Начать с произвольного момента."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Вернуть текущее время."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Сдвинуть время вперёд."""
        self.now += seconds


def win_error(
    winerror: int, cls: type[OSError] = OSError, filename: str | None = r"\\srv\share"
) -> OSError:
    """Ошибка ОС с кодом Windows на любой ОС."""
    error = cls(errno.EIO, "system message", filename)
    error.winerror = winerror
    return error


def wait_for(condition: Callable[[], object], timeout: float = 10) -> None:
    """Дождаться истинного condition() или провалить тест по сроку."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("условие не выполнилось за отведённое время")
        time.sleep(0.05)


def flush_event_logs() -> None:
    """Дождаться писателей журнала событий: ReportEvent в их потоке."""
    for ref in list(logging._handlerList):  # noqa: SLF001 - все обработчики
        handler = ref()
        if isinstance(handler, logging_setup.EventLogHandler):
            handler.flush()


@pytest.fixture
def service_env(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """runner.run() без выхода процесса и без обработчиков сигналов.

    exits — коды, с которыми процесс завершился бы; stop(reason) —
    функция остановки, которую получили бы обработчики сигналов.
    """
    from app import instance_check, runner, stop_signals

    exits: list[int] = []
    install = mock.Mock()
    monkeypatch.setattr(runner, "_exit", exits.append)
    monkeypatch.setattr(stop_signals, "install", install)
    monkeypatch.setattr(instance_check, "PROBE_INTERVAL", 0.01)

    def stop(reason: str = "тест") -> None:
        install.call_args.args[0](reason)

    return types.SimpleNamespace(exits=exits, stop=stop, install=install)


def start_then_stop(server: object, sockets: object = None) -> None:  # noqa: ARG001 - подпись Server.run
    """InstanceServer.run без Uvicorn: старт, затем остановка."""
    server.started = True
    server.service.on_started()
    server.service.request_stop("тест")


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Поддельные часы вместо event_tracking.clock."""
    fake = FakeClock()
    monkeypatch.setattr(event_tracking, "clock", fake)
    return fake


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Журнал сервиса в тестах не выходит за пределы теста.

    Файл журнала — во временной папке, а не в профиле пользователя,
    журнала событий нет, а обработчики и уровни логгеров, которые
    поставил setup_logging, после теста снимаются. Учёт повторов
    начинается заново, его таймер останавливается.
    """
    event_tracking.reset()
    monkeypatch.setattr(
        logging_setup,
        "log_file_path",
        lambda: tmp_path / "logs" / logging_setup.LOG_FILE_NAME,
    )
    monkeypatch.setattr(logging_setup, "event_source_registered", lambda: False)
    names = ("", *logging_setup.INFO_LOGGERS)
    levels = {name: logging.getLogger(name).level for name in names}
    yield
    root = logging.getLogger()
    for handler in logging_setup._handlers:  # noqa: SLF001 - состояние модуля
        root.removeHandler(handler)
        handler.close()
    logging_setup._handlers.clear()  # noqa: SLF001 - состояние модуля
    for name, level in levels.items():
        logging.getLogger(name).setLevel(level)
    event_tracking.reset()
