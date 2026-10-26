"""Пробная служба Windows на pywin32 для проверки сборки Nuitka.

Проверяет то, на чём держится дизайн изменения windows-service
(задачи 1.2–1.4), без самого приложения:
- работу службы через servicemanager в сборке Nuitka standalone;
- запись события с кириллицей от источника со своим файлом сообщений;
- переход в консольный режим при запуске не диспетчером служб (1063);
- выполняется ли __init__.py пакета раньше кода точки входа.

Без аргументов под диспетчером служб работает службой, иначе выводит
отчёт в JSON и завершается с кодом 0.
"""

import json
import sys
import threading
import time

# До любых импортов пакета: если сборка уже выполнила __init__.py,
# пакет есть в sys.modules
PACKAGE_PRELOADED = "probe_pkg" in sys.modules
ENTRY_STARTED_AT = time.perf_counter()

from pathlib import Path  # noqa: E402

import pywintypes  # noqa: E402
import servicemanager  # noqa: E402
import win32api  # noqa: E402
import win32event  # noqa: E402
import win32evtlog  # noqa: E402
import win32security  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402

SERVICE_NAME = "FilePickerProbe"
EVENT_SOURCE = "FilePickerProbe"
EVENT_STARTED = 1000
EVENT_STOPPED = 1001
# Текст с кириллицей и «ё»: проверяется побайтно в Get-WinEvent
STARTED_TEXT = "Пробная служба запущена: проверка кириллицы ёЁ"
STOPPED_TEXT = "Пробная служба остановлена"
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063


def report_event(event_id: int, text: str) -> None:
    """Записать событие «Сведения» от источника пробной службы."""
    handle = win32evtlog.RegisterEventSource(None, EVENT_SOURCE)
    try:
        win32evtlog.ReportEvent(
            handle,
            win32evtlog.EVENTLOG_INFORMATION_TYPE,
            0,
            event_id,
            None,
            [text],
            None,
        )
    finally:
        win32evtlog.DeregisterEventSource(handle)


class ProbeService(win32serviceutil.ServiceFramework):
    """Минимальная служба: событие при запуске и при остановке."""

    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = "File Picker API probe"

    def __init__(self, args: list[str]) -> None:
        """Создать событие остановки."""
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)

    def SvcStop(self) -> None:  # noqa: N802 - имя задаёт pywin32
        """Остановить службу по команде диспетчера служб."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self) -> None:  # noqa: N802 - имя задаёт pywin32
        """Сообщить о запуске, записать события и ждать остановки."""
        self.ReportServiceStatus(win32service.SERVICE_RUNNING)
        report_event(EVENT_STARTED, STARTED_TEXT)
        win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)
        report_event(EVENT_STOPPED, STOPPED_TEXT)


def path_info(path: str | None) -> dict[str, object]:
    """Вернуть путь и признак, что такой файл есть."""
    return {"path": path, "exists": bool(path) and Path(path).exists()}


def owner_sid(path: str) -> str:
    """Вернуть SID владельца файла (проверка win32security)."""
    descriptor = win32security.GetFileSecurity(
        path, win32security.OWNER_SECURITY_INFORMATION
    )
    return win32security.ConvertSidToStringSid(descriptor.GetSecurityDescriptorOwner())


def console_report() -> dict[str, object]:
    """Собрать отчёт консольного режима.

    Какой путь указывает на сам exe в сборке Nuitka, заранее неизвестно:
    sys.executable там может быть путём к несуществующему python.exe.
    Поэтому в отчёте все варианты, а настоящий путь берётся у Windows.
    """
    package = sys.modules.get("probe_pkg")
    imported_at = getattr(package, "IMPORTED_AT", None)
    module_path = win32api.GetModuleFileName(None)
    compiled = globals().get("__compiled__")
    return {
        "mode": "console",
        "package_preloaded": PACKAGE_PRELOADED,
        "package_imported_before_entry": (
            imported_at is not None and imported_at <= ENTRY_STARTED_AT
        ),
        "compiled": compiled is not None,
        "module_file_name": path_info(module_path),
        "sys_executable": path_info(sys.executable),
        "sys_argv0": path_info(sys.argv[0] if sys.argv else None),
        "compiled_containing_dir": getattr(compiled, "containing_dir", None),
        "module_file_owner_sid": owner_sid(module_path),
        "pywintypes": pywintypes.__file__,
        "threads": threading.active_count(),
    }


def main() -> int:
    """Запуститься службой или, если это не служба, вывести отчёт."""
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(ProbeService)
    try:
        servicemanager.StartServiceCtrlDispatcher()
    except pywintypes.error as error:
        if error.winerror != ERROR_FAILED_SERVICE_CONTROLLER_CONNECT:
            raise
        sys.stdout.write(json.dumps(console_report(), ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
