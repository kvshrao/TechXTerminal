"""The texts the About page shows: this program's licence and release notes, and the licences of what it is built on."""

import re
import sys
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ["PyQt6", "paramiko", "pyte", "keyring", "pyserial", "pywinpty"]  # what requirements.txt lists to run the application
LICENCE_FILES = re.compile(r"(^|/)(licen[sc]e|copying|notice|authors)[^/]*$", re.IGNORECASE)
NOTICES_FILE = "THIRD_PARTY_NOTICES.txt"


def find(name: str) -> Path | None:
    """A file shipped with the program: in the folder a packaged build unpacks to, or in the source folder."""
    for folder in (getattr(sys, "_MEIPASS", None), ROOT):
        if folder and (Path(folder) / name).is_file():
            return Path(folder) / name
    return None


def read(name: str) -> str | None:
    """The text of a file shipped with the program; None if it is not there."""
    path = find(name)
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def licence() -> str | None:
    return read("LICENSE")


def release_notes() -> str | None:
    return read("CHANGELOG.md")


def third_party_notices() -> str:
    """The licences of the libraries and fonts the program is built on.

    A packaged build carries the file that was made when it was built. Run from source, the text is made from
    what is installed, so it follows the versions that are really in use.
    """
    if getattr(sys, "frozen", False):
        shipped = read(NOTICES_FILE)
        if shipped:
            return shipped
    return _gather()


def _needed(roots: list[str]) -> list[metadata.Distribution]:
    """The installed distributions the application runs on: its own requirements and theirs."""
    found: dict[str, metadata.Distribution] = {}
    queue = list(roots)
    while queue:
        name = queue.pop()
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue  # a dependency that is not for this platform
        key = distribution.metadata["Name"].lower().replace("_", "-")
        if key in found:
            continue
        found[key] = distribution
        for requirement in distribution.requires or []:
            if "extra ==" in requirement:
                continue
            queue.append(re.split(r"[ ;<>=!~\[(]", requirement, maxsplit=1)[0])
    return sorted(found.values(), key=lambda item: item.metadata["Name"].lower())


def _licence_name(distribution: metadata.Distribution) -> str:
    info = distribution.metadata
    expression = info.get("License-Expression") or ""
    if expression:
        return expression
    classifiers = [line.split(" :: ")[-1] for line in info.get_all("Classifier") or [] if line.startswith("License ::")]
    declared = (info.get("License") or "").strip()
    return ", ".join(classifiers) or (declared if len(declared) < 80 else "see the licence text below")


def _licence_texts(distribution: metadata.Distribution) -> list[tuple[str, str]]:
    texts = []
    for entry in distribution.files or []:
        if LICENCE_FILES.search(str(entry).replace("\\", "/")):
            try:
                texts.append((Path(str(entry)).name, entry.locate().read_bytes().decode("utf-8", errors="replace")))
            except OSError:
                pass
    return texts


def _gather() -> str:
    parts = [
        "Tech X Terminal is free software under the GNU General Public License, version 3 (see LICENSE).\n"
        "It is built on the software below, each part under its own licence, whose text follows.\n"
    ]
    for distribution in _needed(RUNTIME):
        info = distribution.metadata
        parts.append("=" * 78)
        parts.append(f"{info['Name']} {info['Version']}")
        parts.append(f"Licence: {_licence_name(distribution)}")
        home = info.get("Home-page") or next(
            (value.split(",", 1)[-1].strip() for value in info.get_all("Project-URL") or [] if value.lower().startswith(("homepage", "source"))), ""
        )
        if home:
            parts.append(f"From:    {home}")
        parts.append("")
        for name, text in _licence_texts(distribution):
            parts.append(f"--- {name} ---")
            parts.append(text.strip())
            parts.append("")
    fonts = Path(__file__).parent / "fonts" / "UFL.txt"
    parts.append("=" * 78)
    parts.append("The Ubuntu fonts")
    parts.append("Licence: Ubuntu Font Licence 1.0")
    parts.append("")
    try:
        parts.append(fonts.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        parts.append("(the text of this licence could not be read)")
    parts.append("")
    parts.append("=" * 78)
    parts.append(f"Python {sys.version.split()[0]}, which the application runs on")
    parts.append("Licence: Python Software Foundation License (https://docs.python.org/3/license.html)")
    return "\n".join(parts) + "\n"
