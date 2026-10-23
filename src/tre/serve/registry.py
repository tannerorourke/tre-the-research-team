"""Registry of repository roots the supervisor watches, persisted as JSON at
'~/.tre/registry.json'.

    python -m pytest -q tests/test_supervisor.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


class Registry:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else Path.home() / ".tre" / "registry.json"

    def roots(self) -> list[Path]:
        if not self.path.exists():
            return []
        return [Path(p) for p in json.loads(self.path.read_text())["roots"]]

    def add(self, root: Path) -> Path:
        root = Path(root).resolve()
        roots = self.roots()
        if root not in roots:
            self._save([*roots, root])
        return root

    def remove(self, root: Path) -> None:
        root = Path(root).resolve()
        self._save([r for r in self.roots() if r != root])

    def _save(self, roots: list[Path]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"roots": [str(r) for r in roots]}, indent=2))
        tmp.replace(self.path)
