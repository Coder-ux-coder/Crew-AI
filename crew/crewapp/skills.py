"""The skills library: built-in skills, skills the teams created, and which are switched on.

Agents load one combined "active" pack (~/.crew/skills-active), rebuilt whenever
a skill is added or switched on/off, so a switched-off skill never reaches them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

from crewlib.util import atomic_write, crew_home

BUILTIN = Path(__file__).resolve().parent.parent / "skills_pack" / "skills"
SKILL_ID = r"[a-z0-9][a-z0-9-]{0,63}"  # a skill's id is at most 48 letters; a far longer one is not a skill

# What each built-in skill does, in the owner's words (the SKILL.md descriptions are written for the agents).
SUMMARIES = {
    "create-skill": "When a member finds a way of working that others will need again, they write it down as a new "
                    "skill, so every future team starts with it.",
    "handover-note": "Members keep clear notes on their work, so anyone (even a different AI) can take over a task "
                     "without asking questions.",
    "usage-thrift": "Saves your subscription limits without lowering quality: used when a subscription is running low "
                    "or a job involves very large files.",
    "verify-before-submit": "A checklist every member runs before handing work in, so checks pass the first time.",
    "visual-check": "Anything people will look at is opened in a real browser, at computer and phone sizes, and "
                    "checked for mistakes before it is handed in.",
}


def user_dir() -> Path:
    path = crew_home() / "skills" / "skills"
    path.mkdir(parents=True, exist_ok=True)
    manifest = crew_home() / "skills" / ".claude-plugin" / "plugin.json"
    if not manifest.is_file():
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": "crew-team-skills", "version": "1.0.0",
                                        "description": "Skills created by past Crew teams.",
                                        "author": {"name": "Crew teams"}}), encoding="utf-8")
    return path


def _state_path() -> Path:
    return crew_home() / "skills-state.json"


def _disabled() -> set[str]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return {str(x) for x in data.get("disabled", [])} if isinstance(data, dict) else set()
    except (OSError, ValueError, TypeError):
        return set()


def _unquote(value: str) -> str:
    """A front-matter value as written: plain, "double-quoted" (JSON escapes) or 'single-quoted'."""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return str(json.loads(v))
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def _parse(skill_md: Path) -> dict:
    text = skill_md.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                meta[k.strip()] = _unquote(v)
        body = text[m.end():]
    else:
        body = text
    return {"name": meta.get("name") or skill_md.parent.name, "description": meta.get("description", ""), "body": body}


def list_all() -> list[dict]:
    disabled = _disabled()
    out = []
    for origin, root in (("built-in", BUILTIN), ("created", user_dir())):
        if not root.is_dir():
            continue
        for d in sorted(p for p in root.iterdir() if (p / "SKILL.md").is_file()):
            info = _parse(d / "SKILL.md")
            out.append({"id": d.name, "name": info["name"], "description": info["description"],
                        "summary": SUMMARIES.get(d.name, "") if origin == "built-in" else "",
                        "origin": origin, "enabled": d.name not in disabled})
    return out


def get(skill_id: str) -> dict | None:
    for root in (BUILTIN, user_dir()):
        path = root / skill_id / "SKILL.md"
        if isinstance(skill_id, str) and re.fullmatch(SKILL_ID, skill_id) and path.is_file():
            info = _parse(path)
            return {"id": skill_id, **info, "origin": "built-in" if root == BUILTIN else "created",
                    "enabled": skill_id not in _disabled()}
    return None


def create(name: str, when: str, steps: str) -> dict:
    if not all(isinstance(x, str) for x in (name, when, steps)):
        raise ValueError("Give the skill a name, say when it is used, and describe its steps.")
    if not name.strip():
        raise ValueError("Give the skill a name.")
    skill_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48]
    if not skill_id:  # a name in Urdu (or any other script): the id is made from it, the title keeps it
        skill_id = "skill-" + hashlib.sha1(name.strip().encode("utf-8")).hexdigest()[:8]
    if skill_id == "anthropic":  # the app's address for Anthropic's own skills
        skill_id = "anthropic-skill"
    if len(when.strip()) < 10 or len(steps.strip()) < 20:
        raise ValueError("Say when the skill should be used and describe its steps.")
    if (BUILTIN / skill_id).exists() or (user_dir() / skill_id).exists():
        raise ValueError("A skill with this name already exists.")
    description = " ".join(when.split())
    if not description.lower().startswith("use when"):
        description = "Use when " + description[0].lower() + description[1:]
    # Quoted, so that a colon or a # in the owner's words cannot break the skill's front matter for Claude Code.
    text = (f"---\nname: {skill_id}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n\n"
            f"# {' '.join(name.split())}\n\n{steps.strip()}\n")
    folder = user_dir() / skill_id
    folder.mkdir(parents=True)
    atomic_write(folder / "SKILL.md", text)
    build_active_pack()
    return get(skill_id)


def set_enabled(skill_id: str, enabled: bool) -> dict | None:
    if not isinstance(skill_id, str) or get(skill_id) is None:
        return None
    disabled = _disabled()
    (disabled.discard if enabled else disabled.add)(skill_id)
    atomic_write(_state_path(), json.dumps({"disabled": sorted(disabled)}))
    build_active_pack()
    return get(skill_id)


def delete(skill_id: str) -> bool:
    if not isinstance(skill_id, str) or not re.fullmatch(SKILL_ID, skill_id):
        return False
    folder = user_dir() / skill_id
    if not folder.is_dir():
        return False
    shutil.rmtree(folder)
    build_active_pack()
    return True


def build_active_pack() -> Path:
    """One plugin folder holding every switched-on skill (what agents actually load)."""
    active = crew_home() / "skills-active"
    tmp = crew_home() / ".skills-active-new"
    shutil.rmtree(tmp, ignore_errors=True)
    (tmp / ".claude-plugin").mkdir(parents=True)
    (tmp / ".claude-plugin" / "plugin.json").write_text(json.dumps({
        "name": "crew-skills", "version": "1.0.0",
        "description": "Crew skills: built-in and created by teams (switched-on ones only).",
        "author": {"name": "Crew"}}), encoding="utf-8")
    disabled = _disabled()
    for root in (BUILTIN, user_dir()):
        if root.is_dir():
            for d in root.iterdir():
                if (d / "SKILL.md").is_file() and d.name not in disabled:
                    shutil.copytree(d, tmp / "skills" / d.name, dirs_exist_ok=True)
    shutil.rmtree(active, ignore_errors=True)
    try:
        tmp.rename(active)
    except OSError:  # Windows: a file still open (antivirus scan, a running agent) blocks the swap
        shutil.copytree(tmp, active, dirs_exist_ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
    return active
