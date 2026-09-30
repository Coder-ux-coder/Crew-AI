"""Your connections: MCP servers (tools from other services: search, CRM, e-mail, Notion …) that the
assistant and the team may use. API keys are kept separately in secrets.env.

Stored in ~/.crew/connections.json:
  {"mcp": {"notion": {"type": "http", "url": "...", "headers": {...}, "enabled": true}, ...}}
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

from .util import atomic_write, crew_home

KINDS = ("http", "sse", "stdio")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")


def _path() -> Path:
    return crew_home() / "connections.json"


def _read() -> tuple[dict, bool]:
    """(the connections, whether the file on disk was readable). A missing file is fine; a damaged one is not."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"mcp": {}}, True
    except (OSError, ValueError):
        return {"mcp": {}}, False
    if not isinstance(data, dict):
        return {"mcp": {}}, False
    servers = data.get("mcp")
    data["mcp"] = {str(k): v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}
    return data, isinstance(servers, dict) or servers is None


def load() -> dict:
    return _read()[0]


def _save(data: dict) -> None:
    if _path().is_file() and not _read()[1]:
        # The file cannot be read (damaged, or edited by hand): keep it, under a dated name, before writing anew.
        try:
            shutil.copyfile(_path(), _path().with_name(f"connections.json.damaged-{time.strftime('%Y%m%d-%H%M%S')}"))
        except OSError:
            pass
    atomic_write(_path(), json.dumps(data, indent=1, ensure_ascii=False))
    if os.name != "nt":
        os.chmod(_path(), 0o600)


def _mask(value: str) -> str:
    value = str(value)
    return value if len(value) < 12 else value[:6] + "•" * 6 + value[-4:]


def listing() -> list[dict]:
    """For the Connections screen: no secret values, only hints."""
    out = []
    for name, srv in sorted(load()["mcp"].items()):
        args = srv.get("args") if isinstance(srv.get("args"), list) else []
        headers = srv.get("headers") if isinstance(srv.get("headers"), dict) else {}
        env = srv.get("env") if isinstance(srv.get("env"), dict) else {}
        out.append({"name": name, "type": srv.get("type", "stdio"), "enabled": srv.get("enabled", True),
                    "url": str(srv.get("url") or ""),
                    "command": " ".join([str(srv.get("command") or ""), *map(str, args)]).strip(),
                    "headers": {str(k): _mask(v) for k, v in headers.items()},
                    "env": sorted(str(k) for k in env), "source": srv.get("source", "you")})
    return out


_ARG = re.compile(r"""(?:"[^"]*"?|'[^']*'?|[^\s"'])+""")
_QUOTED = re.compile(r""""([^"]*)"?|'([^']*)'?""")


def split_args(text: str) -> list[str]:
    """A program's arguments typed on one line: separated by spaces, a part in quotes kept whole ("C:\\Users\\Sam
    Lee\\Documents", as the guides show a folder with a space in it). Backslashes stay as they are: on Windows they
    separate folders."""
    return [_QUOTED.sub(lambda m: m.group(1) if m.group(1) is not None else m.group(2), part)
            for part in _ARG.findall(text)]


def add_mcp(name: str, kind: str, url: str = "", headers: dict | None = None, command: str = "",
            args: list[str] | None = None, env: dict | None = None, source: str = "you") -> dict:
    if not all(isinstance(x, str) for x in (name or "", kind or "", url or "", command or "")) \
            or not isinstance(headers or {}, dict) or not isinstance(env or {}, dict) \
            or not isinstance(args or [], list):
        raise ValueError("Fill in the connection's name, and its web address or program, as text.")
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("Give the connection a short name: letters, digits, - or _.")
    if kind not in KINDS:
        raise ValueError("Choose a web address (http) or a program (stdio).")
    srv: dict = {"type": kind, "enabled": True, "source": source}
    if kind in ("http", "sse"):
        if not re.match(r"^https?://", url or ""):
            raise ValueError("The address must start with https:// (or http:// for this computer).")
        srv["url"] = url.strip()
        if headers:
            srv["headers"] = {str(k).strip(): str(v).strip() for k, v in headers.items() if str(k).strip()}
    else:
        if not (command or "").strip():
            raise ValueError("Say which program runs this connection (for example npx).")
        srv["command"] = command.strip()
        srv["args"] = [str(a) for a in (args or [])]
        if env:
            srv["env"] = {str(k): str(v) for k, v in env.items()}
    data = load()
    data["mcp"][name] = srv
    _save(data)
    return srv


def remove_mcp(name: str) -> bool:
    data = load()
    if data["mcp"].pop(name, None) is None:
        return False
    _save(data)
    return True


def set_enabled(name: str, enabled: bool) -> bool:
    data = load()
    if name not in data["mcp"]:
        return False
    data["mcp"][name]["enabled"] = bool(enabled)
    _save(data)
    return True


def mcp_servers() -> dict:
    """Switched-on servers in Claude Code's --mcp-config format."""
    out = {}
    for name, srv in load()["mcp"].items():
        if not srv.get("enabled", True):
            continue
        if srv.get("type") in ("http", "sse"):
            if not srv.get("url"):
                continue  # an entry without its address (a hand edit) cannot be used
            entry = {"type": srv["type"], "url": str(srv["url"])}
            if isinstance(srv.get("headers"), dict) and srv["headers"]:
                entry["headers"] = {str(k): str(v) for k, v in srv["headers"].items()}
        else:
            if not srv.get("command"):
                continue
            args = srv.get("args") if isinstance(srv.get("args"), list) else []
            entry = {"type": "stdio", "command": str(srv["command"]), "args": [str(a) for a in args]}
            if isinstance(srv.get("env"), dict) and srv["env"]:
                entry["env"] = {str(k): str(v) for k, v in srv["env"].items()}
        out[name] = entry
    return out


def claude_desktop_config() -> Path | None:
    """Where the Claude desktop app keeps its connectors on this computer."""
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(home / "AppData" / "Roaming")))
        candidates = [base / "Claude" / "claude_desktop_config.json"]
    elif sys.platform == "darwin":
        candidates = [home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"]
    else:
        candidates = [home / ".config" / "Claude" / "claude_desktop_config.json"]
    return next((p for p in candidates if p.is_file()), None)


def import_claude_desktop() -> list[str]:
    """Copy the Claude desktop app's MCP servers into Crew (existing names are left alone)."""
    path = claude_desktop_config()
    if path is None:
        return []
    try:
        servers = (json.loads(path.read_text(encoding="utf-8")) or {}).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(servers, dict):
        return []
    data = load()
    added = []
    for name, srv in servers.items():
        clean = re.sub(r"[^A-Za-z0-9_-]+", "-", str(name)).strip("-")[:40] or "server"
        if clean in data["mcp"] or not isinstance(srv, dict):
            continue
        headers = srv.get("headers") if isinstance(srv.get("headers"), dict) else {}
        env = srv.get("env") if isinstance(srv.get("env"), dict) else {}
        args = srv.get("args") if isinstance(srv.get("args"), list) else []  # a hand edit may hold anything
        if srv.get("url"):
            entry = {"type": "sse" if srv.get("type") == "sse" else "http", "url": str(srv["url"]),
                     "headers": {str(k): str(v) for k, v in headers.items()}}
        elif srv.get("command"):
            entry = {"type": "stdio", "command": str(srv["command"]), "args": [str(a) for a in args],
                     "env": {str(k): str(v) for k, v in env.items()}}
        else:
            continue
        entry.update(enabled=True, source="Claude app")
        data["mcp"][clean] = entry
        added.append(clean)
    if added:
        _save(data)
    return added


# Friendly names for API keys the owner may add (any other name works too).
KEY_PRESETS = [
    {"name": "HUNTER_API_KEY", "label": "Hunter.io (find and verify e-mail addresses)"},
    {"name": "OPENAI_API_KEY", "label": "OpenAI API"},
    {"name": "GOOGLE_API_KEY", "label": "Google (Maps, Places, Search …)"},
    {"name": "SERPAPI_API_KEY", "label": "SerpApi (search results)"},
    {"name": "BRAVE_API_KEY", "label": "Brave Search"},
    {"name": "GITHUB_TOKEN", "label": "GitHub"},
    {"name": "NOTION_API_KEY", "label": "Notion"},
    {"name": "AIRTABLE_API_KEY", "label": "Airtable"},
    {"name": "SENDGRID_API_KEY", "label": "SendGrid (e-mail)"},
    {"name": "TWILIO_AUTH_TOKEN", "label": "Twilio (SMS)"},
    {"name": "APOLLO_API_KEY", "label": "Apollo.io"},
    {"name": "ELEVENLABS_API_KEY", "label": "ElevenLabs (voices)"},
]


def key_label(name: str) -> str:
    return next((p["label"] for p in KEY_PRESETS if p["name"] == name), "")
