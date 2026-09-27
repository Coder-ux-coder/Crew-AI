"""The team's three tiers, as the owner set them.

  workhorse  GPT-6 Sol (Codex seats): routine, fully specified work — most tasks by count.
  manager    Opus 5.5 (Claude seats): plans, reviews every workhorse task, builds what needs high intelligence.
  ceo        GPT-6 Astra: reviews the plan and gives the final approval; checks rather than builds.

Token targets (share of all tokens a project uses): manager 60–70%, CEO about 5%, workhorse the rest.
"""

from __future__ import annotations

TIERS = ("workhorse", "manager")
TARGETS = {"workhorse": (25, 35), "manager": (60, 70), "ceo": (0, 5)}  # percent of a project's tokens
TIER_NAMES = {"workhorse": "Workhorse", "manager": "Manager", "ceo": "CEO"}

MODEL_LABELS = {
    "gpt-6-sol": "GPT-6 Sol", "gpt-6-astra": "GPT-6 Astra", "gpt-6-luna": "GPT-6 Luna",
    "claude-opus-5-5": "Opus 5.5", "claude-fable-5-1": "Fable 5.1", "claude-opus-5": "Opus 5",
}


def vendor_of(model: str) -> str:
    """Which CLI runs a model: OpenAI's models run in Codex, everything else in Claude Code."""
    m = (model or "").strip().lower()
    if m.startswith(("gpt", "codex", "o1", "o3", "o4", "o5")) or m == "codex-default":
        return "codex"
    return "claude"


def model_label(model: str) -> str:
    m = (model or "").strip()
    if not m or m == "codex-default":
        return "ChatGPT"
    return MODEL_LABELS.get(m.lower(), m)


def seat_tier(vendor: str) -> str:
    return "workhorse" if vendor == "codex" else "manager"


def default_tier(kind: str | None, size: str | None) -> str:
    """When the lead does not say: shared decisions and big pieces need the manager; small or routine work does not."""
    if kind == "foundation" or size == "L":
        return "manager"
    if kind in ("research", "docs", "test", "verify") or size == "S":
        return "workhorse"
    return "manager"


def shares(store) -> dict:
    """Tokens per tier so far, with each tier's share and target: seats by their vendor, one-off runs by role."""
    used = {"workhorse": 0, "manager": 0, "ceo": 0}
    models: dict[str, dict] = {}

    def add(tier: str, model: str, tokens: int) -> None:
        if tokens <= 0:
            return
        used[tier] += tokens
        slot = models.setdefault(model or "", {"model": model or "", "label": model_label(model), "tier": tier,
                                               "tokens": 0})
        slot["tokens"] += tokens

    for s in store.seats():
        add(seat_tier(s.get("vendor") or "claude"), s.get("model") or "", int(s.get("tokens") or 0))
    started: dict[str, dict] = {}
    for ev in store.events("oneoff", limit=2000):
        d = ev["data"]
        if d.get("state") == "start":
            started[ev["seat"]] = d
            continue
        info = started.get(ev["seat"]) or {}
        role, vendor = info.get("role") or "", info.get("vendor") or "claude"
        tier = "ceo" if role == "ceo" else seat_tier(vendor)
        add(tier, info.get("model") or "", int(d.get("tokens") or 0))
    total = sum(used.values())
    tiers = []
    for tier in ("workhorse", "manager", "ceo"):
        low, high = TARGETS[tier]
        pct = round(100 * used[tier] / total, 1) if total else 0.0
        tiers.append({"tier": tier, "name": TIER_NAMES[tier], "tokens": used[tier], "pct": pct,
                      "target": [low, high], "on_target": (not total) or (low - 5 <= pct <= high + 5)})
    return {"total": total, "tiers": tiers, "models": sorted(models.values(), key=lambda m: -m["tokens"])}


def share_of(store, tier: str) -> float | None:
    """One tier's share of the tokens used so far (None before anything is used)."""
    data = shares(store)
    if not data["total"]:
        return None
    return next(t["pct"] for t in data["tiers"] if t["tier"] == tier)
