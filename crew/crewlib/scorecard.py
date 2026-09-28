"""The model scorecard: how each model has done on real work, and what Crew does with that evidence.

Every finished piece of work leaves one record in ~/.crew/memory.db (lessons.record_effort_outcome): the model
that built it, the kind and size of the work, the effort, whether it passed its first check, the time and the
tokens it took, and — for head-to-head tasks — whether it won. From those records:

  stats()       per model, and per model and kind of work: the first-check pass rate, typical minutes and
                tokens, head-to-head results, each with the number of tasks behind it;
  rules()       what Crew does about it: a kind of work the workhorse keeps failing moves up to the manager
                automatically; a kind it handles as well as the manager is suggested to the lead and the CEO
                (moving work down is left to their judgement, because quality comes first);
  route_tier()  the rule applied to one new task;
  render()      a short summary for the lead's and the CEO's instructions;
  summary()     everything the Scorecard screen shows.
"""

from __future__ import annotations

import statistics

from .lessons import _db
from .tiers import model_label
from .util import now

WINDOW_DAYS = 180    # older work says little about today's models
MIN_EVIDENCE = 4     # tasks before a figure counts as evidence
WEAK = 0.5           # below this first-check pass rate, a kind of work moves up to the manager
STRONG = 0.8         # at or above this, the model is strong at that kind of work
SIZE_ORDER = {"S": 0, "M": 1, "L": 2}
KIND_WORDS = {"build": "building", "fix": "fixing", "test": "tests", "docs": "documents", "research": "research",
              "verify": "checking", "foundation": "foundations"}


def _records(days: int = WINDOW_DAYS) -> list[dict]:
    db = _db()
    try:
        rows = [dict(r) for r in db.execute(
            "SELECT ts, project, kind, size, effort, rounds, first_pass, minutes, tokens, COALESCE(tier,'manager') "
            "AS tier, COALESCE(model,'') AS model, COALESCE(outcome,'merged') AS outcome, "
            "COALESCE(handovers,0) AS handovers FROM effort_outcomes WHERE ts >= ?", (now() - days * 86400,))]
    finally:
        db.close()
    return rows


def _contests(days: int = WINDOW_DAYS, limit: int = 1000) -> list[dict]:
    db = _db()
    try:
        return [dict(r) for r in db.execute(
            "SELECT * FROM contests WHERE ts >= ? ORDER BY id DESC LIMIT ?", (now() - days * 86400, limit))]
    finally:
        db.close()


def label(rate: float | None, n: int) -> str:
    if n < MIN_EVIDENCE or rate is None:
        return "thin"
    return "strong" if rate >= STRONG else "weak" if rate < WEAK else "fair"


def _cell(rows: list[dict]) -> dict:
    n = len(rows)
    passed = sum(1 for r in rows if r["first_pass"])
    rate = passed / n if n else None
    return {"n": n, "passed": passed, "rate": None if rate is None else round(rate, 3), "label": label(rate, n),
            "minutes": round(statistics.median(r["minutes"] for r in rows), 1) if n else None,
            "tokens": int(statistics.median(r["tokens"] for r in rows)) if n else None}


def stats(days: int = WINDOW_DAYS) -> dict:
    """Per model and per (model, kind, size): pass rate, typical minutes and tokens, head-to-head results."""
    rows = [r for r in _records(days) if r["model"]]
    contests = _contests(days)
    models: dict[str, dict] = {}
    for model in sorted({r["model"] for r in rows} | {c["winner_model"] for c in contests}
                        | {c["loser_model"] for c in contests}):
        mine = [r for r in rows if r["model"] == model]
        tiers = [r["tier"] for r in mine] or [c["winner_tier"] for c in contests if c["winner_model"] == model] \
            or [c["loser_tier"] for c in contests if c["loser_model"] == model] or ["manager"]
        by_kind: dict[str, dict] = {}
        for kind, size in sorted({(r["kind"], r["size"]) for r in mine},
                                 key=lambda ks: (ks[0], SIZE_ORDER.get(ks[1], 9))):
            by_kind[f"{kind} {size}"] = {"kind": kind, "size": size,
                                         **_cell([r for r in mine if (r["kind"], r["size"]) == (kind, size)])}
        by_size = {}
        for size in ("S", "M", "L"):
            sized = [r for r in mine if r["size"] == size]
            if sized:
                by_size[size] = {"n": len(sized), "minutes": round(statistics.median(r["minutes"] for r in sized), 1),
                                 "tokens": int(statistics.median(r["tokens"] for r in sized))}
        overall = _cell(mine)
        overall["verdict"] = overall.pop("label")  # the model's name is its label; its overall rating is the verdict
        models[model] = {
            "model": model, "label": model_label(model), "tier": max(set(tiers), key=tiers.count), **overall,
            "by_size": by_size, "by_kind": by_kind,
            "strong": [k for k, c in by_kind.items() if c["label"] == "strong"],
            "weak": [k for k, c in by_kind.items() if c["label"] == "weak"],
            "moved_up": sum(1 for r in mine if r["outcome"] == "moved-up"),
            "handovers": sum(int(r["handovers"] or 0) for r in mine),
            "wins": sum(1 for c in contests if c["winner_model"] == model),
            "losses": sum(1 for c in contests if c["loser_model"] == model),
        }
    return {"models": models, "records": len(rows), "projects": len({r["project"] for r in rows if r["project"]}),
            "contests": contests}


def rules(workhorse: str, manager: str, days: int = WINDOW_DAYS) -> dict:
    """Routing from the record. Moving work up to the manager is automatic (it protects quality); moving work
    down to the workhorse is only suggested (it saves tokens, so it is the lead's and the CEO's call)."""
    data = stats(days)["models"]
    wh, mg = data.get(workhorse) or {}, data.get(manager) or {}
    up, down = [], []
    for key, cell in (wh.get("by_kind") or {}).items():
        if cell["label"] == "weak":
            up.append({"kind": cell["kind"], "size": cell["size"], "rate": cell["rate"], "n": cell["n"],
                       "why": f"{model_label(workhorse)} passed the first check {cell['passed']} of {cell['n']} times"})
    for key, cell in (mg.get("by_kind") or {}).items():
        theirs = (wh.get("by_kind") or {}).get(key)
        if theirs and theirs["n"] >= 3 and (theirs["rate"] or 0) >= STRONG and cell["n"] >= MIN_EVIDENCE \
                and (theirs["rate"] or 0) >= (cell["rate"] or 0) - 0.05:
            down.append({"kind": cell["kind"], "size": cell["size"],
                         "why": f"{model_label(workhorse)} passed {theirs['passed']} of {theirs['n']}, "
                                f"{model_label(manager)} {cell['passed']} of {cell['n']}"})
    # Head-to-head wins count as evidence for suggestions too (they compare the same task directly).
    for c in _grouped_contests(workhorse, manager, days):
        if c["workhorse_wins"] >= 2 and c["workhorse_wins"] >= 2 * c["manager_wins"] \
                and not any((d["kind"], d["size"]) == (c["kind"], c["size"]) for d in down):
            down.append({"kind": c["kind"], "size": c["size"],
                         "why": f"{model_label(workhorse)} won {c['workhorse_wins']} of "
                                f"{c['workhorse_wins'] + c['manager_wins']} head-to-heads"})
    return {"up": up, "down": down}


def _grouped_contests(workhorse: str, manager: str, days: int) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    for c in _contests(days):
        g = groups.setdefault((c["kind"], c["size"]), {"kind": c["kind"], "size": c["size"], "workhorse_wins": 0,
                                                        "manager_wins": 0})
        if c["winner_model"] == workhorse and c["loser_model"] == manager:
            g["workhorse_wins"] += 1
        elif c["winner_model"] == manager and c["loser_model"] == workhorse:
            g["manager_wins"] += 1
    return list(groups.values())


def route_tier(kind: str, size: str, tier: str, workhorse: str, manager: str) -> tuple[str, str]:
    """The tier a new task should get: a workhorse task of a kind the workhorse keeps failing moves up.
    Returns (tier, reason) — reason is empty when nothing changed."""
    if tier != "workhorse" or not workhorse or not manager:
        return tier, ""
    for rule in rules(workhorse, manager)["up"]:
        if (rule["kind"], rule["size"]) == (kind, size):
            return "manager", f"the scorecard moves {KIND_WORDS.get(kind, kind)} ({size}) up: {rule['why']}"
    return tier, ""


def _pct(rate: float | None) -> str:
    return "–" if rate is None else f"{round(100 * rate)}%"


def profile(model: str, data: dict | None = None, limit: int = 3) -> str:
    """One model's measured record in a line, for every agent's roster ('' before there is a record)."""
    m = ((data or stats())["models"]).get(model)
    if not m or not m["n"]:
        return ""
    words = lambda keys: ", ".join(f"{KIND_WORDS.get(m['by_kind'][k]['kind'], m['by_kind'][k]['kind'])} "  # noqa: E731
                                   f"({m['by_kind'][k]['size']})" for k in keys)
    strong = sorted(m["strong"], key=lambda k: -m["by_kind"][k]["n"])[:limit]
    weak = sorted(m["weak"], key=lambda k: -m["by_kind"][k]["n"])[:limit]
    parts = [f"{_pct(m['rate'])} of {m['n']} pieces passed the first check"]
    if strong:
        parts.append("strong at " + words(strong))
    if weak:
        parts.append("weak at " + words(weak))
    if m.get("wins") or m.get("losses"):
        parts.append(f"head-to-head {m['wins']} won, {m['losses']} lost")
    return "; ".join(parts)


def render(workhorse: str, manager: str, limit: int = 4) -> str:
    """A short, factual summary for the lead's and the CEO's instructions ('' when there is no record yet)."""
    data = stats()
    if not data["records"] and not data["contests"]:
        return ""
    lines = [f"Model scorecard (the last {WINDOW_DAYS} days: how often each model's work passed its first check; "
             "tasks in brackets):"]
    for model in (workhorse, manager):
        m = data["models"].get(model)
        if not m:
            continue
        tier = "workhorse" if model == workhorse else "manager"
        parts = [f"- {m['label']} ({tier}): {_pct(m['rate'])} overall ({m['n']})"]
        strong = sorted(m["strong"], key=lambda k: -m["by_kind"][k]["n"])[:limit]
        weak = sorted(m["weak"], key=lambda k: -m["by_kind"][k]["n"])[:limit]
        if strong:
            parts.append("strong at " + ", ".join(f"{k} {_pct(m['by_kind'][k]['rate'])} ({m['by_kind'][k]['n']})"
                                                  for k in strong))
        if weak:
            parts.append("weak at " + ", ".join(f"{k} {_pct(m['by_kind'][k]['rate'])} ({m['by_kind'][k]['n']})"
                                                for k in weak))
        lines.append("; ".join(parts) + ".")
    wh, mg = data["models"].get(workhorse) or {}, data["models"].get(manager) or {}
    if wh.get("wins") or mg.get("wins"):
        lines.append(f"- Head-to-head: {model_label(workhorse)} won {wh.get('wins', 0)}, "
                     f"{model_label(manager)} won {mg.get('wins', 0)}.")
    r = rules(workhorse, manager)
    if r["up"]:
        lines.append("Automatic rule: these kinds of work go to the manager tier — " + "; ".join(
            f"{u['kind']} {u['size']} ({u['why']})" for u in r["up"]) + ".")
    if r["down"]:
        lines.append("Evidence suggests the workhorse handles these as well as the manager (your call): " + "; ".join(
            f"{d['kind']} {d['size']} ({d['why']})" for d in r["down"]) + ".")
    return "\n".join(lines)


def summary(workhorse: str, manager: str) -> dict:
    """Everything the Scorecard screen shows."""
    data = stats()
    models = sorted(data["models"].values(), key=lambda m: ({"workhorse": 0, "manager": 1}.get(m["tier"], 2), -m["n"]))
    rows = sorted({k for m in models for k in m["by_kind"]},
                  key=lambda k: (k.split(" ")[0], SIZE_ORDER.get(k.split(" ")[-1], 9)))
    matrix = []
    for key in rows:
        kind, size = key.split(" ")
        cells = {m["model"]: m["by_kind"].get(key) for m in models}
        judged = [(c["rate"], m) for m, c in cells.items() if c and c["label"] != "thin"]
        best = max(judged)[1] if len(judged) >= 2 and max(judged)[0] > min(judged)[0] else None
        matrix.append({"kind": kind, "size": size, "cells": cells, "best": best})
    recent = [{"at": c["ts"], "project": c["project"], "task": c["task"], "kind": c["kind"], "size": c["size"],
               "winner": model_label(c["winner_model"]), "loser": model_label(c["loser_model"]),
               "winner_tier": c["winner_tier"], "both_passed": bool(c["winner_passed"] and c["loser_passed"]),
               "reason": c["reason"]} for c in data["contests"][:12]]
    return {"window_days": WINDOW_DAYS, "records": data["records"], "projects": data["projects"],
            "thresholds": {"evidence": MIN_EVIDENCE, "weak": WEAK, "strong": STRONG},
            "workhorse": workhorse, "manager": manager, "models": models, "matrix": matrix,
            "rules": rules(workhorse, manager), "contests": recent}
