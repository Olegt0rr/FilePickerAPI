"""
Тесты пакета MSI: installer/FilePickerAPI.wxs как XML и перевод версии
сборки в ProductVersion (scripts/msi_version.py).

Сам пакет здесь не собирается: WiX есть только в CI на Windows, где
установку проверяет дымовая проверка.
"""

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.script_loader import load_script

ROOT = Path(__file__).resolve().parent.parent
WXS_FILE = ROOT / "installer" / "FilePickerAPI.wxs"
NS = {
    "wix": "http://wixtoolset.org/schemas/v4/wxs",
    "util": "http://wixtoolset.org/schemas/v4/wxs/util",
}
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MESSAGE_DLL = "FilePickerAPI.messages.dll"


@pytest.fixture
def msi_version():
    return load_script("msi_version")


@pytest.fixture(scope="module")
def wix():
    # С инструкциями препроцессора WiX (<?define?>, <?if?>). Файл
    # из репозитория, поэтому defusedxml не нужен
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_pis=True))  # noqa: S314
    return ET.parse(WXS_FILE, parser).getroot()  # noqa: S314


@pytest.fixture(scope="module")
def instructions(wix):
    """Инструкции препроцессора в корне Wix: (имя, остаток)."""
    result = []
    for element in wix:
        if element.tag is ET.ProcessingInstruction:
            name, _, rest = element.text.partition(" ")
            result.append((name, rest.strip()))
    return result


@pytest.fixture(scope="module")
def package(wix):
    return wix.find("wix:Package", NS)


def local_names(root):
    return {
        element.tag.rpartition("}")[2]
        for element in root.iter()
        if isinstance(element.tag, str)
    }


def component_of(package, element):
    for component in package.iterfind(".//wix:Component", NS):
        if element in list(component.iter()):
            return component
    pytest.fail(f"{element.tag} вне компонента")


class TestWxsPackage:
    def test_per_machine_with_cyrillic_name(self, package):
        assert package.get("Scope") == "perMachine"
        assert package.get("Name") == "File Picker API (филиал $(var.Branch))"
        assert package.get("Version") == "$(var.Version)"
        # Без кодовой страницы 1251 название на кириллице не собирается
        assert package.get("Codepage") == "1251"
        summary = package.find("wix:SummaryInformation", NS)
        assert summary.get("Codepage") == "1251"

    def test_upgrade_code_is_fixed(self, package, instructions):
        # Один UpgradeCode для всех филиалов, а не параметр сборки
        defines = [rest for name, rest in instructions if name == "define"]
        assert len(defines) == 1
        assert re.fullmatch(
            r'UpgradeCode\s*=\s*"[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}"',
            defines[0],
        )
        assert package.get("UpgradeCode") == "$(var.UpgradeCode)"

    def test_only_x64(self, instructions):
        names = [name for name, _ in instructions]
        checks = [
            index
            for index, (name, rest) in enumerate(instructions)
            if name == "if"
            and re.fullmatch(r"""\$\(sys\.BUILDARCH\)\s*~=\s*(["']?)x64\1""", rest)
        ]

        assert len(checks) == 1, "нет проверки архитектуры x64"
        start = checks[0]
        assert names[start + 1 : start + 4] == ["else", "error", "endif"]
        assert instructions[start + 2][1]

    def test_install_dir_is_not_public_property(self, package):
        standard = package.findall(".//wix:StandardDirectory", NS)
        directories = package.findall(".//wix:Directory", NS)

        assert [d.get("Id") for d in standard] == ["ProgramFiles64Folder"]
        assert len(directories) == 1
        assert directories[0] in list(standard[0])
        assert directories[0].get("Name") == "FilePickerAPI"
        # Публичные свойства MSI — только из заглавных букв, их можно
        # передать в командной строке msiexec
        install_dir = directories[0].get("Id")
        assert install_dir != install_dir.upper()
        assert "SetDirectory" not in local_names(package)

    def test_files_from_dist_without_bat_scripts(self, package):
        group = package.find("wix:ComponentGroup", NS)
        directory = package.find(".//wix:Directory", NS)
        files = package.findall(".//wix:Files", NS)

        assert group.get("Directory") == directory.get("Id")
        assert [f.get("Include") for f in files] == [r"$(var.Dist)\**"]
        excludes = {e.get("Files") for e in files[0].iterfind("wix:Exclude", NS)}
        assert excludes == {
            r"$(var.Dist)\**\*.bat",
            rf"$(var.Dist)\{MESSAGE_DLL}",
        }
        for element in package.iter():
            if element.tag != f"{{{NS['wix']}}}Exclude":
                for value in element.attrib.values():
                    assert not value.lower().endswith(".bat")

    def test_run_value_in_hklm(self, package):
        directory = package.find(".//wix:Directory", NS).get("Id")
        values = package.findall(".//wix:RegistryValue", NS)

        assert len(values) == 1
        value = values[0]
        assert value.get("Root") == "HKLM"
        assert value.get("Key") == RUN_KEY
        assert value.get("Name") == "FilePickerAPI"
        # Путь в кавычках, без аргументов
        assert value.get("Value") == f'"[{directory}]FilePickerAPI.exe"'
        assert value.get("Type") == "string"
        assert value.get("KeyPath") == "yes"
        # Значение Run удаляется вместе с продуктом
        assert component_of(package, value).get("Permanent") is None

    def test_message_file_and_event_source_are_permanent(self, package):
        sources = package.findall(".//util:EventSource", NS)
        files = package.findall(".//wix:File", NS)

        assert len(sources) == 1
        assert len(files) == 1
        source, message_file = sources[0], files[0]
        assert source.get("Log") == "Application"
        assert source.get("Name") == "FilePickerAPI"
        assert source.get("EventMessageFile") == f"[#{message_file.get('Id')}]"
        for kind in ("Errors", "Warnings", "Informationals"):
            assert source.get(f"Supports{kind}") == "yes"
        assert message_file.get("Source") == rf"$(var.Dist)\{MESSAGE_DLL}"
        assert component_of(package, source) is not component_of(package, message_file)
        for element in (source, message_file):
            assert element.get("KeyPath") == "yes"
            assert component_of(package, element).get("Permanent") == "yes"

    def test_major_upgrade_refuses_same_and_older_versions(self, package):
        upgrades = package.findall("wix:MajorUpgrade", NS)

        assert len(upgrades) == 1
        upgrade = upgrades[0]
        assert upgrade.get("Schedule") == "afterInstallInitialize"
        assert upgrade.get("DowngradeErrorMessage")
        assert upgrade.get("AllowSameVersionUpgrades") is None
        assert upgrade.get("AllowDowngrades") is None

    def test_same_version_is_detected_and_refused(self, package):
        upgrades = package.findall("wix:Upgrade", NS)
        launches = package.findall("wix:Launch", NS)

        assert len(upgrades) == 1
        assert upgrades[0].get("Id") == package.get("UpgradeCode")
        versions = upgrades[0].findall("wix:UpgradeVersion", NS)
        assert len(versions) == 1
        version = versions[0]
        assert version.get("Minimum") == version.get("Maximum") == "$(var.Version)"
        assert version.get("IncludeMinimum") == "yes"
        assert version.get("IncludeMaximum") == "yes"
        assert version.get("OnlyDetect") == "yes"
        assert len(launches) == 1
        assert launches[0].get("Condition") == (
            f"Installed OR NOT {version.get('Property')}"
        )
        assert launches[0].get("Message")

    def test_no_custom_actions(self, package):
        forbidden = {
            "CustomAction",
            "Custom",
            "SetProperty",
            "InstallExecuteSequence",
            "InstallUISequence",
            "AdminExecuteSequence",
            "AdvertiseExecuteSequence",
            "Binary",
        }

        assert not forbidden & local_names(package)

    def test_all_components_in_feature(self, package):
        group = package.find("wix:ComponentGroup", NS)
        refs = package.findall("wix:Feature/wix:ComponentGroupRef", NS)

        assert [r.get("Id") for r in refs] == [group.get("Id")]
        assert len(package.findall(".//wix:ComponentGroup", NS)) == 1


class TestProductVersion:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("2.3.0", "2.3.0"),
            ("0.0.1", "0.0.1"),
            ("255.255.65535", "255.255.65535"),
            ("02.3.010", "2.3.10"),
        ],
    )
    def test_release(self, msi_version, version, expected):
        assert msi_version.product_version(version) == expected

    @pytest.mark.parametrize(
        "version",
        [
            "2.3.1.dev2+gaa7062aaa",
            "2.3.1.dev2+gaa7062aaa.d20261102",
            "2.3.1.dev0",
            "2.3.1+d20261102",
        ],
    )
    def test_intermediate_build_gets_next_release(self, msi_version, version):
        assert msi_version.product_version(version) == "2.3.1"

    @pytest.mark.parametrize("version", ["256.0.0", "0.256.0", "0.0.65536"])
    def test_out_of_limits(self, msi_version, version):
        with pytest.raises(ValueError, match=r"255\.255\.65535"):
            msi_version.product_version(version)

    @pytest.mark.parametrize(
        "version",
        [
            "",
            "2.3",
            "0.1.dev1+gaa7062aaa",
            "2.3.1.4",
            "v2.3.1",
            "2.3.1rc1",
            "2.3.1.dev",
            "2.3.1+",
            "2.3.1\n",
            " 2.3.1",
            "２.3.1",
        ],
    )
    def test_invalid(self, msi_version, version):
        with pytest.raises(ValueError, match="не вида"):
            msi_version.product_version(version)


class TestNextVersion:
    @pytest.mark.parametrize(
        ("version", "expected"),
        [
            ("2.3.1", "2.3.2"),
            ("2.3.1.dev2+gaa7062aaa", "2.3.2"),
            ("0.0.0", "0.0.1"),
            ("255.255.65534", "255.255.65535"),
        ],
    )
    def test_next(self, msi_version, version, expected):
        assert msi_version.next_version(version) == expected

    def test_out_of_limits(self, msi_version):
        with pytest.raises(ValueError, match=r"255\.255\.65535"):
            msi_version.next_version("2.3.65535")

    def test_invalid(self, msi_version):
        with pytest.raises(ValueError, match="не вида"):
            msi_version.next_version("2.3")


class TestProductVersionCli:
    def test_prints_product_version(self, msi_version, capsys):
        msi_version.main(["2.3.1.dev2+gaa7062aaa"])

        assert capsys.readouterr().out == "2.3.1\n"

    def test_error_on_out_of_limits(self, msi_version):
        with pytest.raises(ValueError, match="ограничения"):
            msi_version.main(["256.0.0"])

    def test_prints_next_version(self, msi_version, capsys):
        msi_version.main(["next", "2.3.1.dev2+gaa7062aaa"])

        assert capsys.readouterr().out == "2.3.2\n"

    @pytest.mark.parametrize(
        "argv", [[], ["2.3.0", "2.4.0"], ["next", "2.3.0", "2.4.0"]]
    )
    def test_usage_on_bad_arguments(self, msi_version, capsys, argv):
        with pytest.raises(SystemExit) as exc_info:
            msi_version.main(argv)

        assert exc_info.value.code == 2
        assert "Использование" in capsys.readouterr().err
