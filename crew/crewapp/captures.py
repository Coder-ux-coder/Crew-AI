"""Screenshots and screen recordings the owner (or an agent) captured."""

from __future__ import annotations

import re
import time
from pathlib import Path

from crewlib.util import crew_home

TYPES = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webm": "video", ".mp4": "video"}


def folder() -> Path:
    path = crew_home() / "captures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save(data: bytes, ext: str, label: str = "capture") -> dict:
    ext = ext.lower() if ext.startswith(".") else "." + ext.lower()
    if ext not in TYPES:
        raise ValueError(f"unsupported capture type {ext}")
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "capture"
    name = time.strftime("%Y%m%d-%H%M%S") + f"-{slug}{ext}"
    path = folder() / name
    n = 1
    while path.exists():
        path = folder() / f"{path.stem}-{n}{ext}"
        n += 1
    path.write_bytes(data)
    return info(path)


def info(path: Path) -> dict:
    st = path.stat()
    return {"name": path.name, "kind": TYPES.get(path.suffix.lower(), "file"), "size": st.st_size,
            "created": st.st_mtime, "url": f"/captures/{path.name}"}


def list_all() -> list[dict]:
    items = [info(p) for p in folder().iterdir() if p.is_file() and p.suffix.lower() in TYPES]
    return sorted(items, key=lambda x: x["created"], reverse=True)


def resolve(name: str) -> Path | None:
    # A capture's name is about 60 letters; a far longer one is not a capture (and too long a name for the computer).
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", name) or name.strip(".") == "":
        return None
    path = folder() / name
    return path if path.is_file() and path.suffix.lower() in TYPES else None  # a picture or a recording, nothing else


def delete(name: str) -> bool:
    path = resolve(name)
    if path:
        path.unlink()
        return True
    return False
