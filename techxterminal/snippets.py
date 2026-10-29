"""Saved commands ("snippets") that can be sent to a tab with one click."""

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .sessions import CONFIG_DIR, write_atomically

SNIPPETS_FILE = CONFIG_DIR / "snippets.json"

# {?label} or {?label=default}: asked for when the command is sent, so one saved command can serve many uses.
_PROMPT = re.compile(r"\{\?([^{}=]+?)(?:=([^{}]*))?\}")

# Placeholders a saved command may contain; each is filled in from the tab it is sent to.
# Only these exact words are replaced, so braces in ordinary commands (awk, find) are left alone.
PLACEHOLDERS = {
    "{host}": lambda session: session.host,
    "{user}": lambda session: session.username,
    "{port}": lambda session: str(session.port),
    "{name}": lambda session: session.title,
}


def fill_placeholders(command: str, session) -> str:
    """The command with {host}, {user}, {port} and {name} replaced by the session's values."""
    for placeholder, value in PLACEHOLDERS.items():
        command = command.replace(placeholder, value(session))
    return command


def prompts_in(command: str) -> list[tuple[str, str]]:
    """The values a command asks for, as (label, default), each label once, in the order it first appears."""
    asked: dict[str, str] = {}
    for match in _PROMPT.finditer(command):
        asked.setdefault(match[1].strip(), (match[2] or "").strip())
    return list(asked.items())


def fill_prompts(command: str, answers: dict[str, str]) -> str:
    """The command with each {?label} replaced by its answer; a line break in an answer becomes a space,
    so that one answer can never become a second command."""

    def answer(match: re.Match) -> str:
        label = match[1].strip()
        return re.sub(r"[\r\n]+", " ", answers.get(label, (match[2] or "").strip()))

    return _PROMPT.sub(answer, command)


@dataclass
class Snippet:
    name: str = ""
    command: str = ""  # one or more lines; each line is sent followed by Enter

    @property
    def title(self) -> str:
        return self.name or self.command.strip().splitlines()[0] if (self.name or self.command.strip()) else "(empty)"


class SnippetStore:
    def __init__(self, path: Path = SNIPPETS_FILE):
        self.path = path
        self.snippets: list[Snippet] = []
        self.load()

    def load(self) -> None:
        self.snippets = []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw["snippets"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        for item in items:
            if isinstance(item, dict) and str(item.get("command", "")).strip():
                self.snippets.append(Snippet(str(item.get("name", "")), str(item["command"])))

    def save(self) -> None:
        data = {"snippets": [asdict(snippet) for snippet in self.snippets]}
        write_atomically(self.path, json.dumps(data, indent=2))

    def replace_all(self, snippets: list[Snippet]) -> None:
        self.snippets = [snippet for snippet in snippets if snippet.command.strip()]
        self.save()
