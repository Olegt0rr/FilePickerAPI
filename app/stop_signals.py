"""Сигналы остановки сервиса.

Ctrl+C обрабатывается одним путём на платформу: на Windows —
консольным обработчиком SetConsoleCtrlHandler (он вызывается раньше
обработчика Python, и KeyboardInterrupt не возникает), вне Windows —
обработчиками SIGINT и SIGTERM в главном потоке. На Windows ещё есть
скрытое окно верхнего уровня FilePickerAPI.Instance: без окна процесс
не получил бы сообщений о завершении сеанса и о закрытии от Restart
Manager. Обычные сигналы вызывают функцию остановки stop, срочные
(закрытие консоли, завершение сеанса) — urgent_stop, которая не
возвращается до завершения процесса. Модули pywin32 импортируются
лениво и только на Windows.
"""

import functools
import logging
import signal
import sys
import threading
from collections.abc import Callable
from types import FrameType
from typing import NamedTuple

logger = logging.getLogger(__name__)

# События консоли (wincon.h); свои константы, чтобы тестам не
# подделывать win32con
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2

# Сообщения окна и флаги lParam у WM_ENDSESSION (winuser.h)
WM_CLOSE = 0x0010
WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
ENDSESSION_CLOSEAPP = 0x00000001
ENDSESSION_LOGOFF = 0x80000000

# Класс скрытого окна: по нему окно находят WM_CLOSE и дымовая проверка
WINDOW_CLASS = "FilePickerAPI.Instance"
# Сколько ждать создания окна до опроса порта
WINDOW_READY_SECONDS = 5.0

_CONSOLE_REASONS = {
    CTRL_C_EVENT: "Ctrl+C в консоли",
    CTRL_BREAK_EVENT: "Ctrl+Break в консоли",
}

# Обработчик консоли живёт до конца процесса: ссылка не даёт его
# собрать, пока Windows может его вызвать
_console_handler: Callable[[int], bool] | None = None


class StopHandlers(NamedTuple):
    """Функции остановки сервиса.

    stop(reason) — обычная остановка, возвращается сразу;
    urgent_stop(reason) — срочная, возвращается только в тестах:
    процесс завершается раньше.
    """

    stop: Callable[[str], object]
    urgent_stop: Callable[[str], object]


def console_handler(handlers: StopHandlers, ctrl_type: int) -> bool:
    """Обработать событие консоли; True — событие обработано.

    Закрытие окна консоли — срочная остановка: Windows завершит
    процесс сразу после возврата из обработчика. Выход и выключение
    (CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT) процессу, загрузившему
    user32.dll, не приходят: их получает скрытое окно как WM_ENDSESSION.
    Прочие события идут дальше по цепочке обработчиков.
    """
    if ctrl_type == CTRL_CLOSE_EVENT:
        handlers.urgent_stop("закрытие консоли")
        return True
    reason = _CONSOLE_REASONS.get(ctrl_type)
    if reason is None:
        return False
    handlers.stop(reason)
    return True


def end_session_reason(lparam: int) -> str:
    """Причина остановки по lParam сообщения WM_ENDSESSION."""
    if lparam & ENDSESSION_CLOSEAPP:
        # Restart Manager закрывает программу для Windows Installer
        return "обновление или удаление программы"
    if lparam & ENDSESSION_LOGOFF:
        return "выход пользователя"
    return "выключение Windows"


def window_procedure(
    handlers: StopHandlers, hwnd: int, message: int, wparam: int, lparam: int
) -> int:
    """Обработать сообщение скрытого окна.

    WM_QUERYENDSESSION ничего не останавливает: выход могут отменить,
    и тогда придёт WM_ENDSESSION с wParam = FALSE, на который сервис
    тоже ничего не делает и продолжает работать.
    """
    if message == WM_CLOSE:
        handlers.stop("сообщение WM_CLOSE")
        return 0
    if message == WM_QUERYENDSESSION:
        return 1
    if message == WM_ENDSESSION:
        if wparam:
            handlers.urgent_stop(end_session_reason(lparam))
        return 0
    import win32gui

    return win32gui.DefWindowProc(hwnd, message, wparam, lparam)


def _run_window(handlers: StopHandlers, ready: threading.Event) -> None:
    """Создать скрытое окно и обрабатывать его сообщения.

    Окно верхнего уровня, а не HWND_MESSAGE: окна «только для
    сообщений» не получают широковещательных WM_QUERYENDSESSION и
    WM_ENDSESSION, а Restart Manager ищет окна верхнего уровня. Стиль
    без WS_VISIBLE: окно не показывается.
    """
    try:
        import win32api
        import win32gui

        window_class = win32gui.WNDCLASS()
        window_class.hInstance = win32api.GetModuleHandle(None)
        window_class.lpszClassName = WINDOW_CLASS
        window_class.lpfnWndProc = functools.partial(window_procedure, handlers)
        win32gui.RegisterClass(window_class)
        # Стиль 0, нулевые размеры, без родителя и меню
        position = (0, 0, 0, 0)
        win32gui.CreateWindow(
            WINDOW_CLASS, WINDOW_CLASS, 0, *position, 0, 0, window_class.hInstance, None
        )
    except Exception as e:
        logger.warning("Не удалось создать скрытое окно сервиса: %s", e)
        return
    finally:
        ready.set()
    win32gui.PumpMessages()


def _install_window(handlers: StopHandlers) -> None:
    """Создать скрытое окно в отдельном потоке с циклом сообщений."""
    ready = threading.Event()
    threading.Thread(
        target=_run_window, args=(handlers, ready), name="instance-window", daemon=True
    ).start()
    # Окно должно быть до опроса порта: он длится до 10 секунд
    ready.wait(WINDOW_READY_SECONDS)


def _install_console_handler(handlers: StopHandlers) -> None:
    global _console_handler  # noqa: PLW0603 - один обработчик на процесс
    import win32api

    def handler(ctrl_type: int) -> bool:
        return console_handler(handlers, ctrl_type)

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


def install(
    stop: Callable[[str], object], urgent_stop: Callable[[str], object]
) -> None:
    """Установить обработчики остановки (из главного потока).

    stop(reason) — идемпотентная функция обычной остановки,
    urgent_stop(reason) — срочной.
    """
    if _windows():
        handlers = StopHandlers(stop, urgent_stop)
        _install_console_handler(handlers)
        _install_window(handlers)
    else:
        _install_signal_handlers(stop)
