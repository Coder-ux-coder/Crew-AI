"""Settings: accounts (subscriptions), seats (workers), model policy, team limits.

Everything has a default, so a run works with no settings file at all: one
Claude account (your normal login) and one seat.

The team has three tiers (see tiers.py): Sonnet 5.5 is the workhorse and Opus 5.5 the manager (both on Claude
seats; the lead is a manager), GPT-6 Astra the CEO (on ChatGPT, with a Claude model as its backup).
"""

from __future__ import annotations

import math
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from .tiers import vendor_of
from .util import crew_home

EFFORTS = ("low", "medium", "high", "xhigh", "max")
ALLOWED = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1")  # the Claude models Crew may run
BANNED = ("haiku", "terra", "luna", "gpt-6-sol")  # the owner's choice: GPT-6 Sol was replaced by Sonnet 5.5
EFFORT_CHOICES = ("auto",) + EFFORTS  # auto: the model decides (assistant), or the CEO decides per task (team)
CEO_EFFORT_CHOICES = EFFORT_CHOICES + ("ultra",)  # GPT-6's deepest level; a Claude CEO runs it as max
VENDORS = ("claude", "codex")
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+\[\]-]{0,127}")  # claude-opus-5-5, gpt-6-astra, sonnet[1m] …


class ConfigError(ValueError):
    pass


@dataclass
class ModelPolicy:
    work: str = "claude-opus-5-5"        # the manager: plans, reviews, builds the hard parts (Claude seats)
    ceo: str = "gpt-6-astra"             # the CEO: plan review, final approval, rulings
    ceo_backup: str = "claude-fable-5-1"  # the CEO when its model cannot run (no ChatGPT, a limit, an error)
    workhorse: str = "claude-sonnet-5-5"  # the workhorse: routine, fully specified work (Claude seats)
    allowed: list[str] = field(default_factory=lambda: list(ALLOWED))
    banned: list[str] = field(default_factory=lambda: list(BANNED))
    effort_work: str = "auto"   # auto: the CEO sets each task's effort when it reviews the plan
    effort_light: str = "auto"  # checks, notes, research
    effort_ceo: str = "max"     # the CEO always thinks hardest

    def check(self, model: str) -> str:
        """Return the model if policy allows it, else raise. Every model is subject to the ban list; Claude
        models also to the allow list."""
        m = (model or "").strip()
        if m and not MODEL_NAME.fullmatch(m):  # a stray character would break every start of the chat later
            raise ConfigError(f"'{m[:60]}' is not a model's name (letters, digits and . - _ : @ / [ ] only)")
        low = m.lower()
        for bad in self.banned:
            if bad and bad.lower() in low:
                raise ConfigError(f"{m} is on your list of banned models (it matches “{bad}”), so Crew will not use "
                                  "it. Choose another model, or lift the ban in Settings → Models")
        if low.startswith("claude") or low in ("opus", "sonnet", "fable"):
            if self.allowed and m not in self.allowed:
                raise ConfigError(f"{m} is not one of the Claude models allowed in Settings → Models "
                                  f"({', '.join(self.allowed)}). Choose one of those, or add it to that list")
        return m

    def validate(self) -> None:
        for name in ("effort_work", "effort_light"):
            if getattr(self, name) not in EFFORT_CHOICES:
                raise ConfigError(f"models.{name} must be one of: {', '.join(EFFORT_CHOICES)}")
        if self.effort_ceo not in CEO_EFFORT_CHOICES:
            raise ConfigError(f"models.effort_ceo must be one of: {', '.join(CEO_EFFORT_CHOICES)}")
        self.check(self.work)
        if vendor_of(self.work) != "claude":
            raise ConfigError("models.work (the manager, who leads the team) must be a Claude model")
        if not self.workhorse:
            raise ConfigError("models.workhorse must name a model, such as claude-sonnet-5-5")
        self.check(self.workhorse)
        if vendor_of(self.workhorse) != "claude":
            raise ConfigError("models.workhorse must be a Claude model, such as claude-sonnet-5-5 (the workhorse "
                              "seats run on your Claude subscriptions)")
        for name in ("ceo", "ceo_backup"):
            if getattr(self, name):
                self.check(getattr(self, name))
        if self.ceo_backup and vendor_of(self.ceo_backup) != "claude":
            raise ConfigError("models.ceo_backup must be a Claude model (it runs when ChatGPT cannot)")


@dataclass
class Account:
    name: str
    vendor: str  # claude | codex
    profile: str = ""  # "" -> ~/.crew/accounts/<name>; "default" -> the CLI's normal login

    def profile_dir(self) -> Path | None:
        """Config home passed as CLAUDE_CONFIG_DIR / CODEX_HOME (None = CLI default)."""
        if self.profile == "default":
            return None
        if self.profile:
            return Path(self.profile).expanduser().resolve()
        accounts = (crew_home() / "accounts").resolve()
        path = (accounts / self.name).resolve()
        if accounts not in path.parents:  # a name such as "../x" must not reach outside Crew's accounts folder
            path = accounts / ("".join(c if c.isalnum() or c in "_@+-" else "_" for c in self.name) or "account")
        return path


@dataclass
class SeatSpec:
    name: str
    vendor: str
    account: str
    role: str = "member"  # lead | member
    tier: str = ""  # manager | workhorse ("" in an older file: the lead and other seats are managers)


@dataclass
class TeamSettings:
    mode: str = "auto"  # auto | team | solo  (auto: solo for small or hard-to-split jobs)
    max_hours: float = 0.0  # 0: no time limit (set a timer per project if you want one)
    max_cost_usd: float = 0.0  # 0 = no dollar cap (subscriptions are flat-rate)
    stall_minutes: float = 8.0
    ledger_minutes: float = 12.0
    chat_budget: int = 8
    review: str = "cross"  # cross | same | off
    ceo_reviews: bool = True
    deliver: str = "merge"  # merge | branch | push
    permission_mode: str = "bypassPermissions"  # or auto
    max_review_rounds: int = 2
    checks_timeout_minutes: float = 15.0
    web_port: int = 8765
    workhorse_seats: int = 2  # Sonnet 5.5 seats in a team, spread over the Claude subscriptions (most tasks by count)
    head_to_head: str = "off"  # off | some | all: parts built by both tiers' models, judged blind (feeds the scorecard)
    head_to_head_style: str = "combine"  # combine: keep the better version and fold in what the other did better
    prompt_writer: bool = True  # the owner's messages to a team are written up clearly before the agents read them


@dataclass
class Config:
    team: TeamSettings
    models: ModelPolicy
    accounts: list[Account]
    seats: list[SeatSpec]
    source: Path | None = None
    explicit_seats: bool = False

    def restrict(self, names: list[str]) -> "Config":
        """Only these subscriptions for one project (the owner's choice). The seats follow the subscriptions."""
        keep = [a for a in self.accounts if a.name in set(names)]
        missing = sorted(set(names) - {a.name for a in keep})
        if missing:
            raise ConfigError(f"unknown subscription(s): {', '.join(missing)}")
        if not any(a.vendor == "claude" for a in keep):
            raise ConfigError("choose at least one Claude subscription: the team's lead runs on Claude")
        chosen = {a.name for a in keep}
        seats = [s for s in self.seats if s.account in chosen] if self.explicit_seats else \
            _default_seats(keep, None, int(self.team.workhorse_seats))
        if not any(s.role == "lead" for s in seats):
            lead = next((s for s in seats if s.vendor == "claude" and s.tier == "manager"), None)
            if lead is None:
                raise ConfigError("none of your seats on the chosen subscriptions can lead (a manager seat on "
                                  "Claude): choose another subscription too")
            lead.role = "lead"
        self.accounts, self.seats = keep, seats
        return self

    def account(self, name: str) -> Account:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        raise ConfigError(f"unknown account '{name}'")

    def accounts_for(self, vendor: str) -> list[Account]:
        return [a for a in self.accounts if a.vendor == vendor]

    @property
    def lead(self) -> SeatSpec:
        return next(s for s in self.seats if s.role == "lead")


def _find_config(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"settings file not found: {path}")
        return path
    for candidate in (Path.cwd() / "crew.toml", crew_home() / "crew.toml"):
        if candidate.is_file():
            return candidate
    return None


def load(explicit: str | None = None, seats: int | None = None) -> Config:
    path = _find_config(explicit)
    data = tomllib.loads(path.read_text(encoding="utf-8-sig")) if path else {}  # -sig: Notepad's byte-order mark

    team = TeamSettings(**_known(TeamSettings, data.get("team", {})))
    models = ModelPolicy(**_known(ModelPolicy, _current_models(data.get("models", {}))))
    models.validate()
    if team.mode not in ("auto", "team", "solo"):
        raise ConfigError('team.mode must be "auto", "team" or "solo"')
    if team.review not in ("cross", "same", "off"):
        raise ConfigError('team.review must be "cross", "same" or "off"')
    if team.deliver not in ("merge", "branch", "push"):
        raise ConfigError('team.deliver must be "merge", "branch" or "push"')
    if not 1 <= int(team.workhorse_seats) <= 6:
        raise ConfigError("team.workhorse_seats must be between 1 and 6")
    if team.head_to_head not in ("off", "some", "all"):
        raise ConfigError('team.head_to_head must be "off", "some" or "all"')
    if team.head_to_head_style not in ("compete", "combine"):
        raise ConfigError('team.head_to_head_style must be "compete" or "combine"')
    # Numbers that would stop the team working: no time for the checks, a watchdog that never waits …
    for name in ("stall_minutes", "ledger_minutes", "checks_timeout_minutes"):
        if getattr(team, name) <= 0:
            raise ConfigError(f"team.{name} must be more than 0")
    for name in ("max_hours", "max_cost_usd"):
        if getattr(team, name) < 0:
            raise ConfigError(f"team.{name} cannot be below 0 (0 means no limit)")
    for name in ("chat_budget", "max_review_rounds"):
        if getattr(team, name) < 1:
            raise ConfigError(f"team.{name} must be 1 or more")
    if not 0 <= team.web_port <= 65535:
        raise ConfigError("team.web_port must be a port number (0 to 65535)")

    accounts = [Account(**_known(Account, a)) for a in data.get("account", [])]
    if not accounts:
        accounts = [Account(name="claude-1", vendor="claude", profile="default")]
        if shutil.which("codex") and os.environ.get("CREW_AUTO_CODEX") == "1":
            accounts.append(Account(name="codex-1", vendor="codex", profile="default"))
    names = [a.name for a in accounts]
    if len(set(names)) != len(names):
        raise ConfigError("account names must be unique")
    for acc in accounts:
        if acc.vendor not in VENDORS:
            raise ConfigError(f"account {acc.name}: vendor must be claude or codex (ChatGPT)")

    # Seats written by hand. ChatGPT seats were the workhorse before Sonnet 5.5 took that place; ChatGPT now only
    # reviews (the CEO), so such a seat is left out rather than stopping every project.
    seat_specs = [SeatSpec(**_known(SeatSpec, s)) for s in data.get("seat", [])
                  if not (isinstance(s, dict) and s.get("vendor") == "codex")]
    if not seat_specs:
        seat_specs = _default_seats(accounts, seats or data.get("team", {}).get("seats"), int(team.workhorse_seats))
    for spec in seat_specs:
        spec.tier = spec.tier or "manager"
        if spec.tier not in ("manager", "workhorse"):
            raise ConfigError(f"seat {spec.name}: tier must be \"manager\" or \"workhorse\"")
    if sum(1 for s in seat_specs if s.role == "lead") != 1:
        raise ConfigError("exactly one seat must have role = \"lead\"")
    lead = next(s for s in seat_specs if s.role == "lead")
    if lead.vendor != "claude" or lead.tier != "manager":
        raise ConfigError("the lead seat must be a manager seat on Claude")
    for spec in seat_specs:
        acc = next((a for a in accounts if a.name == spec.account), None)
        if acc is None or acc.vendor != spec.vendor:
            raise ConfigError(f"seat {spec.name}: account '{spec.account}' missing or wrong vendor")

    return Config(team=team, models=models, accounts=accounts, seats=seat_specs, source=path,
                  explicit_seats=bool(data.get("seat")))


def _default_seats(accounts: list[Account], count: int | None, workhorse_seats: int = 2) -> list[SeatSpec]:
    """One manager seat per Claude account (the first leads), then `workhorse_seats` workhorse seats spread over
    the Claude accounts, starting after the lead's so its account is not the busiest. With an explicit seat count,
    the first seats are managers (one per Claude account) and the rest workhorses. ChatGPT accounts get no seats:
    they run the CEO."""
    claude = [a for a in accounts if a.vendor == "claude"]
    if not claude:
        raise ConfigError("at least one Claude account is needed (the lead runs on Claude)")
    total = max(int(count), 1) if count else len(claude) + max(1, int(workhorse_seats or 1))
    managers = min(total, len(claude))
    plan = [(acc, "manager") for acc in claude[:managers]]
    plan += [(claude[(1 + i) % len(claude)], "workhorse") for i in range(total - managers)]
    return [SeatSpec(name=_seat_name(i, acc.vendor), vendor=acc.vendor, account=acc.name,
                     role="lead" if i == 0 else "member", tier=tier) for i, (acc, tier) in enumerate(plan)]


def _current_models(values: dict) -> dict:
    """The [models] section as this Crew reads it. A file from before Sonnet 5.5 became the workhorse names the old
    ChatGPT workhorse as `codex`; that model no longer has a place in the team, so the setting is left out."""
    if not isinstance(values, dict):
        raise ConfigError("the models settings must be a group of named values")
    return {k: v for k, v in values.items() if k != "codex"}


_NAMES = ["ada", "boole", "curie", "dijkstra", "euler", "fermi", "gauss", "hopper", "ibn-sina", "jabir"]


def _seat_name(i: int, vendor: str) -> str:
    base = _NAMES[i % len(_NAMES)]
    return base if i < len(_NAMES) else f"{base}-{i}"


SECTIONS = {"TeamSettings": "team", "ModelPolicy": "models", "Account": "account", "SeatSpec": "seat"}


def _known(cls, values: dict) -> dict:
    """The settings a section may have, each of the kind its default is: text where a number belongs (a hand
    edit, or a stray request) would otherwise be saved and stop every project later, far from its cause."""
    known = cls.__dataclass_fields__
    unknown = set(values) - set(known) - {"seats"}
    if unknown:
        raise ConfigError(f"unknown setting(s) for {cls.__name__}: {', '.join(sorted(unknown))}")
    out = {k: v for k, v in values.items() if k in known}
    for f in fields(cls):
        if f.name not in out:
            continue
        v, kind, where = out[f.name], str(f.type), f"{SECTIONS.get(cls.__name__, cls.__name__)}.{f.name}"
        if kind == "bool":
            if not isinstance(v, bool):
                raise ConfigError(f"{where} must be true or false")
        elif kind in ("int", "float"):
            infinite = isinstance(v, float) and not math.isfinite(v)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or infinite:
                raise ConfigError(f"{where} must be a number")
            if kind == "int":
                if v != int(v):
                    raise ConfigError(f"{where} must be a whole number")
                out[f.name] = int(v)
        elif kind == "str":
            if not isinstance(v, str):
                raise ConfigError(f"{where} must be text")
        elif kind.startswith("list"):
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ConfigError(f"{where} must be a list of names")
    return out
