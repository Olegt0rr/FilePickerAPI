"""Проверка прав на папку сборки при запуске сервиса.

Права на папку в Program Files ставит Windows Installer, но их могли
изменить потом. Сервис проверяет папку своего exe, её содержимое и
каждую папку-предка до корня тома: менять их могут только SYSTEM,
Administrators и TrustedInstaller. При нарушении сервис пишет
предупреждение (событие 1003) и продолжает работу.

Сравнение прав — чистая функция find_violations, её можно проверить
без Windows. Чтение прав (check_build_folder) импортирует
win32security внутри функций, поэтому модуль импортируется на любой
ОС.
"""

import functools
import os
import stat
from collections.abc import Callable, Iterable, Iterator
from enum import Enum, StrEnum, auto
from pathlib import Path
from typing import NamedTuple

# Код события «права на папку сборки нарушены»; с появлением
# app/event_tracking.py — EventId.BUILD_FOLDER_PERMISSIONS
BUILD_FOLDER_PERMISSIONS_EVENT_ID = 1003

# SYSTEM, BUILTIN\Administrators и NT SERVICE\TrustedInstaller
TRUSTED_SIDS = frozenset(
    {
        "S-1-5-18",
        "S-1-5-32-544",
        "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }
)

# Разрешающие ACE: обычный, составной, объектный и условные. Запрещающие
# не учитываются — это ошибка в безопасную сторону
_ACCESS_ALLOWED_ACE_TYPES = frozenset({0x0, 0x4, 0x5, 0x9, 0xB})
# ACE действует только на дочерние объекты, а не на сам объект
INHERIT_ONLY_ACE = 0x08

FILE_WRITE_DATA = 0x2
FILE_APPEND_DATA = 0x4
FILE_WRITE_EA = 0x10
FILE_DELETE_CHILD = 0x40
FILE_WRITE_ATTRIBUTES = 0x100
DELETE = 0x10000
WRITE_DAC = 0x40000
WRITE_OWNER = 0x80000
GENERIC_ALL = 0x10000000
GENERIC_WRITE = 0x40000000

# Имена прав в событии; для директорий FILE_WRITE_DATA и
# FILE_APPEND_DATA — это FILE_ADD_FILE и FILE_ADD_SUBDIRECTORY
_RIGHT_NAMES = (
    (FILE_WRITE_DATA, "FILE_WRITE_DATA"),
    (FILE_APPEND_DATA, "FILE_APPEND_DATA"),
    (FILE_WRITE_EA, "FILE_WRITE_EA"),
    (FILE_DELETE_CHILD, "FILE_DELETE_CHILD"),
    (FILE_WRITE_ATTRIBUTES, "FILE_WRITE_ATTRIBUTES"),
    (DELETE, "DELETE"),
    (WRITE_DAC, "WRITE_DAC"),
    (WRITE_OWNER, "WRITE_OWNER"),
    (GENERIC_ALL, "GENERIC_ALL"),
    (GENERIC_WRITE, "GENERIC_WRITE"),
)

# Запись, удаление, смена прав и владельца содержимого папки сборки
_CONTENT_MASK = (
    FILE_WRITE_DATA
    | FILE_APPEND_DATA
    | FILE_WRITE_EA
    | FILE_WRITE_ATTRIBUTES
    | DELETE
    | WRITE_DAC
    | WRITE_OWNER
    | GENERIC_WRITE
    | GENERIC_ALL
)
# У предка опасно то, что позволяет удалить или переименовать папку
# сборки и подложить свою. Право создавать папки (FILE_APPEND_DATA,
# есть у пользователей на C:\) не опасно
_ANCESTOR_MASK = DELETE | FILE_DELETE_CHILD | WRITE_DAC | WRITE_OWNER | GENERIC_ALL


class ObjectKind(Enum):
    """Роль объекта в проверке: от неё зависят опасные права."""

    # Папка сборки и папки внутри неё
    DIRECTORY = auto()
    # Файлы в папке сборки
    FILE = auto()
    # Папки над папкой сборки до корня тома
    ANCESTOR = auto()


_DANGEROUS_MASKS = {
    ObjectKind.DIRECTORY: _CONTENT_MASK | FILE_DELETE_CHILD,
    ObjectKind.FILE: _CONTENT_MASK,
    ObjectKind.ANCESTOR: _ANCESTOR_MASK,
}


class Ace(NamedTuple):
    """Элемент DACL."""

    sid: str
    ace_type: int
    flags: int
    mask: int


class ObjectSecurity(NamedTuple):
    """Права объекта, прочитанные для проверки.

    dacl: None — DACL отсутствует или NULL (полный доступ у всех),
    пустой кортеж — пустая DACL (доступа нет ни у кого).
    """

    path: str
    kind: ObjectKind
    owner: str | None
    dacl: tuple[Ace, ...] | None
    reparse_point: bool = False


class Problem(StrEnum):
    """Вид нарушения."""

    NULL_DACL = "полный доступ у всех (нет DACL)"
    OWNER = "владелец не из доверенных"
    RIGHTS = "опасные права"
    REPARSE_POINT = "точка повторной обработки (junction или символическая ссылка)"
    UNREADABLE = "права не удалось проверить"


class Violation(NamedTuple):
    """Нарушение прав на объект.

    account — SID владельца или учётной записи с опасными правами
    (None, если владельца нет), rights — имена опасных прав, detail —
    текст ошибки чтения.
    """

    path: str
    problem: Problem
    account: str | None = None
    rights: tuple[str, ...] = ()
    detail: str = ""


def _right_names(mask: int) -> tuple[str, ...]:
    return tuple(name for bit, name in _RIGHT_NAMES if mask & bit)


def _object_violations(
    obj: ObjectSecurity, trusted: frozenset[str]
) -> Iterator[Violation]:
    if obj.reparse_point:
        yield Violation(obj.path, Problem.REPARSE_POINT)
    if obj.owner not in trusted:
        yield Violation(obj.path, Problem.OWNER, obj.owner)
    if obj.dacl is None:
        yield Violation(obj.path, Problem.NULL_DACL)
        return
    # Права одной учётной записи из разных ACE — одним нарушением
    masks: dict[str, int] = {}
    for ace in obj.dacl:
        if (
            ace.ace_type not in _ACCESS_ALLOWED_ACE_TYPES
            or ace.flags & INHERIT_ONLY_ACE
            or ace.sid in trusted
        ):
            continue
        dangerous = ace.mask & _DANGEROUS_MASKS[obj.kind]
        if dangerous:
            masks[ace.sid] = masks.get(ace.sid, 0) | dangerous
    for sid, mask in masks.items():
        yield Violation(obj.path, Problem.RIGHTS, sid, _right_names(mask))


def find_violations(
    objects: Iterable[ObjectSecurity], trusted: frozenset[str] = TRUSTED_SIDS
) -> list[Violation]:
    """Найти нарушения прав на папку сборки.

    Учитываются только разрешающие ACE, которые действуют на сам
    объект: ACE «только для наследования» пропускаются. Пустая DACL
    нарушением не считается, отсутствующая или NULL — нарушение
    «полный доступ у всех» без обхода ACE.

    Args:
        objects: Папка сборки, её содержимое и предки до корня тома
        trusted: SID-ы, которым права разрешены

    Returns:
        Нарушения в порядке объектов

    """
    return [
        violation for obj in objects for violation in _object_violations(obj, trusted)
    ]


def _lookup_account_name(sid: str) -> str:
    """Вернуть имя учётной записи с доменом или сам SID."""
    import pywintypes
    import win32security

    try:
        name, domain, _type = win32security.LookupAccountSid(
            None, win32security.ConvertStringSidToSid(sid)
        )
    except pywintypes.error:
        return sid
    return f"{domain}\\{name}" if domain else name


# Сообщение журнала событий ограничено ~32 тысячами символов, а
# нарушение на папке сборки наследуется всем её файлам
_MAX_MESSAGE_LINES = 50


def format_violations(
    violations: list[Violation],
    account_name: Callable[[str], str] = _lookup_account_name,
) -> str:
    """Составить текст события 1003.

    Args:
        violations: Непустой список нарушений
        account_name: Имя учётной записи по SID

    Returns:
        Текст предупреждения с перечнем объектов, учётных записей и прав

    """
    # Имя доменной учётной записи — запрос к контроллеру домена, а одна
    # запись обычно повторяется во многих строках
    account_name = functools.cache(account_name)
    lines = [
        (
            "Права на папку сборки нарушены: запись, удаление, смену прав и "
            "владельца должны иметь только SYSTEM, Administrators и "
            "TrustedInstaller. Сервис продолжает работу."
        )
    ]
    for violation in violations[:_MAX_MESSAGE_LINES]:
        text = f"{violation.path}: {violation.problem}"
        if violation.problem is Problem.OWNER:
            owner = (
                "нет" if violation.account is None else account_name(violation.account)
            )
            text += f" ({owner})"
        elif violation.problem is Problem.RIGHTS:
            account = account_name(violation.account)
            text += f" у {account}: {', '.join(violation.rights)}"
        elif violation.problem is Problem.UNREADABLE:
            text += f": {violation.detail}"
        lines.append(f"- {text}")
    if len(violations) > _MAX_MESSAGE_LINES:
        lines.append(f"…и ещё нарушений: {len(violations) - _MAX_MESSAGE_LINES}")
    return "\n".join(lines)


# IsReparseTagNameSurrogate: тег junction (IO_REPARSE_TAG_MOUNT_POINT)
# и символической ссылки (IO_REPARSE_TAG_SYMLINK)
_NAME_SURROGATE_TAG = 0x20000000


def _is_reparse_point(stat_result: os.stat_result) -> bool:
    """Проверить, что объект — junction или символическая ссылка.

    os.lstat и DirEntry.stat(follow_symlinks=False) на Windows
    открывают объект с FILE_FLAG_OPEN_REPARSE_POINT и не переходят по
    junction и символическим ссылкам. Прочие точки повторной обработки
    (дедупликация, облачные файлы) ведут к самому объекту, а не к
    другому пути: у их тега нет бита «суррогат имени».
    """
    attributes = getattr(stat_result, "st_file_attributes", 0)
    tag = getattr(stat_result, "st_reparse_tag", 0)
    return stat.S_ISLNK(stat_result.st_mode) or bool(
        attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT and tag & _NAME_SURROGATE_TAG
    )


def _read_security(
    path: str, kind: ObjectKind, *, reparse_point: bool
) -> ObjectSecurity:
    """Прочитать владельца и DACL объекта через win32security."""
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        path,
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION,
    )
    owner = descriptor.GetSecurityDescriptorOwner()
    acl = descriptor.GetSecurityDescriptorDacl()
    dacl = None
    if acl is not None:
        aces = []
        for index in range(acl.GetAceCount()):
            # ((тип, флаги), маска, ..., SID): у объектных ACE между
            # маской и SID ещё GUID-ы
            ace = acl.GetAce(index)
            (ace_type, flags), mask, sid = ace[0], ace[1], ace[-1]
            aces.append(
                Ace(win32security.ConvertSidToStringSid(sid), ace_type, flags, mask)
            )
        dacl = tuple(aces)
    return ObjectSecurity(
        path=path,
        kind=kind,
        owner=None if owner is None else win32security.ConvertSidToStringSid(owner),
        dacl=dacl,
        reparse_point=reparse_point,
    )


def _check_object(
    path: str, kind: ObjectKind, stat_result: os.stat_result
) -> list[Violation]:
    try:
        obj = _read_security(path, kind, reparse_point=_is_reparse_point(stat_result))
    except Exception as e:
        return [Violation(path, Problem.UNREADABLE, detail=str(e))]
    return find_violations([obj])


def _check_content(directory: str) -> Iterator[Violation]:
    """Проверить содержимое папки рекурсивно, не переходя по ссылкам."""
    try:
        with os.scandir(directory) as it:
            entries = list(it)
    except OSError as e:
        yield Violation(directory, Problem.UNREADABLE, detail=str(e))
        return
    for entry in entries:
        try:
            stat_result = entry.stat(follow_symlinks=False)
        except OSError as e:
            yield Violation(entry.path, Problem.UNREADABLE, detail=str(e))
            continue
        is_directory = stat.S_ISDIR(stat_result.st_mode)
        kind = ObjectKind.DIRECTORY if is_directory else ObjectKind.FILE
        yield from _check_object(entry.path, kind, stat_result)
        if is_directory and not _is_reparse_point(stat_result):
            yield from _check_content(entry.path)


def check_build_folder(folder: Path) -> list[Violation]:
    """Проверить права на папку сборки, её содержимое и предков.

    Путь не разрешается: junction в нём — само по себе нарушение.
    Ошибка чтения прав объекта — тоже нарушение, а не исключение, и
    проверка продолжается.

    Args:
        folder: Абсолютный путь к папке exe

    Returns:
        Нарушения: сначала предки от корня тома, затем папка сборки и
        её содержимое

    """
    violations: list[Violation] = []
    targets = [(parent, ObjectKind.ANCESTOR) for parent in reversed(folder.parents)]
    targets.append((folder, ObjectKind.DIRECTORY))
    for path, kind in targets:
        try:
            stat_result = os.lstat(path)
        except OSError as e:
            violations.append(Violation(str(path), Problem.UNREADABLE, detail=str(e)))
            continue
        violations.extend(_check_object(str(path), kind, stat_result))
        if path == folder:
            violations.extend(_check_content(str(folder)))
    return violations
