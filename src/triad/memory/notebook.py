"""The lab notebook: a disciplined docs/ folder.

Every artifact is a markdown file with flat frontmatter, a stable ID
(HYP-001, EXP-003, TCK-014, ADR-002, LIT-007, REP-001), and [[ID]]
cross-links. Humans read and edit it in any editor; agents read and write it
through tools; indexes are rebuilt, never hand-maintained.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from string import Template
from typing import Optional


@dataclass(frozen=True)
class Kind:
    key: str
    subdir: str
    prefix: str
    template: str


KINDS: dict[str, Kind] = {
    "literature": Kind("literature", "literature", "LIT", "literature_note.md"),
    "hypothesis": Kind("hypothesis", "hypotheses", "HYP", "hypothesis.md"),
    "experiment": Kind("experiment", "experiments", "EXP", "experiment.md"),
    "adr": Kind("adr", "decisions", "ADR", "adr.md"),
    "ticket": Kind("ticket", "tickets", "TCK", "ticket.md"),
    "report": Kind("report", "reports", "REP", "report.md"),
}

LINK_RE = re.compile(r"\[\[([A-Z]+-\d+)\]\]")
ID_RE = re.compile(r"^([A-Z]+)-(\d+)")


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def slug(text: str, limit: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "untitled"


def links_of(text: str) -> list[str]:
    return sorted(set(LINK_RE.findall(text)))


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    lines = text.split("\n")
    fm: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return fm, "\n".join(lines[i + 1:]).lstrip("\n")
        if ":" in line:
            key, _, value = line.partition(":")
            fm[key.strip()] = value.split("#", 1)[0].strip()
    return {}, text  # unterminated frontmatter: treat as body


def serialize(fm: dict[str, str], body: str) -> str:
    head = "\n".join(f"{k}: {v}" for k, v in fm.items())
    return f"---\n{head}\n---\n\n{body.lstrip()}" if fm else body


class LabNotebook:
    def __init__(self, docs_dir: Path):
        self.docs = Path(docs_dir)

    # -- scaffold ----------------------------------------------------------

    def ensure_scaffold(self) -> None:
        self.docs.mkdir(parents=True, exist_ok=True)
        for kind in KINDS.values():
            (self.docs / kind.subdir).mkdir(exist_ok=True)
        (self.docs / "standups").mkdir(exist_ok=True)
        (self.docs / "knowledge").mkdir(exist_ok=True)
        (self.docs / "graphs").mkdir(exist_ok=True)
        charter = self.docs / "charter.md"
        if not charter.exists():
            charter.write_text(self._template("charter.md").safe_substitute(date=today()))
        for kind in KINDS.values():
            index = self.docs / kind.subdir / "INDEX.md"
            if not index.exists():
                self.rebuild_index(kind.key)

    # -- ids & paths -------------------------------------------------------

    def next_id(self, kind_key: str) -> str:
        kind = KINDS[kind_key]
        best = 0
        for path in (self.docs / kind.subdir).glob(f"{kind.prefix}-*.md"):
            m = ID_RE.match(path.name)
            if m:
                best = max(best, int(m.group(2)))
        return f"{kind.prefix}-{best + 1:03d}"

    def path_of(self, entry_id: str) -> Optional[Path]:
        m = ID_RE.match(entry_id)
        if not m:
            return None
        for kind in KINDS.values():
            if kind.prefix == m.group(1):
                hits = sorted((self.docs / kind.subdir).glob(f"{entry_id}-*.md"))
                hits += [p for p in [(self.docs / kind.subdir / f"{entry_id}.md")] if p.exists()]
                return hits[0] if hits else None
        return None

    # -- create / read / update -------------------------------------------

    def create(self, kind_key: str, title: str, fields: Optional[dict] = None) -> tuple[str, Path]:
        kind = KINDS[kind_key]
        entry_id = self.next_id(kind_key)
        raw = resources.files("triad.templates").joinpath(kind.template).read_text()
        idents = set(re.findall(r"\$([a-z_]+)", raw))
        mapping: dict[str, str] = {i: "_" for i in idents}
        mapping.update({"id": entry_id, "title": title, "date": today()})
        mapping.update({k: str(v) for k, v in (fields or {}).items() if k in idents})
        # substitute once with every identifier provided, so "$" in *values*
        # (e.g. LaTeX) is never re-interpreted
        text = Template(raw).substitute(mapping)
        path = self.docs / kind.subdir / f"{entry_id}-{slug(title)}.md"
        path.write_text(text)
        self.rebuild_index(kind_key)
        return entry_id, path

    def read(self, entry_id: str) -> str:
        path = self.path_of(entry_id)
        if not path:
            raise FileNotFoundError(f"no notebook entry {entry_id!r}")
        return path.read_text()

    def frontmatter(self, entry_id: str) -> dict[str, str]:
        fm, _ = parse_frontmatter(self.read(entry_id))
        return fm

    def set_frontmatter(self, entry_id: str, **updates: str) -> None:
        path = self.path_of(entry_id)
        if not path:
            raise FileNotFoundError(f"no notebook entry {entry_id!r}")
        fm, body = parse_frontmatter(path.read_text())
        fm.update({k: str(v) for k, v in updates.items()})
        path.write_text(serialize(fm, body))
        m = ID_RE.match(entry_id)
        for kind in KINDS.values():
            if m and kind.prefix == m.group(1):
                self.rebuild_index(kind.key)

    def append(self, entry_id: str, heading: str, content: str) -> None:
        path = self.path_of(entry_id)
        if not path:
            raise FileNotFoundError(f"no notebook entry {entry_id!r}")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        with path.open("a") as f:
            f.write(f"\n\n### {heading} — {stamp}\n\n{content.strip()}\n")

    # -- listing / indexes -------------------------------------------------

    def entries(self, kind_key: str) -> list[dict]:
        kind = KINDS[kind_key]
        out = []
        for path in sorted((self.docs / kind.subdir).glob(f"{kind.prefix}-*.md")):
            fm, _ = parse_frontmatter(path.read_text())
            m = ID_RE.match(path.name)
            fallback_id = m.group(0) if m else path.stem
            out.append({
                "id": fm.get("id", fallback_id),
                "title": fm.get("title", path.stem),
                "status": fm.get("status", ""),
                "links": fm.get("links", ""),
                "path": str(path),
            })
        return out

    def rebuild_index(self, kind_key: str) -> Path:
        kind = KINDS[kind_key]
        rows = self.entries(kind_key)
        lines = [
            f"# {kind.subdir.capitalize()} index",
            "",
            "<!-- generated by triad; do not edit by hand -->",
            "",
            "| ID | Title | Status | Links |",
            "|----|-------|--------|-------|",
        ]
        for r in rows:
            lines.append(f"| {r['id']} | {r['title']} | {r['status']} | {r['links']} |")
        if not rows:
            lines.append("| — | (empty) | | |")
        index = self.docs / kind.subdir / "INDEX.md"
        index.write_text("\n".join(lines) + "\n")
        return index

    def digest(self, max_per_kind: int = 8) -> str:
        """Compact cross-kind summary used to prime chat sessions."""
        parts = []
        charter = self.docs / "charter.md"
        if charter.exists():
            parts.append("## Charter\n" + charter.read_text()[:1500])
        for key in ("hypothesis", "experiment", "ticket", "report"):
            rows = self.entries(key)[-max_per_kind:]
            if rows:
                listing = "\n".join(f"- {r['id']} [{r['status']}] {r['title']}" for r in rows)
                parts.append(f"## {key.capitalize()}s\n{listing}")
        return "\n\n".join(parts) if parts else "(notebook is empty)"

    # -- templates ---------------------------------------------------------

    @staticmethod
    def _template(name: str) -> Template:
        text = resources.files("triad.templates").joinpath(name).read_text()
        return Template(text)
