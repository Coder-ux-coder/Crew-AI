"""Read and write the owner's settings (~/.crew/crew.toml), rules, and API keys.

The app is the settings editor, so the file is rewritten in a clean, commented
layout on every save (a backup of the previous version is kept).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import threading
import time
import tomllib
from pathlib import Path

from crewlib import config as cfgmod
from crewlib.prompts import team_rules
from crewlib.tiers import RETIRED
from crewlib.util import atomic_write, crew_home, load_env_file

# Models offered in the pickers. The owner can type any other name too; the ban list still applies.
KNOWN_MODELS = [
    {"id": "claude-opus-5-5", "label": "Opus 5.5", "note": "Best for everyday work", "engine": "claude"},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "note": "Fast and economical: the team's workhorse",
     "engine": "claude"},
    {"id": "claude-fable-5-1", "label": "Fable 5.1", "note": "Most capable, tighter limits", "engine": "claude"},
    {"id": "claude-opus-5", "label": "Opus 5", "note": "Previous Opus", "engine": "claude"},
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
    "codex_model": "gpt-6-astra",
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
    "auto_update": True,
    "improve_prompts": False,
    "settings_version": 4,
}


def path() -> Path:
    return crew_home() / "crew.toml"


def secrets_path() -> Path:
    return crew_home() / "secrets.env"


def _raw() -> dict:
    p = path()
    if not p.is_file():
        return {}
    return tomllib.loads(p.read_text(encoding="utf-8-sig"))  # -sig: a hand edit in Notepad starts with a byte-order mark


SETTINGS_VERSION = 4


def _sonnet_workhorse(raw: dict) -> None:
    """2.3.1, the owner's decision: Sonnet 5.5 replaces GPT-6 Sol as the team's workhorse, and GPT-6 Sol leaves Crew.
    Sonnet was banned by default until now, so the ban on it is lifted and it joins the allowed Claude models;
    GPT-6 Sol joins the ban list; ChatGPT chats that used it by default use GPT-6 Astra. Nothing else changes."""
    models = raw.get("models")
    if isinstance(models, dict):
        if models.pop("codex", None) is not None:  # the old ChatGPT workhorse: the workhorse is models.workhorse now
            models.setdefault("workhorse", "claude-sonnet-5-5")
        banned = models.get("banned")
        if isinstance(banned, list):
            banned = [b for b in banned if not (isinstance(b, str) and b.strip().lower() == "sonnet")]
            models["banned"] = banned + ([] if "gpt-6-sol" in banned else ["gpt-6-sol"])
        allowed = models.get("allowed")
        if isinstance(allowed, list) and allowed and "claude-sonnet-5-5" not in allowed:
            models["allowed"] = allowed + ["claude-sonnet-5-5"]
    app = raw.get("app")
    if isinstance(app, dict) and app.get("codex_model") in RETIRED:
        app["codex_model"] = RETIRED[app["codex_model"]]


# Each settings version: values an older Crew wrote as its defaults, and what they became. Only an exact old
# default is changed, and each step runs once, so a choice the owner made afterwards is never overwritten.
# A step can also be a function, for a change the owner asked for that is more than one value.
MIGRATIONS = {
    2: [("models", "effort_work", "high", "auto"), ("models", "effort_light", "medium", "auto"),
        ("models", "effort_ceo", "high", "max"), ("team", "max_hours", 3.0, 0.0),
        ("app", "chat_effort", "high", "auto"), ("app", "accent", "green", "clay")],
    # 2.1: the three-tier team (GPT-6 Sol workhorse, Opus 5.5 manager, GPT-6 Astra CEO); GPT-6 has no "minimal"
    3: [("models", "ceo", "claude-fable-5-1", "gpt-6-astra"), ("models", "codex", "", "gpt-6-sol"),
        ("app", "codex_model", "", "gpt-6-sol"), ("app", "codex_effort", "minimal", "low")],
    4: [_sonnet_workhorse],
}


def _migrate(raw: dict) -> dict:
    """Bring a settings file from an older Crew up to date without losing anything the owner chose."""
    have = int((raw.get("app") or {}).get("settings_version") or 1)
    if have >= SETTINGS_VERSION:
        return raw
    for version in sorted(MIGRATIONS):
        if version <= have:
            continue
        for step in MIGRATIONS[version]:
            if callable(step):
                step(raw)
                continue
            section, key, old, new = step
            sec = raw.get(section)
            if isinstance(sec, dict) and key in sec and sec[key] == old:
                sec[key] = new
    raw.setdefault("app", {})["settings_version"] = SETTINGS_VERSION
    return raw


_repair_lock = threading.Lock()
# What goes wrong when a settings file is edited by hand: a typing slip (TOML), a value Crew refuses (ConfigError),
# a section of the wrong kind (TypeError / AttributeError), text that is not UTF-8 (a ValueError too).
DAMAGE = (ValueError, TypeError, AttributeError)


def problem_path() -> Path:
    return crew_home() / "settings-problem.json"


def problem() -> dict | None:
    """The last time a settings file could not be read and was set aside (for the app to tell the owner)."""
    try:
        data = json.loads(problem_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("kept") else None


def _set_aside(error: Exception) -> bool:
    """crew.toml cannot be read: keep it under a dated name, put the last good copy (crew.toml.bak, written at
    every save) in its place if that one loads, else start from the defaults — and record what happened so
    the owner is told. Crew opens either way. False when nothing could be done (the file could not be moved)."""
    with _repair_lock:
        p = path()
        if not p.is_file():
            return True  # another request has just repaired it
        try:
            _load()
            return True  # likewise
        except DAMAGE:
            pass
        kept = p.with_name(f"crew.toml.damaged-{time.strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(p, kept)
        except OSError:
            return False
        backup = p.with_suffix(".toml.bak")
        restored = False
        if backup.is_file():
            try:
                shutil.copyfile(backup, p)
                _load()
                restored = True
            except (OSError, *DAMAGE):  # the last copy is no better: start from the defaults (it stays as .bak)
                p.unlink(missing_ok=True)
        info = {"at": time.time(), "error": str(error)[:300], "kept": kept.name,
                "restored": "the last good copy" if restored else "the defaults"}
        try:
            atomic_write(problem_path(), json.dumps(info))
        except OSError:
            pass
        print(f"settings: {p} could not be read ({error}); kept as {kept.name}, using {info['restored']}")
        return True


def load() -> dict:
    """Everything the Settings screen shows, with defaults filled in. A settings file that cannot be read never
    stops Crew: it is set aside (see _set_aside) and the last good settings are used."""
    try:
        return _load()
    except DAMAGE as exc:
        if not _set_aside(exc):
            raise
    return _load()


def _load() -> dict:
    raw = _raw()
    if raw and int((raw.get("app") or {}).get("settings_version") or 1) < SETTINGS_VERSION:
        migrated = _migrate(raw)
        text = dump({"team": migrated.get("team") or {}, "models": migrated.get("models") or {},
                     "app": migrated.get("app") or {}, "account": migrated.get("account") or [],
                     "seat": migrated.get("seat") or []})
        _check_readable(text)  # one the engine cannot read is set aside as the owner left it, not rewritten first
        try:
            shutil.copyfile(path(), path().with_suffix(".toml.bak"))  # the file as it was, like every save
            atomic_write(path(), text)
        except OSError:
            pass
        raw = _raw()
    cfg = cfgmod.load(str(path()) if path().is_file() else None)
    team = {k: getattr(cfg.team, k) for k in cfg.team.__dataclass_fields__}
    models = {k: getattr(cfg.models, k) for k in cfg.models.__dataclass_fields__}
    app = {**APP_DEFAULTS, **(raw.get("app") or {})}
    accounts = [{"name": a.name, "vendor": a.vendor, "profile": a.profile} for a in cfg.accounts]
    seats = [{"name": s.name, "vendor": s.vendor, "account": s.account, "role": s.role, "tier": s.tier}
             for s in cfg.seats]
    explicit_seats = bool(raw.get("seat"))
    return {"team": team, "models": models, "app": app, "accounts": accounts, "seats": seats,
            "explicit_seats": explicit_seats, "known_models": KNOWN_MODELS, "efforts": EFFORTS,
            "codex_efforts": CODEX_EFFORTS}


ACCOUNT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")


def _app_value_ok(key: str, value) -> bool:
    """An app setting keeps the kind of its default: a switch stays on or off ("false" as text would switch phone
    access on), a number stays a number, text stays text. A setting Crew does not list may be any single value."""
    finite = not (isinstance(value, float) and not math.isfinite(value))
    if key not in APP_DEFAULTS:
        return isinstance(value, (str, bool, int, float)) and finite
    default = APP_DEFAULTS[key]
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and finite
    return isinstance(value, str)


def _check_update(update, current: dict) -> None:
    """Refuse, in plain words, an update that is not shaped like settings (before anything is written)."""
    if not isinstance(update, dict):
        raise ValueError("Send the settings as a group of named values.")
    for section in ("team", "models", "app"):
        value = update.get(section)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"The {section} settings must be a group of named values.")
    for key, value in (update.get("app") or {}).items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(key)):
            raise ValueError(f"Unknown setting: {key}")
        if key == "settings_version" and value != current["app"].get(key):
            # Crew's own record of the file's format: set lower, the migrations would run again over the owner's
            # choices; set to text, the file could not be read at all.
            raise ValueError("settings_version is Crew's own record and cannot be changed.")
        if value is not None and not _app_value_ok(key, value):  # None: back to the default
            default = APP_DEFAULTS.get(key)
            kind = ("on or off" if isinstance(default, bool) else "a number" if isinstance(default, (int, float))
                    else "text" if key in APP_DEFAULTS else "a single value")
            raise ValueError(f"The setting {key} must be {kind}.")
    if "accounts" in update:
        accounts = update["accounts"]
        if not isinstance(accounts, list) or not all(isinstance(a, dict) for a in accounts):
            raise ValueError("The subscriptions must be a list, each with a name and a product.")
        known = {a["name"] for a in current["accounts"]}
        for acc in accounts:
            name = acc.get("name")
            if not isinstance(name, str) or not isinstance(acc.get("vendor"), str) or \
                    not isinstance(acc.get("profile", ""), (str, type(None))):
                raise ValueError("Each subscription needs a name and a product (Claude or ChatGPT).")
            if name not in known and (not ACCOUNT_NAME.fullmatch(name) or ".." in name):
                raise ValueError(f"“{name}” cannot be a subscription's name: use letters, digits, dots, dashes, "
                                 "@ or _ (for example claude-2 or work.max).")


def _check_readable(text: str) -> None:
    """Raise (a ValueError, in plain words) unless the engine can load these settings. Nothing is written."""
    tmp = crew_home() / f".crew.toml.check-{os.getpid()}-{threading.get_ident()}"  # its own: saves may overlap
    tmp.write_text(text, encoding="utf-8")
    try:
        cfgmod.load(str(tmp))
    finally:
        tmp.unlink(missing_ok=True)


_save_lock = threading.Lock()


def save(update: dict) -> dict:
    """Merge a partial update ({team:{...}, models:{...}, app:{...}, accounts:[...]}) and write the file. One at a
    time: two changes made at the same moment (the theme and the voice speed) must not undo each other."""
    with _save_lock:
        return _save(update)


def _save(update: dict) -> dict:
    current = load()
    _check_update(update, current)
    data = {
        "team": {**current["team"], **(update.get("team") or {})},
        "models": {**current["models"], **(update.get("models") or {})},
        "app": {**current["app"], **(update.get("app") or {})},
        "account": update.get("accounts", current["accounts"]),
    }
    if current["explicit_seats"] and "accounts" not in update:
        data["seat"] = current["seats"]
    text = dump(data)
    _check_readable(text)  # the engine must be able to load what we save
    if path().is_file():
        shutil.copyfile(path(), path().with_suffix(".toml.bak"))
    atomic_write(path(), text)
    return load()


# ------------------------------------------------------------------ TOML writer

_HEADERS = {
    "team": "How the team works",
    "models": "Which models run: Opus 5.5 manages, Sonnet 5.5 is the workhorse, GPT-6 Astra is the CEO",
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
        for k in ("name", "vendor", "account", "role", "tier"):
            if seat.get(k) not in (None, ""):
                out.append(f"{k} = {_val(seat[k])}")
        out.append("")
    return "\n".join(out)


# ------------------------------------------------------------------ rules & keys


def rules() -> str:
    return team_rules()


def save_rules(text: str) -> None:
    if not isinstance(text, str):
        raise ValueError("Write the team rules as text.")
    atomic_write(crew_home() / "team_rules.md", text.rstrip() + "\n")


def secret_names() -> list[dict]:
    """Key names with a masked hint only; values never leave the machine through the app."""
    items = []
    for k, v in load_env_file(secrets_path()).items():
        items.append({"name": k, "hint": ("•" * 6) + v[-4:] if len(v) > 8 else "•" * 6})
    return items


def save_secret(name: str, value: str | None) -> None:
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Use letters, digits and underscores for the key name, e.g. OPENWEATHER_API_KEY")
    if value is not None and not isinstance(value, str):
        raise ValueError("Paste the key as text.")
    value = (value or "").strip()  # a key copied with a line break or spaces around it
    if "\n" in value or "\r" in value:
        raise ValueError("Paste the key on its own: it should be one line, without line breaks.")
    p = secrets_path()
    lines = p.read_text(encoding="utf-8-sig").splitlines() if p.is_file() else [
        "# Your API keys. Written by the Crew app; values are hidden from every chat, log and report."]
    lines = [ln for ln in lines if not re.match(rf"^\s*(export\s+)?{re.escape(name)}\s*=", ln)]
    if value:
        lines.append(f"{name}={value}")
    atomic_write(p, "\n".join(lines) + "\n")
    if os.name != "nt":
        os.chmod(p, 0o600)
