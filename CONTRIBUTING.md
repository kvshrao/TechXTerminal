# Contributing

Thanks for wanting to help. Tech X Terminal is free software under the GNU General Public License, version 3; by contributing you agree that your contribution is released under the same licence.

## Reporting a bug or asking for a feature

[Open an issue](https://github.com/kvshrao/TechXTerminal/issues/new/choose). For a bug, give the version (click the logo to open About), whether it is the installer or the portable copy, the steps, and what you expected. The error log is in `%USERPROFILE%\.techxterminal\` (or in `TechXTerminal-data` for a portable copy); please remove anything private from it first.

Security problems go through [SECURITY.md](SECURITY.md), not a public issue.

## Changing the code

Set up and run it as described in the [README](README.md):

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

Before sending a pull request:

```
python -m pyflakes techxterminal tests main.py
python tests\run_e2e.py
```

The second opens windows for a minute or two and uses a temporary home folder, so it does not touch your own sessions or settings.

- Keep a change to one thing; open an issue first for anything large.
- Match the style of the code around it: naming, comment density, plain wording.
- Add or extend a check in `tests\run_e2e.py` for behaviour you change.
- Add a line under Unreleased in [CHANGELOG.md](CHANGELOG.md). Do not change the version; `python build.py` does that when a release is built.
- Anything that handles what a server sends must keep treating it as untrusted (see Safety in the README).

Windows 64-bit is the only supported platform.
