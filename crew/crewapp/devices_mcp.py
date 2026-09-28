"""MCP tools that let an agent use the app's browser and the owner's phone.

Launched by Claude Code (stdio). Every action goes through the running Crew app,
so the owner sees it happen live in the side panel.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

APP = os.environ.get("CREW_APP_URL", "").rstrip("/")
TOKEN = os.environ.get("CREW_APP_TOKEN", "")

S, N, B = {"type": "string"}, {"type": "number"}, {"type": "boolean"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


TOOLS = [
    ("browser_open", "Open a web address (or search words) in the app's browser. The owner watches it live.",
     _obj({"url": S}, ["url"])),
    ("browser_read", "Read the current page: its text, visible links and form fields.", _obj({})),
    ("browser_screenshot", "See the current page as a picture.", _obj({"full_page": B})),
    ("browser_click", "Click something: give its visible text, or a position as fractions of the page width and "
                      "height (x and y between 0 and 1).", _obj({"text": S, "x": N, "y": N})),
    ("browser_fill", "Type into a form field found by its label, placeholder or name.",
     _obj({"field": S, "text": S}, ["field", "text"])),
    ("browser_type", "Type text where the cursor is; submit=true presses Enter after.", _obj({"text": S, "submit": B},
                                                                                             ["text"])),
    ("browser_press", "Press a key: enter, tab, escape, backspace, up, down, pagedown …", _obj({"key": S}, ["key"])),
    ("browser_scroll", "Scroll the page up or down by screens.", _obj({"direction": S, "screens": N}, ["direction"])),
    ("browser_back", "Go back to the previous page.", _obj({})),
    ("browser_device", "Show pages as a desktop or as a phone (for checking mobile layouts).",
     _obj({"device": {"type": "string", "enum": ["desktop", "phone"]}}, ["device"])),
    ("computer_screenshot", "See the owner's Windows screen as a picture. Take one before acting and after each "
                            "step. Positions for the other computer tools are pixels of the latest picture.", _obj({})),
    ("computer_click", "Click on the Windows screen at x, y (pixels of the latest screenshot). button: left, right or "
                       "middle; double=true for a double-click.",
     _obj({"x": N, "y": N, "button": {"type": "string", "enum": ["left", "right", "middle"]}, "double": B}, ["x", "y"])),
    ("computer_drag", "Drag with the mouse from x1, y1 to x2, y2 (pixels of the latest screenshot).",
     _obj({"x1": N, "y1": N, "x2": N, "y2": N}, ["x1", "y1", "x2", "y2"])),
    ("computer_type", "Type text where the cursor is on the computer. Any language. New lines press "
                      "Enter.", _obj({"text": S}, ["text"])),
    ("computer_key", "Press a key or a combination on the computer: enter, esc, tab, ctrl+s, alt+tab, win+r, f5, "
                     "ctrl+shift+esc …", _obj({"keys": S}, ["keys"])),
    ("computer_scroll", "Scroll where the mouse pointer is: direction up or down, amount in wheel steps (default 3).",
     _obj({"direction": {"type": "string", "enum": ["up", "down"]}, "amount": N}, ["direction"])),
    ("computer_open", "Open something on the computer: an app by name (excel, word, notepad, calculator…), a file, a "
                      "folder, or a web address.", _obj({"target": S}, ["target"])),
    ("phone_status", "Is the owner's Android phone connected?", _obj({})),
    ("phone_screenshot", "See the phone's screen as a picture.", _obj({})),
    ("phone_screen", "List what is on the phone's screen (text, buttons) with tap positions.", _obj({})),
    ("phone_tap", "Tap on the phone: give the visible text of a button/item, or x and y as fractions of the screen.",
     _obj({"text": S, "x": N, "y": N})),
    ("phone_swipe", "Swipe on the phone: up, down, left or right.", _obj({"direction": S}, ["direction"])),
    ("phone_type", "Type English text into the focused field on the phone.", _obj({"text": S}, ["text"])),
    ("phone_key", "Press a phone key: home, back, recents, enter, delete, power, volume_up, volume_down.",
     _obj({"key": S}, ["key"])),
    ("phone_open_app", "Open an app on the phone by name (for example WhatsApp, Chrome, Settings).",
     _obj({"name": S}, ["name"])),
    ("todo_write", "Your to-do list for multi-step work, shown live to the owner. Send the whole list each time, with "
                   "each item's status: pending, in_progress (one at a time) or completed. Use it for any task with "
                   "three or more steps.",
     _obj({"todos": {"type": "array", "items": _obj({"content": S, "status": {"type": "string", "enum": [
         "pending", "in_progress", "completed"]}}, ["content", "status"])}}, ["todos"])),
]


def call_app(name: str, args: dict) -> dict:
    group, _, action = name.partition("_")
    req = urllib.request.Request(f"{APP}/internal/{group}/{action}", data=json.dumps(args).encode(),
                                 headers={"Content-Type": "application/json", "X-Crew-Token": TOKEN}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except ValueError:
            return {"error": f"The app refused the request ({exc.code})."}
    except (urllib.error.URLError, OSError):
        return {"error": "The Crew app is not running, so the browser and phone are unavailable."}


def handle(name: str, args: dict) -> tuple[list[dict], bool]:
    if name == "todo_write":  # the app shows the list from the call itself; nothing to do here
        items = args.get("todos") or []
        done = sum(1 for x in items if isinstance(x, dict) and x.get("status") == "completed")
        return [{"type": "text", "text": f"To-do list updated ({done} of {len(items)} done)."}], False
    if not APP:
        return [{"type": "text", "text": "The browser and phone tools need the Crew app to be open."}], True
    res = call_app(name, args)
    if res.get("error"):
        return [{"type": "text", "text": res["error"]}], True
    if res.get("image"):
        content = [{"type": "image", "data": res["image"], "mimeType": res.get("mime", "image/jpeg")}]
        if res.get("text"):
            content.append({"type": "text", "text": res["text"]})
        return content, False
    return [{"type": "text", "text": json.dumps(res.get("result", res), ensure_ascii=False)[:20000]}], False


def main() -> None:
    from crewlib.util import utf8_stdio

    utf8_stdio()  # Codex starts this without PYTHONUTF8: on Windows, non-English text for computer_type arrived garbled
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if not isinstance(msg, dict):
            continue
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "crew-devices", "version": "1.0"},
                      "instructions": "The owner's browser (in the Crew app), Windows computer and Android phone. "
                                            "The owner watches live and can stop computer control by moving "
                                            "the mouse pointer into the top-left corner."}
        elif method == "tools/list":
            result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
        elif method == "tools/call":
            try:
                content, is_error = handle(params.get("name", ""), params.get("arguments") or {})
            except Exception as exc:  # never crash the agent's tool server
                content, is_error = [{"type": "text", "text": f"Tool error: {exc}"}], True
            result = {"content": content, "isError": is_error}
        elif method == "ping":
            result = {}
        else:
            if mid is not None and not str(method).startswith("notifications/"):
                sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                             "error": {"code": -32601, "message": "method not found"}}) + "\n")
                sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
