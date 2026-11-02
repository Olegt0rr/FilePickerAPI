"""Коды событий журнала событий Windows и пометка учтённых ошибок.

Коды постоянные: для каждого в app/messages/FilePickerAPI.mc есть
сообщение, иначе Event Viewer показал бы «Не найдено описание». Коды
только добавляются, смысл существующих не меняется.
"""

from enum import IntEnum

# Атрибут исключения, уже записанного в журнал событий: запись с таким
# исключением повторного события не даёт
TRACKED_ATTRIBUTE = "_filepicker_event_tracked"


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
