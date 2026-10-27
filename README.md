# Tech X Terminal

A modern tabbed SSH terminal for Windows, with a file browser, tunnels and saved sessions. Free software under the GNU General Public License, version 3.

## What it does

- **Connections:** SSH, Telnet, Rlogin, Raw TCP and serial lines, in tabs that can be split into panes. SSH with passwords, keys (OpenSSH and PuTTY `.ppk`), the SSH agent, two-factor prompts, jump hosts, agent forwarding, and local, remote and SOCKS tunnels.
- **Sessions:** saved with folders, favourites, colours and notes; quick connect with suggestions; a command palette (Ctrl+Shift+P); import from an OpenSSH config, PuTTY's saved sessions or a file; export and full backup.
- **Files:** a two-sided file browser (this computer and the server) with a transfer queue. Several transfers run at the same time, each on its own channel, with a shared speed limit, pause, cancel, and partial files that can be continued. Remote files can be edited in a local editor and are saved back.
- **Local shells:** PowerShell, the Command Prompt, WSL or Git Bash in a tab, like any other session.
- **Scheduled tasks:** a series of commands run on a saved SSH session or on this computer, by hand, every so many minutes or every day at a time, with the output kept in a text file. They run while the application is open, or from Windows Task Scheduler, also while it is closed.
- **Terminal:** scrollback with search, copy on select, slow paste for devices that lose characters, automatic responses (`--More--`, `[yes/no]`), session logs, saved commands that can be sent to one tab or all of them, workspaces (saved sets of tabs), and dark, light or system themes.

See [CHANGELOG.md](CHANGELOG.md) for what each version added.

## Run it

You need Python 3.15, which is what it is developed and tested on.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

On the command line:

```
python main.py [SESSION ...]    open saved sessions by name, or addresses such as user@host[:port]
python main.py local://cmd.exe  open a shell on this computer (plain "local" opens PowerShell)
python main.py --list           print the saved sessions
python main.py --tasks          print the scheduled tasks
python main.py --run-task NAME  run a scheduled task without opening the window; exit status 0 if every command succeeded
python main.py --version        print the version
```

## Where things are kept

| What | Where |
| --- | --- |
| Saved sessions, saved commands, scheduled tasks and their output, workspaces, approved host keys, logs, the error log | `%USERPROFILE%\.techxterminal\` |
| Settings and window layout | the registry, under `HKEY_CURRENT_USER\Software\TechX\TechXTerminal` |
| Passwords you chose to save | Windows Credential Manager, under the name `TechXTerminal` |

Passwords are not in the sessions file or in backups. The one exception is a password you write after `-pw` in a session's command line, which is stored as plain text; the session window warns you when you save one.

## Safety

- An unknown host key is shown for you to approve, and a key that has changed is refused.
- Agent forwarding is off unless a session asks for it.
- What a server sends is not trusted: file names are made into plain names before anything is written (a name such as `..\..\x` cannot put a file outside the folder you chose), a script or program fetched for editing is shown in Notepad and never run, and names and messages from a server are shown as written, not drawn as HTML.

## Build

`python build.py` rebuilds everything that is released, in one go, raising the version first; `--test` runs the end-to-end tests first, and `--portable`, `--installer` or `--folder` build just one of them. It runs the scripts in `packaging\`: `packaging\build.ps1` builds the application into `dist\`, `packaging\build_installer.ps1` the Windows installer (it needs [Inno Setup](https://jrsoftware.org/isinfo.php)), and `packaging\build_release.ps1` the portable `.exe`, the installer and a file of checksums. The builds are 64-bit Windows only, because Qt 6 does not support 32-bit Windows.

## Portable copy

Put a folder named `TechXTerminal-data` next to the program (the portable `.exe`, say, on a USB stick) and it keeps the saved sessions, saved commands, approved host keys, logs and preferences there, instead of in `.techxterminal` in your user folder and in the registry. Nothing is left behind on the computer it runs on except saved passwords, which stay in that computer's credential store and do not travel with the folder. About shows "Data folder (portable)" when it is in use.

## Releasing

The version is set in one place, `__version__` in `techxterminal\__init__.py`; the installer, the properties of the `.exe`, About and the file names of a build all follow it. `python build.py` raises its last number before each build (0.3.0 to 0.3.1), so no two builds share a number; `--bump minor` or `--bump major` raise another one instead, `--no-bump` keeps it, and a build that fails puts it back. For a release, rename the Unreleased heading in `CHANGELOG.md` to the version it is built as.

## Tests

```
python tests\run_e2e.py
python -m pyflakes techxterminal tests main.py
```

The first drives the real application against throwaway SSH, Telnet and serial servers on this computer. It opens windows for a minute or two, uses a temporary home folder, and does not touch your own sessions or settings.

## Licence

Tech X Terminal is released under the GNU General Public License, version 3: see [LICENSE](LICENSE) and [NOTICE](NOTICE). It is built on other free software, each part under its own licence; builds include their texts in `THIRD_PARTY_NOTICES.txt`. If you use it or build on it, the author would like to hear about it: please [open an issue](https://github.com/kvshrao/TechXTerminal/issues). That is a request, not a condition. To report a bug or suggest a change, see [CONTRIBUTING.md](CONTRIBUTING.md); to report a security problem, see [SECURITY.md](SECURITY.md).
