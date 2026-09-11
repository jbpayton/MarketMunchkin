"""Skills: packaged procedures the agent loads on demand.

Format follows the open Agent Skills convention: a folder per skill with SKILL.md (YAML-style front matter with
`name` and `description`, then the procedure), optional scripts/*.py that run in the analysis sandbox, and optional
resources/* reference files. Two roots:

  skills/          operator-curated, versioned with the repo, active by default
  data/skills/     written by the agent with save_skill; drafts until the operator approves them on the dashboard

Only the index (name + description) is in the system prompt; bodies load on demand, so context cost stays small.
The fixed trading rules are not affected by skills: a skill is instructions, the risk engine is code.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import DATA_DIR, ROOT

SKILLS_DIR = ROOT / "skills"
AGENT_SKILLS_DIR = DATA_DIR / "skills"
STATE_FILE = DATA_DIR / "skills.json"
MAX_BODY = 6000
MAX_RESOURCE = 8000
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")


@dataclass
class Skill:
    name: str
    description: str
    body: str
    source: str            # repo | agent
    status: str            # active | draft
    enabled: bool
    path: Path
    scripts: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    updated: str = ""

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "source": self.source, "status": self.status, "enabled": self.enabled,
                "scripts": self.scripts, "resources": self.resources, "tags": self.tags, "chars": len(self.body), "updated": self.updated}


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    text = text.replace("\r", "")
    if not text.startswith("---\n"):
        return {}, text.strip()
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith(" "):
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip().strip('"').strip("'")
    body = text[end + 4:]
    if body.startswith("-"):
        body = body[1:]
    return meta, body.strip()


class SkillStore:
    def __init__(self, repo_dir: Path = SKILLS_DIR, agent_dir: Path = AGENT_SKILLS_DIR, state_file: Path = STATE_FILE) -> None:
        self.repo_dir, self.agent_dir, self.state_file = repo_dir, agent_dir, state_file
        self.errors: list[str] = []

    # ------------------------------------------------------------------ state (dashboard toggles)
    def _state(self) -> dict[str, list[str]]:
        try:
            d = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        except Exception:
            d = {}
        return {"disabled": list(d.get("disabled", [])), "approved": list(d.get("approved", []))}

    def _save_state(self, st: dict[str, list[str]]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(st, indent=1))

    # ------------------------------------------------------------------ loading
    def _load_one(self, folder: Path, source: str, st: dict[str, list[str]]) -> Skill | None:
        f = folder / "SKILL.md"
        if not f.exists():
            return None
        name = folder.name
        if not NAME_RE.match(name):
            self.errors.append(f"{name}: folder name must be a slug (a-z, 0-9, -)")
            return None
        meta, body = parse_frontmatter(f.read_text(encoding="utf-8"))
        if meta.get("name", name) != name:
            self.errors.append(f"{name}: front matter name '{meta.get('name')}' must match the folder")
            return None
        desc = (meta.get("description") or "").strip()
        if not desc or len(desc) > 240:
            self.errors.append(f"{name}: description missing or over 240 chars")
            return None
        if len(body) > MAX_BODY:
            self.errors.append(f"{name}: body is {len(body)} chars; the cap is {MAX_BODY}")
            return None
        scripts = sorted(p.name for p in (folder / "scripts").glob("*.py")) if (folder / "scripts").is_dir() else []
        resources = sorted(p.name for p in (folder / "resources").iterdir() if p.is_file()) if (folder / "resources").is_dir() else []
        status = "active" if source == "repo" or name in st["approved"] else "draft"
        if meta.get("status") == "active" and source == "agent" and name not in st["approved"]:
            status = "draft"   # only the operator can activate an agent-written skill
        tags = [t.strip() for t in (meta.get("tags") or "").split(",") if t.strip()]
        updated = meta.get("updated") or dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="minutes")
        return Skill(name, desc, body, source, status, name not in st["disabled"], folder, scripts, resources, tags, updated)

    def all(self) -> list[Skill]:
        self.errors = []
        st = self._state()
        out: dict[str, Skill] = {}
        for root, source in ((self.repo_dir, "repo"), (self.agent_dir, "agent")):
            if not root.is_dir():
                continue
            for folder in sorted(p for p in root.iterdir() if p.is_dir()):
                s = self._load_one(folder, source, st)
                if s is None:
                    continue
                if s.name in out:
                    self.errors.append(f"{s.name}: agent draft shadows a repo skill and is ignored")
                    continue
                out[s.name] = s
        return sorted(out.values(), key=lambda s: s.name)

    def get(self, name: str) -> Skill | None:
        return next((s for s in self.all() if s.name == name), None)

    def active(self) -> list[Skill]:
        return [s for s in self.all() if s.status == "active" and s.enabled]

    def index_text(self) -> str:
        rows = self.active()
        if not rows:
            return "(no skills installed)"
        return "\n".join(f"- {s.name}: {s.description}" + (f" [scripts: {', '.join(s.scripts)}]" if s.scripts else "") for s in rows)

    # ------------------------------------------------------------------ contents
    def script_source(self, name: str, script: str) -> tuple[str | None, str]:
        s = self.get(name)
        if not s:
            return None, f"no skill '{name}'"
        if s.status != "active" or not s.enabled:
            return None, f"skill '{name}' is {s.status}{'' if s.enabled else ' and disabled'}; it must be approved and enabled before its scripts run"
        if script not in s.scripts:
            return None, f"skill '{name}' has no script '{script}' (available: {', '.join(s.scripts) or 'none'})"
        return (s.path / "scripts" / script).read_text(encoding="utf-8"), ""

    def resource_text(self, name: str, file: str) -> str:
        s = self.get(name)
        if not s:
            return f"no skill '{name}'"
        if file not in s.resources:
            return f"skill '{name}' has no resource '{file}' (available: {', '.join(s.resources) or 'none'})"
        try:
            t = (s.path / "resources" / file).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"cannot read {file}: {e}"
        return t[:MAX_RESOURCE] + ("\n...[truncated]" if len(t) > MAX_RESOURCE else "")

    # ------------------------------------------------------------------ agent-authored drafts
    def save_draft(self, name: str, description: str, body: str, tags: list[str] | None = None) -> str:
        if not NAME_RE.match(name or ""):
            return "REJECTED: name must be a slug like cpi-day (a-z, 0-9, -)"
        if (self.repo_dir / name).is_dir():
            return f"REJECTED: '{name}' is an operator skill; pick another name or ask the operator to change it"
        description = (description or "").strip()
        if not description or len(description) > 240:
            return "REJECTED: description is required (one sentence, <= 240 chars)"
        body = (body or "").strip()
        if len(body) < 300:
            return "REJECTED: body too short; a skill needs the trigger (when to use it), the procedure, the thresholds and the exit"
        if len(body) > MAX_BODY:
            return f"REJECTED: body is {len(body)} chars; cap is {MAX_BODY}"
        folder = self.agent_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        existing = self.get(name)
        was = "updated" if existing else "created"
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\nstatus: draft\nauthor: agent\nupdated: {dt.datetime.now().isoformat(timespec='minutes')}\n"
            f"tags: {', '.join(tags or [])}\n---\n{body}\n", encoding="utf-8")
        st = self._state()
        if name in st["approved"]:      # an edit to an approved skill goes back to draft for re-approval
            st["approved"].remove(name)
            self._save_state(st)
        return f"{was} draft skill '{name}' ({len(body)} chars); it stays a draft until the operator approves it on the dashboard"

    def approve(self, name: str) -> bool:
        s = self.get(name)
        if not s or s.source != "agent":
            return False
        st = self._state()
        if name not in st["approved"]:
            st["approved"].append(name)
        self._save_state(st)
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if not self.get(name):
            return False
        st = self._state()
        if enabled and name in st["disabled"]:
            st["disabled"].remove(name)
        if not enabled and name not in st["disabled"]:
            st["disabled"].append(name)
        self._save_state(st)
        return True

    def delete_draft(self, name: str) -> bool:
        s = self.get(name)
        if not s or s.source != "agent":
            return False
        for p in sorted(s.path.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else p.rmdir()
        s.path.rmdir()
        st = self._state()
        st["approved"] = [x for x in st["approved"] if x != name]
        self._save_state(st)
        return True
