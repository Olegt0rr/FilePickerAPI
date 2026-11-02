"""Тесты проверки прав на папку сборки (app/permissions.py).

Сравнение прав проверяется на наборах ACE, чтение — с поддельным
win32security на любой ОС, а на Windows ещё и на настоящих папках с
правами, выданными через icacls.
"""

import os
import subprocess
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

from app import permissions
from app.permissions import (
    Ace,
    ObjectKind,
    ObjectSecurity,
    Problem,
    Violation,
    find_violations,
)
from tests.platforms import windows_only

SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
USERS = "S-1-5-32-545"
AUTHENTICATED_USERS = "S-1-5-11"
CREATOR_OWNER = "S-1-3-0"
ALICE = "S-1-5-21-1111-2222-3333-1001"

ALLOWED = 0x0
DENIED = 0x1
ALLOWED_OBJECT = 0x5
OBJECT_INHERIT = 0x1
CONTAINER_INHERIT = 0x2
INHERIT_ONLY = 0x8
INHERITED = 0x10

FULL_CONTROL = 0x1F01FF
MODIFY = 0x1301BF
READ_AND_EXECUTE = 0x1200A9
# FILE_APPEND_DATA у директории — право создавать папки
CREATE_FOLDERS = 0x4

# Обычные права в Program Files: доверенные — полный доступ, Users —
# чтение
TRUSTED_ACES = (
    Ace(SYSTEM, ALLOWED, 0, FULL_CONTROL),
    Ace(ADMINISTRATORS, ALLOWED, 0, FULL_CONTROL),
    Ace(TRUSTED_INSTALLER, ALLOWED, 0, FULL_CONTROL),
    Ace(USERS, ALLOWED, 0, READ_AND_EXECUTE),
)


def secured(path, kind, *aces, owner=TRUSTED_INSTALLER, dacl=()):
    """Объект с доверенными ACE и дополнительными aces."""
    if dacl is not None:
        dacl = TRUSTED_ACES + aces
    return ObjectSecurity(path, kind, owner, dacl)


class TestFindViolations:
    def test_program_files_install(self):
        """Проверить: обычная установка в Program Files — без нарушений.

        На C:\\ у Authenticated Users право создавать папки и
        «изменение» только для наследования, у CREATOR OWNER —
        полный доступ только для наследования.
        """
        objects = [
            secured(
                "C:\\",
                ObjectKind.ANCESTOR,
                Ace(AUTHENTICATED_USERS, ALLOWED, 0, CREATE_FOLDERS),
                Ace(
                    AUTHENTICATED_USERS,
                    ALLOWED,
                    OBJECT_INHERIT | CONTAINER_INHERIT | INHERIT_ONLY,
                    MODIFY,
                ),
                Ace(
                    CREATOR_OWNER,
                    ALLOWED,
                    OBJECT_INHERIT | CONTAINER_INHERIT | INHERIT_ONLY,
                    0x10000000,
                ),
            ),
            secured("C:\\Program Files", ObjectKind.ANCESTOR),
            secured(
                "C:\\Program Files\\FilePickerAPI",
                ObjectKind.DIRECTORY,
                owner=ADMINISTRATORS,
            ),
            secured(
                "C:\\Program Files\\FilePickerAPI\\FilePickerAPI.exe",
                ObjectKind.FILE,
                owner=SYSTEM,
            ),
        ]
        assert find_violations(objects) == []

    @pytest.mark.parametrize("kind", list(ObjectKind))
    def test_null_dacl(self, kind):
        """Проверить: NULL DACL при доверенном владельце — нарушение."""
        obj = secured("C:\\x", kind, owner=ADMINISTRATORS, dacl=None)
        assert find_violations([obj]) == [Violation("C:\\x", Problem.NULL_DACL)]

    @pytest.mark.parametrize("kind", list(ObjectKind))
    def test_empty_dacl(self, kind):
        """Проверить: пустая DACL (доступа нет ни у кого) — норма."""
        obj = ObjectSecurity("C:\\x", kind, ADMINISTRATORS, ())
        assert find_violations([obj]) == []

    def test_delete_child_on_build_folder(self):
        """Проверить: FILE_DELETE_CHILD на папке сборки — нарушение.

        Право удалять дочерние объекты позволяет подменить exe, даже
        если на самих файлах права DELETE нет.
        """
        folder = secured(
            "C:\\Program Files\\FilePickerAPI",
            ObjectKind.DIRECTORY,
            Ace(USERS, ALLOWED, 0, 0x40),
        )
        assert find_violations([folder]) == [
            Violation(folder.path, Problem.RIGHTS, USERS, ("FILE_DELETE_CHILD",))
        ]

    def test_delete_child_on_file_ignored(self):
        """Проверить: у файла бит FILE_DELETE_CHILD ничего не значит."""
        obj = secured("C:\\a.dll", ObjectKind.FILE, Ace(USERS, ALLOWED, 0, 0x40))
        assert find_violations([obj]) == []

    def test_write_on_file(self):
        obj = secured(
            "C:\\a.dll", ObjectKind.FILE, Ace(USERS, ALLOWED, INHERITED, MODIFY)
        )
        assert find_violations([obj]) == [
            Violation(
                "C:\\a.dll",
                Problem.RIGHTS,
                USERS,
                (
                    "FILE_WRITE_DATA",
                    "FILE_APPEND_DATA",
                    "FILE_WRITE_EA",
                    "FILE_WRITE_ATTRIBUTES",
                    "DELETE",
                ),
            )
        ]

    def test_write_dac_on_ancestor(self):
        obj = secured(
            "C:\\Program Files",
            ObjectKind.ANCESTOR,
            Ace(USERS, ALLOWED, 0, READ_AND_EXECUTE | 0x40000),
        )
        assert find_violations([obj]) == [
            Violation(obj.path, Problem.RIGHTS, USERS, ("WRITE_DAC",))
        ]

    def test_untrusted_owner_of_ancestor(self):
        obj = secured("C:\\Program Files", ObjectKind.ANCESTOR, owner=ALICE)
        assert find_violations([obj]) == [Violation(obj.path, Problem.OWNER, ALICE)]

    def test_no_owner(self):
        obj = secured("C:\\Program Files", ObjectKind.ANCESTOR, owner=None)
        assert find_violations([obj]) == [Violation(obj.path, Problem.OWNER, None)]

    def test_deletable_ancestor_in_user_profile(self):
        """Проверить: сборка в C:\\Users\\alice\\container — нарушение.

        Папку пользователя alice может удалить или переименовать и
        подложить свою.
        """
        obj = secured(
            "C:\\Users\\alice\\container",
            ObjectKind.ANCESTOR,
            Ace(ALICE, ALLOWED, INHERITED, FULL_CONTROL),
            owner=ALICE,
        )
        assert find_violations([obj]) == [
            Violation(obj.path, Problem.OWNER, ALICE),
            Violation(
                obj.path,
                Problem.RIGHTS,
                ALICE,
                ("FILE_DELETE_CHILD", "DELETE", "WRITE_DAC", "WRITE_OWNER"),
            ),
        ]

    def test_create_folders_on_ancestor(self):
        """Проверить: право создавать папки у предка — не нарушение."""
        obj = secured(
            "C:\\", ObjectKind.ANCESTOR, Ace(AUTHENTICATED_USERS, ALLOWED, 0, 0x6)
        )
        assert find_violations([obj]) == []

    def test_create_folders_in_build_folder(self):
        """Проверить: создать папку в папке сборки — нарушение."""
        obj = secured(
            "C:\\Program Files\\FilePickerAPI",
            ObjectKind.DIRECTORY,
            Ace(USERS, ALLOWED, 0, CREATE_FOLDERS),
        )
        assert find_violations([obj])[0].rights == ("FILE_APPEND_DATA",)

    @pytest.mark.parametrize("kind", list(ObjectKind))
    def test_inherit_only_ace_skipped(self, kind):
        """Проверить: ACE «только для наследования» не действует."""
        obj = secured(
            "C:\\x",
            kind,
            Ace(USERS, ALLOWED, OBJECT_INHERIT | INHERIT_ONLY, FULL_CONTROL),
        )
        assert find_violations([obj]) == []

    def test_deny_ace_ignored(self):
        """Проверить: запрещающий ACE не учитывается и не нарушение."""
        obj = secured("C:\\a.dll", ObjectKind.FILE, Ace(USERS, DENIED, 0, FULL_CONTROL))
        assert find_violations([obj]) == []

    def test_deny_ace_does_not_cancel_allow(self):
        """Проверить: запрет не отменяет разрешение."""
        obj = secured(
            "C:\\a.dll",
            ObjectKind.FILE,
            Ace(USERS, DENIED, 0, 0x10000),
            Ace(USERS, ALLOWED, 0, 0x10000),
        )
        assert find_violations([obj])[0].rights == ("DELETE",)

    def test_object_ace_counted(self):
        obj = secured(
            "C:\\a.dll", ObjectKind.FILE, Ace(USERS, ALLOWED_OBJECT, 0, 0x80000)
        )
        assert find_violations([obj])[0].rights == ("WRITE_OWNER",)

    def test_generic_rights(self):
        obj = secured(
            "C:\\a.dll",
            ObjectKind.FILE,
            Ace(USERS, ALLOWED, 0, 0x40000000),
            Ace(ALICE, ALLOWED, 0, 0x10000000),
        )
        assert [(v.account, v.rights) for v in find_violations([obj])] == [
            (USERS, ("GENERIC_WRITE",)),
            (ALICE, ("GENERIC_ALL",)),
        ]

    def test_generic_write_on_ancestor_allowed(self):
        """Проверить: GENERIC_WRITE у предка не даёт удалить папку."""
        obj = secured(
            "C:\\Program Files", ObjectKind.ANCESTOR, Ace(USERS, ALLOWED, 0, 0x40000000)
        )
        assert find_violations([obj]) == []

    def test_aces_of_one_account_combined(self):
        obj = secured(
            "C:\\a.dll",
            ObjectKind.FILE,
            Ace(USERS, ALLOWED, 0, 0x2),
            Ace(ALICE, ALLOWED, 0, 0x10000),
            Ace(USERS, ALLOWED, INHERITED, 0x100),
        )
        assert find_violations([obj]) == [
            Violation(
                "C:\\a.dll",
                Problem.RIGHTS,
                USERS,
                ("FILE_WRITE_DATA", "FILE_WRITE_ATTRIBUTES"),
            ),
            Violation("C:\\a.dll", Problem.RIGHTS, ALICE, ("DELETE",)),
        ]

    @pytest.mark.parametrize("trusted", [SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER])
    def test_trusted_accounts(self, trusted):
        obj = ObjectSecurity(
            "C:\\a.dll",
            ObjectKind.FILE,
            trusted,
            (Ace(trusted, ALLOWED, 0, FULL_CONTROL | 0x10000000),),
        )
        assert find_violations([obj]) == []

    def test_custom_trusted(self):
        obj = secured("C:\\a.dll", ObjectKind.FILE, owner=ALICE)
        assert find_violations([obj], trusted=frozenset({ALICE})) == [
            Violation("C:\\a.dll", Problem.RIGHTS, SYSTEM, permissions_rights_all()),
            Violation(
                "C:\\a.dll", Problem.RIGHTS, ADMINISTRATORS, permissions_rights_all()
            ),
            Violation(
                "C:\\a.dll",
                Problem.RIGHTS,
                TRUSTED_INSTALLER,
                permissions_rights_all(),
            ),
        ]

    def test_reparse_point(self):
        obj = secured("C:\\link", ObjectKind.ANCESTOR)._replace(reparse_point=True)
        assert find_violations([obj]) == [Violation("C:\\link", Problem.REPARSE_POINT)]

    def test_order_of_objects(self):
        objects = [
            secured("C:\\", ObjectKind.ANCESTOR, owner=ALICE),
            secured("C:\\b", ObjectKind.DIRECTORY, dacl=None),
        ]
        assert [v.path for v in find_violations(objects)] == ["C:\\", "C:\\b"]


def permissions_rights_all():
    """Опасные права файла из полного доступа."""
    return (
        "FILE_WRITE_DATA",
        "FILE_APPEND_DATA",
        "FILE_WRITE_EA",
        "FILE_WRITE_ATTRIBUTES",
        "DELETE",
        "WRITE_DAC",
        "WRITE_OWNER",
    )


class TestFormatViolations:
    def names(self, sid):
        return {USERS: "BUILTIN\\Users", ALICE: "PC\\alice"}.get(sid, sid)

    def test_text(self):
        violations = [
            Violation("C:\\Users\\alice", Problem.OWNER, ALICE),
            Violation("C:\\x", Problem.OWNER, None),
            Violation("C:\\b", Problem.RIGHTS, USERS, ("FILE_WRITE_DATA", "DELETE")),
            Violation("C:\\b\\a.dll", Problem.NULL_DACL),
            Violation("C:\\link", Problem.REPARSE_POINT),
            Violation("C:\\c", Problem.UNREADABLE, detail="Отказано в доступе"),
        ]
        text = permissions.format_violations(violations, self.names)
        lines = text.splitlines()
        assert "SYSTEM, Administrators и TrustedInstaller" in lines[0]
        assert "продолжает работу" in lines[0]
        assert lines[1:] == [
            "- C:\\Users\\alice: владелец не из доверенных (PC\\alice)",
            "- C:\\x: владелец не из доверенных (нет)",
            "- C:\\b: опасные права у BUILTIN\\Users: FILE_WRITE_DATA, DELETE",
            "- C:\\b\\a.dll: полный доступ у всех (нет DACL)",
            "- C:\\link: точка повторной обработки (junction или символическая ссылка)",
            "- C:\\c: права не удалось проверить: Отказано в доступе",
        ]

    def test_truncated(self):
        """Проверить: длинный перечень обрезается с числом остальных."""
        violations = [
            Violation(f"C:\\b\\{i}.dll", Problem.NULL_DACL) for i in range(53)
        ]
        lines = permissions.format_violations(violations, self.names).splitlines()
        assert len(lines) == 1 + 50 + 1
        assert lines[-1] == "…и ещё нарушений: 3"

    def test_event_id(self):
        assert permissions.BUILD_FOLDER_PERMISSIONS_EVENT_ID == 1003


class FakeDescriptor:
    def __init__(self, owner, aces):
        self.owner = owner
        self.aces = aces

    def GetSecurityDescriptorOwner(self):  # noqa: N802 - API pywin32
        return self.owner

    def GetSecurityDescriptorDacl(self):  # noqa: N802 - API pywin32
        if self.aces is None:
            return None
        acl = mock.Mock()
        acl.GetAceCount.return_value = len(self.aces)
        acl.GetAce.side_effect = self.aces.__getitem__
        return acl


class FakeError(Exception):
    """pywintypes.error."""


@pytest.fixture
def win32security():
    """Подставить поддельные win32security и pywintypes.

    SID-ы — строки. descriptors: путь -> (владелец, ACE в виде
    pywin32 или None для NULL DACL) или исключение; по умолчанию —
    доверенный владелец и TRUSTED_ACES.
    """
    module = types.ModuleType("win32security")
    module.SE_FILE_OBJECT = 1
    module.OWNER_SECURITY_INFORMATION = 1
    module.DACL_SECURITY_INFORMATION = 4
    module.descriptors = {}
    module.accounts = {USERS: ("Users", "BUILTIN"), SYSTEM: ("SYSTEM", "")}
    default_aces = [((a.ace_type, a.flags), a.mask, a.sid) for a in TRUSTED_ACES]

    def get_named_security_info(path, object_type, information):
        assert object_type == 1
        assert information == 5
        value = module.descriptors.get(path, (SYSTEM, default_aces))
        if isinstance(value, Exception):
            raise value
        return FakeDescriptor(*value)

    def lookup_account_sid(system, sid):
        assert system is None
        if sid not in module.accounts:
            raise FakeError(1332, "LookupAccountSid", "No mapping")
        return (*module.accounts[sid], 1)

    module.GetNamedSecurityInfo = get_named_security_info
    module.ConvertSidToStringSid = str
    module.ConvertStringSidToSid = str
    module.LookupAccountSid = lookup_account_sid
    pywintypes = types.ModuleType("pywintypes")
    pywintypes.error = FakeError
    with mock.patch.dict(
        sys.modules, {"win32security": module, "pywintypes": pywintypes}
    ):
        yield module


def make_link(link, target):
    """Создать junction на Windows или символическую ссылку на Linux.

    Junction создаётся без прав администратора, символическая ссылка
    на Windows — нет.
    """
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603 - аргументы задаются тестом
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def build_folder(tmp_path):
    """Папка сборки с файлом и подпапкой с файлом."""
    folder = tmp_path / "FilePickerAPI"
    (folder / "lib").mkdir(parents=True)
    (folder / "FilePickerAPI.exe").write_bytes(b"MZ")
    (folder / "lib" / "a.dll").write_bytes(b"MZ")
    return folder


@pytest.mark.usefixtures("win32security")
class TestCheckBuildFolder:
    def test_secure(self, build_folder):
        assert permissions.check_build_folder(build_folder) == []

    def test_objects_and_kinds(self, win32security, build_folder):
        """Проверить: предки до корня, папка и всё её содержимое."""
        seen = []
        real = win32security.GetNamedSecurityInfo

        def record(path, *args):
            seen.append(path)
            return real(path, *args)

        win32security.GetNamedSecurityInfo = record
        kinds = []
        real_find = permissions.find_violations

        def record_kinds(objects):
            kinds.extend((obj.path, obj.kind) for obj in objects)
            return real_find(objects)

        with mock.patch.object(permissions, "find_violations", record_kinds):
            permissions.check_build_folder(build_folder)
        ancestors = [str(p) for p in reversed(build_folder.parents)]
        assert seen[: len(ancestors)] == ancestors
        assert seen[len(ancestors)] == str(build_folder)
        assert sorted(seen[len(ancestors) + 1 :]) == sorted(
            [
                str(build_folder / "FilePickerAPI.exe"),
                str(build_folder / "lib"),
                str(build_folder / "lib" / "a.dll"),
            ]
        )
        kind_of = dict(kinds)
        assert kind_of[ancestors[0]] is ObjectKind.ANCESTOR
        assert kind_of[str(build_folder)] is ObjectKind.DIRECTORY
        assert kind_of[str(build_folder / "lib")] is ObjectKind.DIRECTORY
        assert kind_of[str(build_folder / "lib" / "a.dll")] is ObjectKind.FILE

    def test_reads_owner_and_aces(self, win32security, build_folder):
        exe = str(build_folder / "FilePickerAPI.exe")
        lib = str(build_folder / "lib")
        parent = str(build_folder.parent)
        win32security.descriptors[exe] = (
            ADMINISTRATORS,
            [
                ((ALLOWED, INHERITED), MODIFY, USERS),
                # Объектный ACE: между маской и SID — GUID-ы
                ((ALLOWED_OBJECT, 0), 0x10000, "guid", None, ALICE),
                ((ALLOWED, INHERIT_ONLY | OBJECT_INHERIT), FULL_CONTROL, ALICE),
            ],
        )
        win32security.descriptors[lib] = (None, None)
        win32security.descriptors[parent] = (ALICE, [])
        violations = permissions.check_build_folder(build_folder)
        # Порядок файлов внутри папки зависит от ФС
        assert violations[0] == Violation(parent, Problem.OWNER, ALICE)
        assert len(violations) == 5
        assert set(violations[1:]) == {
            Violation(
                exe,
                Problem.RIGHTS,
                USERS,
                (
                    "FILE_WRITE_DATA",
                    "FILE_APPEND_DATA",
                    "FILE_WRITE_EA",
                    "FILE_WRITE_ATTRIBUTES",
                    "DELETE",
                ),
            ),
            Violation(exe, Problem.RIGHTS, ALICE, ("DELETE",)),
            Violation(lib, Problem.OWNER, None),
            Violation(lib, Problem.NULL_DACL),
        }

    def test_link_in_path(self, tmp_path, build_folder):
        """Проверить: junction (ссылка) в пути к папке — нарушение."""
        link = tmp_path / "link"
        make_link(link, build_folder.parent)
        violations = permissions.check_build_folder(link / build_folder.name)
        assert violations == [Violation(str(link), Problem.REPARSE_POINT)]

    def test_link_inside_not_followed(self, tmp_path, build_folder):
        """Проверить: ссылка в папке — нарушение, внутрь не заходим."""
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x")
        link = build_folder / "link"
        make_link(link, outside)
        violations = permissions.check_build_folder(build_folder)
        assert violations == [Violation(str(link), Problem.REPARSE_POINT)]

    def test_reparse_attribute(self):
        """Проверить: признак берётся из атрибутов файла Windows."""
        is_reparse_point = permissions._is_reparse_point  # noqa: SLF001
        assert not is_reparse_point(types.SimpleNamespace(st_mode=0o040755))
        junction = types.SimpleNamespace(
            st_mode=0o040755, st_file_attributes=0x410, st_reparse_tag=0xA0000003
        )
        assert is_reparse_point(junction)

    def test_reparse_point_without_name_surrogate(self):
        """Проверить: дедупликация или облачный файл — не ссылка.

        Такие точки повторной обработки ведут к самому файлу.
        """
        is_reparse_point = permissions._is_reparse_point  # noqa: SLF001
        deduplicated = types.SimpleNamespace(
            st_mode=0o100644, st_file_attributes=0x420, st_reparse_tag=0x80000013
        )
        assert not is_reparse_point(deduplicated)

    def test_unreadable_security(self, win32security, build_folder):
        exe = str(build_folder / "FilePickerAPI.exe")
        win32security.descriptors[exe] = FakeError(5, "GetNamedSecurityInfo", "Нет")
        violations = permissions.check_build_folder(build_folder)
        assert violations == [
            Violation(
                exe,
                Problem.UNREADABLE,
                detail="(5, 'GetNamedSecurityInfo', 'Нет')",
            )
        ]

    def test_unreadable_ancestor(self, build_folder):
        """Проверить: предок не открылся — нарушение, проверка идёт."""
        parent = build_folder.parent
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if Path(path) == parent:
                raise PermissionError(13, "Отказано в доступе")
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(os, "lstat", lstat):
            violations = permissions.check_build_folder(build_folder)
        assert violations == [
            Violation(
                str(parent),
                Problem.UNREADABLE,
                detail="[Errno 13] Отказано в доступе",
            )
        ]

    def test_unreadable_build_folder(self, build_folder):
        """Проверить: папка сборки не открылась — одно нарушение."""
        real_lstat = os.lstat

        def lstat(path, *args, **kwargs):
            if Path(path) == build_folder:
                raise PermissionError(13, "Отказано в доступе")
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(os, "lstat", lstat):
            violations = permissions.check_build_folder(build_folder)
        assert violations == [
            Violation(
                str(build_folder),
                Problem.UNREADABLE,
                detail="[Errno 13] Отказано в доступе",
            )
        ]

    def test_unlistable_folder(self, build_folder):
        lib = str(build_folder / "lib")
        real_scandir = os.scandir

        def scandir(path):
            if path == lib:
                raise PermissionError(13, "Отказано в доступе")
            return real_scandir(path)

        with mock.patch.object(os, "scandir", scandir):
            violations = permissions.check_build_folder(build_folder)
        assert violations == [
            Violation(lib, Problem.UNREADABLE, detail="[Errno 13] Отказано в доступе")
        ]

    def test_entry_vanished(self, build_folder):
        """Проверить: файл пропал во время обхода — нарушение чтения."""
        real_scandir = os.scandir
        exe = str(build_folder / "FilePickerAPI.exe")

        class Entry:
            def __init__(self, entry):
                self.entry = entry
                self.path = entry.path

            def stat(self, *, follow_symlinks):
                if self.path == exe:
                    raise FileNotFoundError(2, "Нет файла")
                return self.entry.stat(follow_symlinks=follow_symlinks)

        class Scandir:
            def __init__(self, path):
                self.it = real_scandir(path)

            def __enter__(self):
                return [Entry(entry) for entry in self.it]

            def __exit__(self, *args):
                self.it.close()

        with mock.patch.object(os, "scandir", Scandir):
            violations = permissions.check_build_folder(build_folder)
        assert violations == [
            Violation(exe, Problem.UNREADABLE, detail="[Errno 2] Нет файла")
        ]


@pytest.mark.usefixtures("win32security")
class TestLookupAccountName:
    def test_with_domain(self):
        assert permissions._lookup_account_name(USERS) == "BUILTIN\\Users"  # noqa: SLF001

    def test_without_domain(self):
        assert permissions._lookup_account_name(SYSTEM) == "SYSTEM"  # noqa: SLF001

    def test_unknown_sid(self):
        assert permissions._lookup_account_name(ALICE) == ALICE  # noqa: SLF001

    def test_default_for_format(self):
        violations = [Violation("C:\\b", Problem.RIGHTS, USERS, ("DELETE",))]
        assert "BUILTIN\\Users: DELETE" in permissions.format_violations(violations)

    def test_account_looked_up_once(self):
        """Проверить: имя учётной записи запрашивается один раз."""
        names = mock.Mock(return_value="BUILTIN\\Users")
        violations = [
            Violation(f"C:\\b\\{i}.dll", Problem.RIGHTS, USERS, ("DELETE",))
            for i in range(3)
        ]
        permissions.format_violations(violations, names)
        names.assert_called_once_with(USERS)


def icacls(path, *args):
    subprocess.run(  # noqa: S603 - аргументы задаются тестом
        ["icacls", str(path), *args],  # noqa: S607
        check=True,
        capture_output=True,
    )


def users_violations(violations, path):
    """Нарушения группы Users на объекте path."""
    return [
        v
        for v in violations
        if v.path == str(path) and v.problem is Problem.RIGHTS and v.account == USERS
    ]


@windows_only
class TestWindowsFolders:
    """Настоящие папки и права, выданные через icacls.

    Временная папка лежит в профиле пользователя, и у её предков
    владелец — пользователь: такие нарушения ожидаемы, поэтому тесты
    смотрят только на права группы Users на нужном объекте.
    """

    @pytest.fixture
    def tree(self, tmp_path):
        middle = tmp_path / "middle"
        folder = middle / "FilePickerAPI"
        folder.mkdir(parents=True)
        exe = folder / "FilePickerAPI.exe"
        exe.write_bytes(b"MZ")
        return types.SimpleNamespace(middle=middle, folder=folder, exe=exe)

    def test_users_write_on_file(self, tree):
        icacls(tree.exe, "/grant", f"*{USERS}:(W)")
        violations = permissions.check_build_folder(tree.folder)
        (violation,) = users_violations(violations, tree.exe)
        assert "FILE_WRITE_DATA" in violation.rights

    def test_delete_child_on_build_folder(self, tree):
        icacls(tree.folder, "/grant", f"*{USERS}:(DC)")
        violations = permissions.check_build_folder(tree.folder)
        (violation,) = users_violations(violations, tree.folder)
        assert violation.rights == ("FILE_DELETE_CHILD",)
        assert users_violations(violations, tree.exe) == []

    def test_deletable_middle_folder(self, tree):
        icacls(tree.middle, "/grant", f"*{USERS}:(D)")
        violations = permissions.check_build_folder(tree.folder)
        (violation,) = users_violations(violations, tree.middle)
        assert violation.rights == ("DELETE",)

    def test_read_and_inherit_only(self, tree):
        """Проверить: чтение и ACE только для наследования — норма.

        Унаследованные файлом права Users при этом — нарушение.
        """
        icacls(tree.folder, "/grant", f"*{USERS}:(RX)")
        icacls(tree.folder, "/grant", f"*{USERS}:(OI)(IO)(M)")
        violations = permissions.check_build_folder(tree.folder)
        assert users_violations(violations, tree.folder) == []
        (violation,) = users_violations(violations, tree.exe)
        assert "DELETE" in violation.rights

    def test_owner_and_account_name(self, tree):
        """Проверить: владелец — текущий пользователь, имя читается.

        Владельцем новых папок у администратора с повышенными правами
        (раннер CI) становится группа Administrators, поэтому владелец
        назначается явно.
        """
        user = subprocess.run(
            ["whoami"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        icacls(tree.folder, "/setowner", user)
        violations = permissions.check_build_folder(tree.folder)
        owners = [
            v
            for v in violations
            if v.path == str(tree.folder) and v.problem is Problem.OWNER
        ]
        assert len(owners) == 1
        text = permissions.format_violations(owners)
        assert os.environ["USERNAME"].lower() in text.lower()

    def test_junction_in_path(self, tree, tmp_path):
        link = tmp_path / "link"
        make_link(link, tree.middle)
        violations = permissions.check_build_folder(link / tree.folder.name)
        reparse = [v for v in violations if v.problem is Problem.REPARSE_POINT]
        assert reparse == [Violation(str(link), Problem.REPARSE_POINT)]
