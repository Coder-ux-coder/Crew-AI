"""Entry point: `python -m crewlib ...`.

Started from the desktop icon (pythonw.exe on Windows) there is no console: nothing
printed would be seen, and a failure would be silent. So in that case every message goes
to ~/.crew/app.log, and a failure to start is shown in a message box.
"""

import os
import sys
import time
from pathlib import Path

WINDOWLESS = sys.stdout is None or sys.stderr is None
LOG = None
LOG_LIMIT = 5_000_000  # bytes: a larger log starts again, and the last one is kept as app.log.1


def open_log(path: Path):
    """Crew's log, for appending; one that has grown large starts afresh (the last one is kept as app.log.1).
    The same as util.open_log, kept here because this runs before anything else is imported."""
    try:
        if path.stat().st_size > LOG_LIMIT:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:  # not there yet, or held by another Crew window (Windows): carry on appending
        pass
    return open(path, "a", encoding="utf-8", buffering=1)


if WINDOWLESS:
    for folder in (Path(os.environ.get("CREW_HOME") or Path.home() / ".crew"), Path(os.environ.get("TEMP") or "/tmp")):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            LOG = folder / "app.log"
            stream = open_log(LOG)
            break
        except OSError:
            continue
    else:
        stream = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream
    print(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} Crew starting: {' '.join(sys.argv[1:])} "
          f"(Python {sys.version.split()[0]})")


def _tell_owner(error: BaseException) -> None:
    if not (WINDOWLESS and os.name == "nt"):
        return
    try:
        import ctypes

        where = f"\n\nDetails were saved in:\n{LOG}" if LOG else ""
        ctypes.windll.user32.MessageBoxW(
            None, f"Crew could not start.\n\n{type(error).__name__}: {error}{where}\n\n"
                  "Please send a screenshot of this message.", "Crew", 0x10)
    except Exception:
        pass


try:
    from .cli import main

    code = main()
except SystemExit:
    raise
except KeyboardInterrupt:
    code = 130
except BaseException as exc:  # never fail silently
    import traceback

    traceback.print_exc()
    _tell_owner(exc)
    code = 1
sys.exit(code)
