"""Rebuild Tech X Terminal at any time, with one command from the repository root.

    python build.py                 everything that is released: portable .exe, installer, checksums
    python build.py --test          the same, after the end-to-end tests have passed
    python build.py --portable      only the portable single-file .exe
    python build.py --installer     only the installer (needs Inno Setup 6)
    python build.py --folder        only the folder build, dist\\TechXTerminal\\TechXTerminal.exe

Each build raises the version first, the last number by default (0.3.0 to 0.3.1), so that no two builds
share a number; --bump minor or --bump major raise another one instead (to 0.4.0, or 1.0.0), and --no-bump
keeps the number as it is. A build that fails puts the number back.

It runs the PowerShell scripts in packaging\\, which stay the one description of how a build is made, and
says at the end what it built, how big each file is and how long it took. 64-bit Windows only, as the
builds are; see README.md, Build.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
VERSION_FILE = ROOT / "techxterminal" / "__init__.py"  # the one place the version is set
VERSION_LINE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"$', re.MULTILINE)


class BuildFailed(Exception):
    pass


def project_python() -> str:
    """The project's own Python, which has its requirements, as the PowerShell scripts use too."""
    venv = ROOT / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


def version() -> str:
    found = VERSION_LINE.search(VERSION_FILE.read_text(encoding="utf-8"))
    if found is None:
        sys.exit(f'No version of the form __version__ = "1.2.3" in {VERSION_FILE.relative_to(ROOT)}')
    return ".".join(found.groups())


def bumped(number: str, part: str) -> str:
    """The version after raising one of its numbers; the ones after it start again at 0."""
    major, minor, patch = (int(value) for value in number.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def set_version(number: str) -> None:
    text = VERSION_FILE.read_text(encoding="utf-8")
    VERSION_FILE.write_text(VERSION_LINE.sub(f'__version__ = "{number}"', text, count=1), encoding="utf-8")


def run(command: list[str], what: str) -> None:
    print(f"\n==> {what}", flush=True)
    if subprocess.run(command, cwd=ROOT).returncode != 0:
        raise BuildFailed(what)


def powershell(script: str, *arguments: str) -> list[str]:
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PACKAGING / script), *arguments]


def build(options, number: str) -> list[Path]:
    """Run the build asked for; returns what it should have made."""
    if options.portable:
        run(powershell("build.ps1", "-OneFile"), "Portable single-file build")
        portable = DIST / f"TechXTerminal-{number}-x64-portable.exe"
        (DIST / "TechXTerminal.exe").replace(portable)  # named as build_release.ps1 names it
        return [portable]
    if options.installer:
        run(powershell("build_installer.ps1"), "Installer")
        return [DIST / f"TechXTerminal-Setup-{number}-x64.exe"]
    if options.folder:
        run(powershell("build.ps1"), "Folder build")
        return [DIST / "TechXTerminal" / "TechXTerminal.exe"]
    run(powershell("build_release.ps1"), "Release: portable .exe, installer and checksums")
    return [
        DIST / f"TechXTerminal-{number}-x64-portable.exe",
        DIST / f"TechXTerminal-Setup-{number}-x64.exe",
        DIST / "SHA256SUMS.txt",
    ]


def report(number: str, built: list[Path], started: float) -> None:
    minutes, seconds = divmod(round(time.monotonic() - started), 60)
    print(f"\nBuilt Tech X Terminal {number} in {minutes}m {seconds:02d}s:")
    for path in built:
        if not path.exists():
            size = "missing"
        elif path.stat().st_size < 2**20:
            size = f"{path.stat().st_size / 2**10:.1f} KB"
        else:
            size = f"{path.stat().st_size / 2**20:.1f} MB"
        print(f"  {path.relative_to(ROOT)}  ({size})")
    if not all(path.exists() for path in built):
        sys.exit("\nSomething that should have been built is missing.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild Tech X Terminal, raising its version first.")
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--portable", action="store_true", help="only the portable single-file .exe")
    kind.add_argument("--installer", action="store_true", help="only the installer (needs Inno Setup 6)")
    kind.add_argument("--folder", action="store_true", help="only the folder build")
    parser.add_argument("--test", action="store_true", help="run the end-to-end tests first, and build only if they pass")
    raise_part = parser.add_mutually_exclusive_group()
    raise_part.add_argument("--bump", choices=("patch", "minor", "major"), default="patch", help="which number of the version to raise (default: patch)")
    raise_part.add_argument("--no-bump", action="store_true", help="build with the version as it is")
    options = parser.parse_args()

    if os.name != "nt":
        sys.exit("The builds are made on Windows (64-bit), with the scripts in packaging\\.")
    previous = version()
    started = time.monotonic()

    if options.test:  # before the version is raised, so that failing tests do not use up a number
        try:
            run([project_python(), str(ROOT / "tests" / "run_e2e.py")], "End-to-end tests")
        except BuildFailed as failed:
            sys.exit(f"\nStopped: {failed} failed. The version stays {previous}.")

    number = previous if options.no_bump else bumped(previous, options.bump)
    if number != previous:
        set_version(number)
        print(f"\nVersion {previous} -> {number}", flush=True)
    try:
        built = build(options, number)
    except BuildFailed as failed:
        if number != previous:
            set_version(previous)
        sys.exit(f"\nStopped: {failed} failed. The version is back at {previous}.")
    report(number, built, started)


if __name__ == "__main__":
    main()
