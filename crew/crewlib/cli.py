"""Command line: `crew start "what you want"` and a few helpers.

Commands
  crew setup                      first-time setup and sign-in for each subscription
  crew start "request" [--repo PATH|URL] [--seats N]
  crew say "message"              talk to the running team
  crew status | chat | report     see what is happening
  crew stop | resume              pause and continue a run
  crew lessons [words]            what past teams learned
  crew doctor                     check the machine
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import config as config_mod
from . import gitops, lessons
from .store import Store
from .util import crew_home, hhmm, human_duration, load_env_file, now

CREW_DIR = Path(__file__).resolve().parent.parent
USE_COLOR = bool(sys.stdout and sys.stdout.isatty()) and os.environ.get("NO_COLOR") is None  # no console: no colour


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


# ---------------------------------------------------------------- run helpers


def runs_dir() -> Path:
    path = crew_home() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_run(name: str | None) -> Path:
    if name:
        path = runs_dir() / name
        if not path.is_dir():
            sys.exit(f"No run called {name}.")
        return path
    latest = runs_dir() / "LATEST"
    if not latest.is_file():
        sys.exit("No runs yet. Start one with: crew start \"what you want built\"")
    return runs_dir() / latest.read_text().strip()


def new_run_id(request: str) -> str:
    words = re.findall(r"[a-z0-9]+", request.lower())[:5]
    slug = "-".join(words)[:40] or "project"
    return time.strftime("%Y%m%d-%H%M%S") + "-" + slug


def print_message(m: dict) -> None:
    who = "Crew" if m["sender"] == "crew" else ("You" if m["sender"] == "you" else m["sender"])
    color = {"crew": "2", "you": "36"}.get(m["sender"], "1")
    kind = "" if m["kind"] in ("update", "human", "system") else c(f" [{m['kind']}]", "33")
    print(f"{c(hhmm(m['ts']), '2')} {c(who, color)}{kind}: {m['text']}", flush=True)


def follow_chat(store: Store, stop: threading.Event, after: int = 0) -> None:
    while not stop.is_set():
        for m in store.messages_after(after, 200):
            after = m["id"]
            print_message(m)
        stop.wait(1.0)


# ------------------------------------------------------------------ commands


def cmd_start(args) -> int:
    from .orchestrator import Orchestrator

    request = args.request
    if args.request_file:
        request = Path(args.request_file).read_text(encoding="utf-8")
    if not request or not request.strip():
        sys.exit("Tell the team what you want, e.g.  crew start \"a website for my bakery with an order form\"")
    cfg = config_mod.load(args.config, seats=args.seats)
    if args.mode:
        cfg.team.mode = args.mode
    chosen = [a.strip() for a in (args.accounts or "").split(",") if a.strip()]
    if chosen:
        cfg.restrict(chosen)
    run_id = args.run_id or new_run_id(request)
    run_dir = runs_dir() / run_id
    run_dir.mkdir(parents=True, exist_ok=bool(args.run_id))
    if args.repo and re.match(r"^(https?://|git@|ssh://)", args.repo):
        repo = gitops.clone(args.repo, run_dir / "repo")
    else:
        target = Path(args.repo).expanduser() if args.repo else crew_home() / "projects" / run_id
        repo = gitops.ensure_repo(target.resolve())
    (runs_dir() / "LATEST").write_text(run_id)
    return _run(cfg, run_dir, repo, request, run_id, resume=False, open_web=not args.no_web,
                headless=args.headless, max_hours=args.max_hours, head_to_head=args.head_to_head,
                accounts=chosen)


def _run(cfg, run_dir: Path, repo: Path, request: str, run_id: str, resume: bool, open_web: bool,
         headless: bool = False, max_hours: float | None = None, head_to_head: str | None = None,
         accounts: list[str] | None = None) -> int:
    from .orchestrator import Orchestrator

    orch = Orchestrator(cfg, run_dir, repo, request, run_id, resume=resume)
    if max_hours is not None and not resume:
        orch.store.set("max_hours", max(0.0, float(max_hours)))  # a timer for this project only
    if head_to_head and not resume:
        orch.store.set("head_to_head", head_to_head)  # this project only
    if accounts and not resume:
        orch.store.set("accounts_chosen", accounts)  # this project uses only these subscriptions
    server = None
    try:
        if headless:  # started by the Crew app, which shows the run itself
            raise OSError("headless")
        from .web import serve

        server = serve(run_dir, port=cfg.team.web_port)
        url = f"http://127.0.0.1:{cfg.team.web_port}"
        print(c(f"Live view: {url}", "36"))
        if open_web:
            try:
                webbrowser.open(url)
            except Exception:
                pass
    except OSError as exc:
        if not headless:
            print(c(f"(live view unavailable: {exc})", "2"))
    print(c(f"Team: {', '.join(s.name for s in cfg.seats)} · project folder: {repo}", "2"))
    stop = threading.Event()
    threading.Thread(target=follow_chat, args=(orch.store, stop, orch.store.last_message_id() if resume else 0),
                     daemon=True).start()
    try:
        phase = orch.run()
    finally:
        time.sleep(1.2)
        stop.set()
        if server:
            server.shutdown()
    report = run_dir / "REPORT.md"
    print()
    print(c(f"Run {phase}. Report: {report}", "1"))
    if report.is_file():
        print(report.read_text(encoding="utf-8"))
    return 0 if phase == "done" else 1


def cmd_resume(args) -> int:
    run_dir = resolve_run(args.run)
    store = Store(run_dir / "team.db")
    if store.get("phase") == "done":
        sys.exit("That run already finished.")
    store.set("stop_requested", None)
    cfg = config_mod.load(args.config)
    if store.get("accounts_chosen"):  # the project keeps the subscriptions the owner chose for it
        cfg.restrict([a for a in store.get("accounts_chosen") if any(x.name == a for x in cfg.accounts)])
    repo = Path(store.get("repo"))
    return _run(cfg, run_dir, repo, store.get("goal", ""), run_dir.name, resume=True, open_web=not args.no_web,
                headless=args.headless)


def cmd_say(args) -> int:
    store = Store(resolve_run(args.run) / "team.db")
    store.post("you", "human", " ".join(args.message), urgent=True)
    print("Sent to the team.")
    return 0


def cmd_stop(args) -> int:
    store = Store(resolve_run(args.run) / "team.db")
    store.set("stop_requested", now())
    print("The team will save its work and stop. Continue later with: crew resume")
    return 0


def cmd_status(args) -> int:
    run_dir = resolve_run(args.run)
    store = Store(run_dir / "team.db")
    from .web import FRIENDLY, PHASES

    brief = store.get("brief", {}) or {}
    print(c(brief.get("title") or run_dir.name, "1"), "·", PHASES.get(store.get("phase", ""), store.get("phase", "")),
          "·", human_duration(now() - (store.get("started_at") or now())))
    for s in store.seats():
        doing = f"task {s['current_task']}" if s["current_task"] else s["status"]
        print(f"  {s['name']:<10} {doing:<12} {s['note'] or ''}")
    for t in store.tasks():
        print(f"  • {t['title']} — {FRIENDLY.get(t['status'], t['status'])}")
    for a in store.accounts():
        util = "—" if a["util_5h"] is None else f"{a['util_5h'] * 100:.0f}%"
        print(f"  {a['name']:<10} {a['mode']:<9} {util}")
    return 0


def cmd_chat(args) -> int:
    store = Store(resolve_run(args.run) / "team.db")
    for m in store.messages_after(0, 100000):
        print_message(m)
    if args.follow:
        stop = threading.Event()
        try:
            follow_chat(store, stop, store.last_message_id())
        except KeyboardInterrupt:
            pass
    return 0


def cmd_report(args) -> int:
    report = resolve_run(args.run) / "REPORT.md"
    print(report.read_text(encoding="utf-8") if report.is_file() else "No report yet.")
    return 0


def cmd_lessons(args) -> int:
    items = lessons.search(" ".join(args.words), limit=args.max) if args.words else lessons.top(args.max)
    for x in items:
        print(f"- ({x['category']}, {x['weight']}×) {x['text']}")
    print(c(f"\nPlaybook: {lessons.write_playbook()}", "2"))
    return 0


def cmd_setup(args) -> int:
    home = crew_home()
    print(c("Setting up Crew in " + str(home), "1"))
    for name, target in (("crew.toml.example", "crew.toml"), ("secrets.env.example", "secrets.env")):
        src, dst = CREW_DIR / name, home / target
        if not dst.exists() and src.exists():
            shutil.copyfile(src, dst)
            print(f"  created {dst}")
    if os.name != "nt":
        os.chmod(home / "secrets.env", 0o600) if (home / "secrets.env").exists() else None
    from .prompts import team_rules

    team_rules()
    print(f"  your team rules: {home / 'team_rules.md'}")
    cfg = config_mod.load(args.config)
    for acc in cfg.accounts:
        prof = acc.profile_dir()
        tool = "claude" if acc.vendor == "claude" else "codex"
        if not shutil.which(tool):
            print(c(f"  ! {acc.name}: `{tool}` is not installed — see README", "33"))
            continue
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        if prof is not None:
            prof.mkdir(parents=True, exist_ok=True)
            env["CLAUDE_CONFIG_DIR" if acc.vendor == "claude" else "CODEX_HOME"] = str(prof)
        status_cmd = [tool, "auth", "status"] if tool == "claude" else [tool, "login", "status"]
        ok = subprocess.run(status_cmd, env=env, capture_output=True, text=True).returncode == 0
        if ok and not args.relogin:
            print(c(f"  ✓ {acc.name} is signed in", "32"))
            continue
        print(c(f"  → Sign in to {acc.name} ({acc.vendor}) in the browser window that opens…", "36"))
        login = [tool, "auth", "login"] if tool == "claude" else [tool, "login"]
        subprocess.run(login, env=env)
    print(c("\nReady. Try:  crew start \"a one-page website for my bakery\"", "1"))
    return 0


def cmd_doctor(args) -> int:
    ok = True
    print(f"Python {sys.version.split()[0]} ✓")
    for tool in ("git", "claude", "codex"):
        path = shutil.which(tool)
        if path:
            ver = subprocess.run([tool, "--version"], capture_output=True, text=True).stdout.strip().splitlines()
            print(f"{tool}: {ver[0] if ver else path} ✓")
        else:
            print(c(f"{tool}: not found", "33") + (" (needed)" if tool != "codex" else " (optional)"))
            ok = ok and tool == "codex"
    try:
        cfg = config_mod.load(args.config)
        print(f"Settings: {cfg.source or 'defaults'} ✓  seats: {', '.join(f'{s.name}({s.vendor})' for s in cfg.seats)}")
        print(f"Models: work={cfg.models.work} ceo={cfg.models.ceo} banned={cfg.models.banned}")
    except config_mod.ConfigError as exc:
        print(c(f"Settings problem: {exc}", "31"))
        ok = False
    secrets = load_env_file(crew_home() / "secrets.env")
    print(f"Secrets file: {len(secrets)} key(s) in {crew_home() / 'secrets.env'}")
    return 0 if ok else 1


def cmd_app(args) -> int:
    try:
        from crewapp.server import main as app_main
    except ImportError:
        sys.path.insert(0, str(CREW_DIR))
        from crewapp.server import main as app_main
    return app_main(port=args.port, phone=args.phone, open_window=not args.no_open)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crew", description="A team of AI agents that builds what you ask for.")
    ap.add_argument("--config", help="settings file (default: ./crew.toml or ~/.crew/crew.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="start a new project")
    p.add_argument("request", nargs="?", help="what you want, in your own words")
    p.add_argument("--request-file", help="read the request from a file")
    p.add_argument("--repo", help="existing project folder or git URL (default: a new folder)")
    p.add_argument("--seats", type=int, help="number of agents (default: one per subscription)")
    p.add_argument("--no-web", action="store_true", help="do not open the live view in a browser")
    p.add_argument("--headless", action="store_true", help="no live view at all (used by the Crew app)")
    p.add_argument("--run-id", help=argparse.SUPPRESS)
    p.add_argument("--mode", choices=("auto", "solo", "team"), help="solo builder or full team (default: auto)")
    p.add_argument("--max-hours", type=float, help="stop after this many hours (default: no time limit)")
    p.add_argument("--accounts", help="use only these subscriptions for this project (names, comma-separated)")
    p.add_argument("--head-to-head", choices=("off", "some", "all"),
                   help="build some or all parts with both the workhorse and the manager model; the better is kept")
    p.set_defaults(fn=cmd_start)

    p = sub.add_parser("resume", help="continue a stopped run")
    p.add_argument("run", nargs="?")
    p.add_argument("--no-web", action="store_true")
    p.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("app", help="open the Crew app (desktop and phone)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--phone", action="store_true", help="also allow your phone to connect (pairing required)")
    p.add_argument("--no-open", action="store_true", help="do not open a browser window")
    p.set_defaults(fn=cmd_app)

    p = sub.add_parser("say", help="send the team a message")
    p.add_argument("message", nargs="+")
    p.add_argument("--run")
    p.set_defaults(fn=cmd_say)

    for name, fn, text in (("stop", cmd_stop, "save the work and stop"), ("status", cmd_status, "who is doing what"),
                           ("report", cmd_report, "show the final report")):
        p = sub.add_parser(name, help=text)
        p.add_argument("run", nargs="?")
        p.set_defaults(fn=fn)

    p = sub.add_parser("chat", help="show the team chat")
    p.add_argument("run", nargs="?")
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(fn=cmd_chat)

    p = sub.add_parser("lessons", help="what past teams learned")
    p.add_argument("words", nargs="*")
    p.add_argument("--max", type=int, default=30)
    p.set_defaults(fn=cmd_lessons)

    p = sub.add_parser("setup", help="first-time setup and sign-in")
    p.add_argument("--relogin", action="store_true")
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("doctor", help="check this machine")
    p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except config_mod.ConfigError as exc:
        print(c(f"Settings problem: {exc}", "31"))
        return 2
