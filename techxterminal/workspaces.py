"""Workspaces: named sets of saved sessions that are opened together."""

import json
from pathlib import Path

from .sessions import CONFIG_DIR, write_atomically

WORKSPACES_FILE = CONFIG_DIR / "workspaces.json"


class WorkspaceStore:
    def __init__(self, path: Path = WORKSPACES_FILE):
        self.path = path
        self._workspaces: dict[str, list[str]] = {}
        self._layouts: dict[str, dict[str, list[str]]] = {}  # how each one's tabs were arranged: pins, panes, sizes
        self.load()

    def load(self) -> None:
        self._workspaces, self._layouts = {}, {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            raw = data["workspaces"]
        except (OSError, ValueError, KeyError, TypeError):
            return
        if isinstance(raw, dict):
            for name, ids in raw.items():
                if isinstance(name, str) and isinstance(ids, list):
                    self._workspaces[name] = [str(session_id) for session_id in ids]
        layouts = data.get("layouts")
        for name, layout in layouts.items() if isinstance(layouts, dict) else ():
            if name in self._workspaces and isinstance(layout, dict):
                self._layouts[name] = {
                    str(key): [str(item) for item in value] for key, value in layout.items() if isinstance(value, list)
                }

    def save(self) -> None:
        write_atomically(self.path, json.dumps({"workspaces": self._workspaces, "layouts": self._layouts}, indent=2))

    def names(self) -> list[str]:
        return sorted(self._workspaces, key=str.lower)

    def get(self, name: str) -> list[str]:
        """The ids of the sessions in a workspace, in the order they were open."""
        return list(self._workspaces.get(name, []))

    def get_layout(self, name: str) -> dict[str, list[str]]:
        """How the tabs were arranged (pinned, panes, sizes...), by position in get(); empty for an older workspace."""
        return {key: list(value) for key, value in self._layouts.get(name, {}).items()}

    def set(self, name: str, session_ids: list[str], layout: dict[str, list[str]] | None = None) -> None:
        self._workspaces[name] = list(session_ids)
        self._layouts[name] = {key: list(value) for key, value in (layout or {}).items()}
        self.save()

    def remove(self, name: str) -> None:
        if self._workspaces.pop(name, None) is not None:
            self._layouts.pop(name, None)
            self.save()
