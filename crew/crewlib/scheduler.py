"""Usage-aware scheduling: keep every subscription busy, drain them at a similar
pace, never start work an account is unlikely to finish, and use quota that
would otherwise be lost at the next reset.
"""

from __future__ import annotations

from .tiers import seat_tier
from .util import now

FIVE_H = 5 * 3600
SIZE_UNITS = {"S": 1, "M": 3, "L": 8}
LIGHT_KINDS = ("verify", "docs", "test", "research")


def apply_rate(store, account: str, info: dict) -> dict:
    """Fold a rate-limit report (Claude's rate_limit_event or Codex's snapshot) into the account row."""
    fields: dict = {}
    windows = info.get("unifiedWindows") or {}
    five, week = windows.get("five_hour"), windows.get("seven_day")
    if five:
        fields.update(util_5h=float(five.get("utilization") or 0), reset_5h=int(five.get("resetsAt") or 0))
    if week:
        fields.update(util_7d=float(week.get("utilization") or 0), reset_7d=int(week.get("resetsAt") or 0))
    kind = info.get("rateLimitType")
    util = info.get("utilization")
    if util is not None and not five and kind in (None, "five_hour"):
        fields.update(util_5h=float(util), reset_5h=int(info.get("resetsAt") or 0))
    if util is not None and not week and kind in ("seven_day", "seven_day_opus", "seven_day_sonnet"):
        fields.update(util_7d=float(util), reset_7d=int(info.get("resetsAt") or 0))
    status = info.get("status") or "allowed"
    fields["status"] = status
    if status == "rejected":
        fields["parked_until"] = int(info.get("resetsAt") or (now() + 3600))
    store.upsert_account(account, **fields)
    return fields


def mode_of(acc: dict, t: float | None = None) -> str:
    t = t or now()
    if (acc.get("parked_until") or 0) > t:
        return "parked"
    u5, r5, u7 = acc.get("util_5h"), acc.get("reset_5h"), acc.get("util_7d")
    if u7 is not None and u7 >= 0.92:
        return "conserve"
    if u5 is None:
        return "normal"
    left = max(0.0, (r5 or t) - t)
    headroom = 1.0 - u5
    elapsed = 1.0 - min(1.0, left / FIVE_H)
    if headroom <= 0.12 and left > 20 * 60:
        return "conserve"
    if headroom >= 0.35 and left <= 45 * 60:
        return "spend"  # use it or lose it: the window resets soon
    if elapsed > 0.25 and u5 < 0.6 * elapsed:
        return "spend"  # burning slower than the clock
    return "normal"


def refresh_modes(store) -> dict[str, str]:
    t = now()
    modes = {}
    for acc in store.accounts():
        mode = mode_of(acc, t)
        if acc.get("status") == "rejected" and (acc.get("parked_until") or 0) <= t:
            store.upsert_account(acc["name"], status="allowed")
        if mode != acc.get("mode"):
            store.upsert_account(acc["name"], mode=mode)
        modes[acc["name"]] = mode
    return modes


def predicted_util(task: dict, cost_model: dict, model: str, account: str) -> float | None:
    """Share of an account's 5-hour window a task is expected to use (None = not enough history)."""
    per_unit = (cost_model.get("tokens_per_unit") or {}).get(model)
    per_token = (cost_model.get("util_per_token") or {}).get(account)
    if not per_unit or not per_token:
        return None
    return SIZE_UNITS.get(task["size"], 3) * per_unit * per_token


def fits(task: dict, acc: dict, mode: str, cost_model: dict, model: str) -> bool:
    if mode == "parked":
        return False
    if mode == "conserve" and not (task["size"] == "S" or task["kind"] in LIGHT_KINDS):
        return False
    need = predicted_util(task, cost_model, model, acc["name"])
    if need is not None and acc.get("util_5h") is not None:
        return need <= (1.0 - acc["util_5h"]) + 0.02
    return True


def _burn(acc: dict) -> float:
    """Lower = this account should get the next piece of work."""
    u5 = acc.get("util_5h")
    u7 = acc.get("util_7d") or 0.0
    return (u5 if u5 is not None else 0.3) + 0.5 * u7


def tier_allows(seat: dict, task: dict, workhorse_usable: bool, manager_may_help: bool) -> bool:
    """Workhorse seats (Sonnet) take only workhorse tasks; manager seats (Opus) take manager tasks, and
    workhorse tasks only when no workhorse seat can run or when the manager may help (see the orchestrator)."""
    tier = task.get("tier") or "manager"
    if seat_tier(seat) == "workhorse":
        return tier == "workhorse" and task.get("kind") != "foundation"
    if task.get("twin"):
        return tier == "manager"  # a head-to-head needs the two tiers' own models: no stand-ins
    return tier == "manager" or task.get("kind") == "foundation" or not workhorse_usable or manager_may_help


def choose_task(seat: dict, ready: list[dict], acc: dict, mode: str, cost_model: dict, model: str,
                idle_names: set[str], grace_until: dict[int, float], workhorse_usable: bool = False,
                may_help: set[int] | None = None) -> dict | None:
    """Best ready task for an idle seat: the right tier first, then the lead's suggested owners."""
    t = now()
    options = []
    for task in ready:
        if not tier_allows(seat, task, workhorse_usable, task["id"] in (may_help or set())):
            continue
        if not fits(task, acc, mode, cost_model, model):
            continue
        owner = task.get("suggested_owner")
        if owner and owner != seat["name"]:
            # The lead chose someone else: wait for them while they are busy, up to a grace period.
            if owner not in idle_names and grace_until.get(task["id"], 0) > t:
                continue
            if owner in idle_names:
                continue
        mine = 0 if owner == seat["name"] else 1
        size = SIZE_UNITS.get(task["size"], 3)
        weight = -size if mode == "spend" else (size if mode == "conserve" else 0)
        options.append((mine, weight, task["id"], task))
    if not options:
        return None
    options.sort(key=lambda o: o[:3])
    return options[0][3]


def order_idle_seats(seats: list[dict], accounts: dict[str, dict]) -> list[dict]:
    """Hand out work to the seats whose accounts have burned least, so all drain at a similar pace."""
    return sorted(seats, key=lambda s: _burn(accounts.get(s["account"], {})))


def pick_account(candidates: list[dict], modes: dict[str, str], prefer_vendor: str | None = None,
                 avoid: str | None = None) -> dict | None:
    """Account for a one-shot run (review, CEO): most headroom, preferred vendor, not parked."""
    usable = [a for a in candidates if modes.get(a["name"]) != "parked"]
    if not usable:
        return None

    def key(a: dict):
        return (0 if prefer_vendor and a["vendor"] == prefer_vendor else 1,
                1 if a["name"] == avoid else 0,
                1 if modes.get(a["name"]) == "conserve" else 0,
                _burn(a))

    return sorted(usable, key=key)[0]
