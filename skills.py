"""Procedural memory: plain-text skills the organism distills from
experience (Hermes-style reflected skills). Each skill is a markdown
file in artifacts/skills/ — name / when / how plus a meta line tracking
uses and cycles. Stale skills (untouched for `limit` cycles) are moved
to archive/. No textual imports — unit testable without a terminal."""

import re
from dataclasses import dataclass
from pathlib import Path

_STOP = {"the", "a", "an", "is", "to", "of", "and", "it", "i", "you",
         "my", "me", "when", "how", "on", "in", "at", "be", "are"}


@dataclass
class Skill:
    name: str
    when: str
    how: str
    uses: int = 0
    created_cycle: int = 0
    updated_cycle: int = 0


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _words(text):
    return set(re.findall(r"[a-z]+", text.lower())) - _STOP


class SkillStore:
    """A directory of skill files; the index is derived by scanning."""

    def __init__(self, dir_path):
        self.dir_path = Path(dir_path)

    def _path(self, name):
        return self.dir_path / f"{_slug(name)}.md"

    def _render(self, s):
        return (f"# {s.name}\nwhen: {s.when}\nhow: {s.how}\n"
                f"meta: uses={s.uses} created={s.created_cycle} "
                f"updated={s.updated_cycle}\n")

    def _parse(self, text):
        lines = text.splitlines()
        if not lines or not lines[0].startswith("# "):
            return None
        fields = {}
        for line in lines[1:]:
            if ": " in line:
                key, value = line.split(": ", 1)
                fields[key.strip()] = value.strip()
        meta = dict(p.split("=", 1) for p in fields.get("meta", "").split()
                    if "=" in p)
        return Skill(
            name=lines[0][2:].strip(),
            when=fields.get("when", ""),
            how=fields.get("how", ""),
            uses=int(meta.get("uses", 0)),
            created_cycle=int(meta.get("created", 0)),
            updated_cycle=int(meta.get("updated", 0)))

    def save(self, skill):
        """Create or patch a skill file. Patching preserves the use count
        and the original creation cycle."""
        self.dir_path.mkdir(parents=True, exist_ok=True)
        existing = self.get(skill.name)
        if existing is not None:
            skill.uses = max(skill.uses, existing.uses)
            skill.created_cycle = existing.created_cycle
        self._path(skill.name).write_text(self._render(skill))

    def get(self, name):
        path = self._path(name)
        if not path.exists():
            return None
        return self._parse(path.read_text())

    def list(self):
        if not self.dir_path.is_dir():
            return []
        out = []
        for path in sorted(self.dir_path.glob("*.md")):
            skill = self._parse(path.read_text())
            if skill is not None:
                out.append(skill)
        return out

    def record_use(self, name, cycle=0):
        """Count one retrieval; also touches the updated cycle so active
        skills never go stale."""
        skill = self.get(name)
        if skill is None:
            return
        skill.uses += 1
        skill.updated_cycle = max(skill.updated_cycle, cycle)
        self._path(skill.name).write_text(self._render(skill))

    def archive_stale(self, cycle, limit=100):
        """Move skills untouched for `limit` cycles to archive/; returns
        the names archived."""
        archived = []
        for skill in self.list():
            if cycle - skill.updated_cycle >= limit:
                archive = self.dir_path / "archive"
                archive.mkdir(parents=True, exist_ok=True)
                self._path(skill.name).rename(
                    archive / self._path(skill.name).name)
                archived.append(skill.name)
        return archived

    def relevant(self, context, limit=3):
        """Skills whose name/when keywords overlap the context, most
        overlapping first."""
        context_words = _words(context)
        scored = []
        for skill in self.list():
            overlap = len(context_words
                         & _words(f"{skill.name} {skill.when}"))
            if overlap:
                scored.append((overlap, skill))
        scored.sort(key=lambda t: -t[0])
        return [s for _n, s in scored[:limit]]
