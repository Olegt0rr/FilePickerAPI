"""Общие фикстуры и помощники тестов."""

import errno
import logging
from collections.abc import Iterator
from pathlib import Path

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
