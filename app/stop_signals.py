"""Сигналы остановки сервиса.

Ctrl+C обрабатывается одним путём на платформу: на Windows —
консольным обработчиком SetConsoleCtrlHandler (он вызывается раньше
обработчика Python, и KeyboardInterrupt не возникает), вне Windows —
обработчиками SIGINT и SIGTERM в главном потоке. Все они вызывают одну
функцию остановки. Модули pywin32 импортируются лениво и только на
Windows.
"""

import logging
import signal
import sys
from collections.abc import Callable
from types import FrameType

logger = logging.getLogger(__name__)

# События консоли (wincon.h); свои константы, чтобы тестам не
# подделывать win32con
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1

_CONSOLE_REASONS = {
    CTRL_C_EVENT: "Ctrl+C в консоли",
    CTRL_BREAK_EVENT: "Ctrl+Break в консоли",
}

# Обработчик консоли живёт до конца процесса: ссылка не даёт его
# собрать, пока Windows может его вызвать
_console_handler: Callable[[int], bool] | None = None


def console_handler(stop: Callable[[str], object], ctrl_type: int) -> bool:
    """Обработать событие консоли; True — событие обработано.

    Прочие события (закрытие консоли, выход, выключение) идут дальше
    по цепочке обработчиков.
    """
    reason = _CONSOLE_REASONS.get(ctrl_type)
    if reason is None:
        return False
    stop(reason)
    return True


def _install_console_handler(stop: Callable[[str], object]) -> None:
    global _console_handler  # noqa: PLW0603 - один обработчик на процесс
    import win32api

    def handler(ctrl_type: int) -> bool:
        return console_handler(stop, ctrl_type)

    try:
        win32api.SetConsoleCtrlHandler(handler, True)  # noqa: FBT003 - API pywin32
    except Exception as e:
        logger.warning("Не удалось установить обработчик Ctrl+C: %s", e)
        return
    _console_handler = handler


def _install_signal_handlers(stop: Callable[[str], object]) -> None:
    def handler(signum: int, _frame: FrameType | None) -> None:
        stop(f"сигнал {signal.Signals(signum).name}")

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, handler)


def _windows() -> bool:
    return sys.platform == "win32"


def install(stop: Callable[[str], object]) -> None:
    """Установить обработчики остановки (из главного потока).

    stop(reason) — идемпотентная функция остановки сервиса.
    """
    if _windows():
        _install_console_handler(stop)
    else:
        _install_signal_handlers(stop)
