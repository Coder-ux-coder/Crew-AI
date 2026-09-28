"""Settings: accounts (subscriptions), seats (workers), model policy, team limits.

Everything has a default, so a run works with no settings file at all: one
Claude account (your normal login) and one seat.

The team has three tiers (see tiers.py): GPT-6 Sol is the workhorse (Codex seats),
Opus 5.5 the manager (Claude seats, the lead among them), GPT-6 Astra the CEO.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .tiers import vendor_of
from .util import crew_home

EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT_CHOICES = ("auto",) + EFFORTS  # auto: the model decides (assistant), or the CEO decides per task (team)
CEO_EFFORT_CHOICES = EFFORT_CHOICES + ("ultra",)  # GPT-6's deepest level; a Claude CEO runs it as max
VENDORS = ("claude", "codex")


class ConfigError(ValueError):
    pass


@dataclass
class ModelPolicy:
    work: str = "claude-opus-5-5"        # the manager: plans, reviews, builds the hard parts (Claude seats)
    ceo: str = "gpt-6-astra"             # the CEO: plan review, final approval, rulings
    ceo_backup: str = "claude-fable-5-1"  # the CEO when its model cannot run (no ChatGPT, a limit, an error)
    codex: str = "gpt-6-sol"             # the workhorse: routine work (Codex seats); empty = Codex's default
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
        for name in ("effort_work", "effort_light"):
            if getattr(self, name) not in EFFORT_CHOICES:
                raise ConfigError(f"models.{name} must be one of {EFFORT_CHOICES}")
        if self.effort_ceo not in CEO_EFFORT_CHOICES:
            raise ConfigError(f"models.effort_ceo must be one of {CEO_EFFORT_CHOICES}")
        self.check(self.work)
        if vendor_of(self.work) != "claude":
            raise ConfigError("models.work (the manager, who leads the team) must be a Claude model")
        for name in ("ceo", "ceo_backup", "codex"):
            if getattr(self, name):
                self.check(getattr(self, name))
        if self.ceo_backup and vendor_of(self.ceo_backup) != "claude":
            raise ConfigError("models.ceo_backup must be a Claude model (it runs when ChatGPT cannot)")
        if self.codex and vendor_of(self.codex) != "codex":
            raise ConfigError("models.codex (the workhorse) must be a ChatGPT model, such as gpt-6-sol")


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
    workhorse_seats: int = 2  # GPT-6 Sol seats per ChatGPT subscription (they do most tasks by count)
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
            lead = next(s for s in seats if s.vendor == "claude")
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
    if not 1 <= int(team.workhorse_seats) <= 6:
        raise ConfigError("team.workhorse_seats must be between 1 and 6")
    if team.head_to_head not in ("off", "some", "all"):
        raise ConfigError('team.head_to_head must be "off", "some" or "all"')
    if team.head_to_head_style not in ("compete", "combine"):
        raise ConfigError('team.head_to_head_style must be "compete" or "combine"')

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
        seat_specs = _default_seats(accounts, seats or data.get("team", {}).get("seats"), int(team.workhorse_seats))
    if sum(1 for s in seat_specs if s.role == "lead") != 1:
        raise ConfigError("exactly one seat must have role = \"lead\"")
    lead = next(s for s in seat_specs if s.role == "lead")
    if lead.vendor != "claude":
        raise ConfigError("the lead seat must be a Claude seat")
    for spec in seat_specs:
        acc = next((a for a in accounts if a.name == spec.account), None)
        if acc is None or acc.vendor != spec.vendor:
            raise ConfigError(f"seat {spec.name}: account '{spec.account}' missing or wrong vendor")

    return Config(team=team, models=models, accounts=accounts, seats=seat_specs, source=path,
                  explicit_seats=bool(data.get("seat")))


def _default_seats(accounts: list[Account], count: int | None, workhorse_seats: int = 2) -> list[SeatSpec]:
    """One manager seat per Claude account (the first leads) and `workhorse_seats` GPT-6 Sol seats per ChatGPT
    account. With an explicit seat count, seats share the accounts round-robin instead."""
    claude = [a for a in accounts if a.vendor == "claude"]
    if not claude:
        raise ConfigError("at least one Claude account is needed (the lead runs on Claude)")
    codex = [a for a in accounts if a.vendor != "claude"]
    if count:
        ordered = claude + codex
        plan = [ordered[i % len(ordered)] for i in range(max(int(count), 1))]
    else:
        plan = claude + [acc for acc in codex for _ in range(max(1, int(workhorse_seats or 1)))]
    return [SeatSpec(name=_seat_name(i, acc.vendor), vendor=acc.vendor, account=acc.name,
                     role="lead" if i == 0 else "member") for i, acc in enumerate(plan)]


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
