"""Settings: accounts (subscriptions), seats (workers), model policy, team limits.

Everything has a default, so a run works with no settings file at all: one
Claude account (your normal login) and two seats.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .util import crew_home

EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT_CHOICES = ("auto",) + EFFORTS  # auto: the model decides (assistant), or the CEO decides per task (team)
VENDORS = ("claude", "codex")


class ConfigError(ValueError):
    pass


@dataclass
class ModelPolicy:
    work: str = "claude-opus-5-5"
    ceo: str = "claude-fable-5-1"
    codex: str = ""  # empty: Codex uses its own default (its best model)
    allowed: list[str] = field(default_factory=lambda: ["claude-opus-5-5", "claude-fable-5-1"])
    banned: list[str] = field(default_factory=lambda: ["haiku", "sonnet", "terra", "luna"])
    effort_work: str = "auto"   # auto: the CEO sets each task's effort when it reviews the plan
    effort_light: str = "auto"  # checks, notes, research
    effort_ceo: str = "max"     # the CEO always thinks hardest

    def check(self, model: str) -> str:
        """Return the model if policy allows it, else raise. Codex models are
        governed by `codex`, Claude models by the allow/ban lists."""
        m = (model or "").strip()
        low = m.lower()
        for bad in self.banned:
            if bad and bad.lower() in low:
                raise ConfigError(f"model '{m}' is banned by your policy ({bad})")
        if low.startswith("claude") or low in ("opus", "fable"):
            if self.allowed and m not in self.allowed:
                raise ConfigError(f"model '{m}' is not on the allowed list {self.allowed}")
        return m

    def validate(self) -> None:
        for name in ("effort_work", "effort_light", "effort_ceo"):
            if getattr(self, name) not in EFFORT_CHOICES:
                raise ConfigError(f"models.{name} must be one of {EFFORT_CHOICES}")
        self.check(self.work)
        if self.ceo:
            self.check(self.ceo)


@dataclass
class Account:
    name: str
    vendor: str  # claude | codex
    profile: str = ""  # "" -> ~/.crew/accounts/<name>; "default" -> the CLI's normal login

    def profile_dir(self) -> Path | None:
        """Config home passed as CLAUDE_CONFIG_DIR / CODEX_HOME (None = CLI default)."""
        if self.profile == "default":
            return None
        path = Path(self.profile).expanduser() if self.profile else crew_home() / "accounts" / self.name
        return path.resolve()


@dataclass
class SeatSpec:
    name: str
    vendor: str
    account: str
    role: str = "member"  # lead | member


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


@dataclass
class Config:
    team: TeamSettings
    models: ModelPolicy
    accounts: list[Account]
    seats: list[SeatSpec]
    source: Path | None = None

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
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path else {}

    team = TeamSettings(**_known(TeamSettings, data.get("team", {})))
    models = ModelPolicy(**_known(ModelPolicy, data.get("models", {})))
    models.validate()
    if team.mode not in ("auto", "team", "solo"):
        raise ConfigError('team.mode must be "auto", "team" or "solo"')
    if team.review not in ("cross", "same", "off"):
        raise ConfigError('team.review must be "cross", "same" or "off"')
    if team.deliver not in ("merge", "branch", "push"):
        raise ConfigError('team.deliver must be "merge", "branch" or "push"')

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
            raise ConfigError(f"account {acc.name}: vendor must be one of {VENDORS}")

    seat_specs = [SeatSpec(**_known(SeatSpec, s)) for s in data.get("seat", [])]
    if not seat_specs:
        seat_specs = _default_seats(accounts, seats or data.get("team", {}).get("seats"))
    if sum(1 for s in seat_specs if s.role == "lead") != 1:
        raise ConfigError("exactly one seat must have role = \"lead\"")
    lead = next(s for s in seat_specs if s.role == "lead")
    if lead.vendor != "claude":
        raise ConfigError("the lead seat must be a Claude seat")
    for spec in seat_specs:
        acc = next((a for a in accounts if a.name == spec.account), None)
        if acc is None or acc.vendor != spec.vendor:
            raise ConfigError(f"seat {spec.name}: account '{spec.account}' missing or wrong vendor")

    return Config(team=team, models=models, accounts=accounts, seats=seat_specs, source=path)


def _default_seats(accounts: list[Account], count: int | None) -> list[SeatSpec]:
    """One seat per account; with a seat count, extra seats share accounts round-robin."""
    claude = [a for a in accounts if a.vendor == "claude"]
    if not claude:
        raise ConfigError("at least one Claude account is needed (the lead runs on Claude)")
    ordered = claude + [a for a in accounts if a.vendor != "claude"]
    total = max(int(count or len(ordered)), 1)
    seats = []
    for i in range(total):
        acc = ordered[i % len(ordered)]
        role = "lead" if i == 0 else "member"
        seats.append(SeatSpec(name=_seat_name(i, acc.vendor), vendor=acc.vendor, account=acc.name, role=role))
    return seats


_NAMES = ["ada", "boole", "curie", "dijkstra", "euler", "fermi", "gauss", "hopper", "ibn-sina", "jabir"]


def _seat_name(i: int, vendor: str) -> str:
    base = _NAMES[i % len(_NAMES)]
    return base if i < len(_NAMES) else f"{base}-{i}"


def _known(cls, values: dict) -> dict:
    fields = cls.__dataclass_fields__
    unknown = set(values) - set(fields) - {"seats"}
    if unknown:
        raise ConfigError(f"unknown setting(s) for {cls.__name__}: {', '.join(sorted(unknown))}")
    return {k: v for k, v in values.items() if k in fields}
