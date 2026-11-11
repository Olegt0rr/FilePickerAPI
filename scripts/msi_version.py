"""Перевод версии сборки в ProductVersion пакета MSI.

Использование:
    python scripts/msi_version.py <версия>
        Вывести ProductVersion для версии app._version.version
        (2.3.1.dev2+gaa7062aaa) или имени тега без «v» (2.3.1).
    python scripts/msi_version.py next <версия>
        Вывести ProductVersion на единицу больше по последнему числу
        (2.3.1 -> 2.3.2): версия второго MSI дымовой проверки
        обновления в build-exe.yml.
"""

import re
import sys

# Релиз X.Y.Z и промежуточная сборка setuptools-scm между тегами:
# X.Y.Z.devN с локальной частью +g<хеш> и пометкой изменённой
# рабочей копии. Номер X.Y.Z у неё — уже версия следующего релиза
# (только ASCII-цифры: \d принял бы и другие цифры Юникода)
VERSION_PATTERN = re.compile(
    r"([0-9]+)\.([0-9]+)\.([0-9]+)"
    r"(?:\.dev[0-9]+)?(?:\+[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?"
)
# Ограничения Windows Installer на ProductVersion
LIMITS = (255, 255, 65535)


def product_version(version: str) -> str:
    """Вернуть ProductVersion из первых трёх чисел версии сборки."""
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        msg = f"Версия {version!r} не вида X.Y.Z или X.Y.Z.devN+локальная часть"
        raise ValueError(msg)

    numbers = [int(part) for part in match.groups()]
    if any(number > limit for number, limit in zip(numbers, LIMITS, strict=True)):
        msg = f"Версия {version!r} выходит за ограничения MSI 255.255.65535"
        raise ValueError(msg)

    return ".".join(map(str, numbers))


def next_version(version: str) -> str:
    """Вернуть ProductVersion на единицу больше по последнему числу."""
    *head, last = product_version(version).split(".")
    return product_version(".".join([*head, str(int(last) + 1)]))


def main(argv: list[str]) -> None:
    """Точка входа командной строки."""
    if len(argv) == 1:
        sys.stdout.write(product_version(argv[0]) + "\n")
    elif len(argv) == 2 and argv[0] == "next":  # noqa: PLR2004
        sys.stdout.write(next_version(argv[1]) + "\n")
    else:
        sys.stderr.write(__doc__)
        sys.exit(2)


# Тесты вызывают main() напрямую
if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
