"""Read and write the owner's settings (~/.crew/crew.toml), rules, and API keys.

The app is the settings editor, so the file is rewritten in a clean, commented
layout on every save (a backup of the previous version is kept).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from crewlib import config as cfgmod
from crewlib.prompts import team_rules
from crewlib.util import atomic_write, crew_home, load_env_file

# Models offered in the pickers. The owner can type any other name too; the ban list still applies.
KNOWN_MODELS = [
    {"id": "claude-opus-5-5", "label": "Opus 5.5", "note": "Best for everyday work", "engine": "claude"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1", "note": "Most capable, tighter limits", "engine": "claude"},
    {"id": "claude-opus-5", "label": "Opus 5", "note": "Previous Opus", "engine": "claude"},
    {"id": "gpt-6-sol", "label": "GPT-6 Sol", "note": "Workhorse for coding and everyday work", "engine": "codex"},
    {"id": "gpt-6-astra", "label": "GPT-6 Astra", "note": "Frontier intelligence for the most demanding work",
     "engine": "codex"},
]
EFFORTS = list(cfgmod.EFFORT_CHOICES)
CODEX_EFFORTS = ["auto", "low", "medium", "high", "xhigh", "max", "ultra"]  # GPT-6's own names (no "minimal")
APP_DEFAULTS = {
    "chat_engine": "claude",
    "chat_model": "claude-opus-5-5",
    "chat_effort": "auto",
    "chat_account": "",
    "codex_model": "gpt-6-sol",
    "codex_effort": "auto",
    "start_with_windows": False,
    "voice_name": "",
    "voice_rate": 1.0,
    "auto_read": False,
    "dictation_lang": "en-US",
    "theme": "system",
    "accent": "clay",
    "font": "serif",
    "owner_name": "",
    "phone_enabled": False,
    "settings_version": 3,
}


def path() -> Path:
    return crew_home() / "crew.toml"


def secrets_path() -> Path:
    return crew_home() / "secrets.env"


def _raw() -> dict:
    p = path()
    if not p.is_file():
        return {}
    return tomllib.loads(p.read_text(encoding="utf-8"))


SETTINGS_VERSION = 3
# Each settings version: values an older Crew wrote as its defaults, and what they became. Only an exact old
# default is changed, and each step runs once, so a choice the owner made afterwards is never overwritten.
MIGRATIONS = {
    2: [("models", "effort_work", "high", "auto"), ("models", "effort_light", "medium", "auto"),
        ("models", "effort_ceo", "high", "max"), ("team", "max_hours", 3.0, 0.0),
        ("app", "chat_effort", "high", "auto"), ("app", "accent", "green", "clay")],
    # 2.1: the three-tier team (GPT-6 Sol workhorse, Opus 5.5 manager, GPT-6 Astra CEO); GPT-6 has no "minimal"
    3: [("models", "ceo", "claude-fable-5-1", "gpt-6-astra"), ("models", "codex", "", "gpt-6-sol"),
        ("app", "codex_model", "", "gpt-6-sol"), ("app", "codex_effort", "minimal", "low")],
}


def _migrate(raw: dict) -> dict:
    """Bring a settings file from an older Crew up to date without losing anything the owner chose."""
    have = int((raw.get("app") or {}).get("settings_version") or 1)
    if have >= SETTINGS_VERSION:
        return raw
    for version in sorted(MIGRATIONS):
        if version <= have:
            continue
        for section, key, old, new in MIGRATIONS[version]:
            sec = raw.get(section)
            if isinstance(sec, dict) and key in sec and sec[key] == old:
                sec[key] = new
    raw.setdefault("app", {})["settings_version"] = SETTINGS_VERSION
    return raw


def load() -> dict:
    """Everything the Settings screen shows, with defaults filled in."""
    raw = _raw()
    if raw and int((raw.get("app") or {}).get("settings_version") or 1) < SETTINGS_VERSION:
        migrated = _migrate(raw)
        try:
            atomic_write(path(), dump({"team": migrated.get("team") or {}, "models": migrated.get("models") or {},
                                       "app": migrated.get("app") or {}, "account": migrated.get("account") or [],
                                       "seat": migrated.get("seat") or []}))
        except OSError:
            pass
        raw = _raw()
    cfg = cfgmod.load(str(path()) if path().is_file() else None)
    team = {k: getattr(cfg.team, k) for k in cfg.team.__dataclass_fields__}
    models = {k: getattr(cfg.models, k) for k in cfg.models.__dataclass_fields__}
    app = {**APP_DEFAULTS, **(raw.get("app") or {})}
    accounts = [{"name": a.name, "vendor": a.vendor, "profile": a.profile} for a in cfg.accounts]
    seats = [{"name": s.name, "vendor": s.vendor, "account": s.account, "role": s.role} for s in cfg.seats]
    explicit_seats = bool(raw.get("seat"))
    return {"team": team, "models": models, "app": app, "accounts": accounts, "seats": seats,
            "explicit_seats": explicit_seats, "known_models": KNOWN_MODELS, "efforts": EFFORTS,
            "codex_efforts": CODEX_EFFORTS}


def save(update: dict) -> dict:
    """Merge a partial update ({team:{...}, models:{...}, app:{...}, accounts:[...]}) and write the file."""
    current = load()
    data = {
        "team": {**current["team"], **(update.get("team") or {})},
        "models": {**current["models"], **(update.get("models") or {})},
        "app": {**current["app"], **(update.get("app") or {})},
        "account": update.get("accounts", current["accounts"]),
    }
    if current["explicit_seats"] and "accounts" not in update:
        data["seat"] = current["seats"]
    # Validate before writing: the engine must be able to load what we save.
    text = dump(data)
    tmp = crew_home() / ".crew.toml.check"
    tmp.write_text(text, encoding="utf-8")
    try:
        cfgmod.load(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)
    if path().is_file():
        shutil.copyfile(path(), path().with_suffix(".toml.bak"))
    atomic_write(path(), text)
    return load()


# ------------------------------------------------------------------ TOML writer

_HEADERS = {
    "team": "How the team works",
    "models": "Which models may run (Haiku and Sonnet stay banned unless you remove them)",
    "app": "The app: voice, look, phone",
}


def _val(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_val(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)


def dump(data: dict) -> str:
    out = ["# Crew settings — written by the Crew app. Edit here or in the app's Settings screen.", ""]
    for section in ("team", "models", "app"):
        values = data.get(section) or {}
        out.append(f"# {_HEADERS[section]}")
        out.append(f"[{section}]")
        for k, v in values.items():
            if v is None:
                continue
            out.append(f"{k} = {_val(v)}")
        out.append("")
    for acc in data.get("account") or []:
        out.append("[[account]]")
        for k in ("name", "vendor", "profile"):
            if acc.get(k) not in (None, ""):
                out.append(f"{k} = {_val(acc[k])}")
        out.append("")
    for seat in data.get("seat") or []:
        out.append("[[seat]]")
        for k in ("name", "vendor", "account", "role"):
            out.append(f"{k} = {_val(seat[k])}")
        out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------ rules & keys


def rules() -> str:
    return team_rules()


def save_rules(text: str) -> None:
    atomic_write(crew_home() / "team_rules.md", text.rstrip() + "\n")


def secret_names() -> list[dict]:
    """Key names with a masked hint only; values never leave the machine through the app."""
    items = []
    for k, v in load_env_file(secrets_path()).items():
        items.append({"name": k, "hint": ("•" * 6) + v[-4:] if len(v) > 8 else "•" * 6})
    return items


def save_secret(name: str, value: str | None) -> None:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""):
        raise ValueError("Use letters, digits and underscores for the key name, e.g. OPENWEATHER_API_KEY")
    p = secrets_path()
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else [
        "# Your API keys. Written by the Crew app; values are hidden from every chat, log and report."]
    lines = [ln for ln in lines if not re.match(rf"^\s*(export\s+)?{re.escape(name)}\s*=", ln)]
    if value:
        lines.append(f"{name}={value}")
    atomic_write(p, "\n".join(lines) + "\n")
    if os.name != "nt":
        os.chmod(p, 0o600)
