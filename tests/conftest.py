"""Общие фикстуры тестов."""

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import logging_setup


@pytest.fixture(autouse=True)
def isolated_logging(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Журнал сервиса в тестах не выходит за пределы теста.

    Файл журнала — во временной папке, а не в профиле пользователя,
    журнала событий нет, а обработчики и уровни логгеров, которые
    поставил setup_logging, после теста снимаются.
    """
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
