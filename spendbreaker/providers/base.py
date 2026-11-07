import datetime as dt
import os
from dataclasses import dataclass, field


class ProviderError(Exception):
    pass


class BreakNotSupported(ProviderError):
    pass


@dataclass
class Snapshot:
    provider: str
    day_total: float          # spend for the current UTC day so far
    month_total: float        # spend for the current UTC month so far
    currency: str = "USD"
    estimated: bool = False   # True when derived from usage x configured price
    note: str = ""
    daily: dict = field(default_factory=dict)  # "YYYY-MM-DD" -> amount


def utc_bounds(now):
    """Return (month_start, day_start, tomorrow_start) as aware UTC datetimes."""
    day = now.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.replace(day=1), day, day + dt.timedelta(days=1)


def snapshot_from_daily(name, daily, now, **kw):
    _, day, _ = utc_bounds(now)
    month_prefix = day.strftime("%Y-%m")
    month_total = sum(v for k, v in daily.items() if k.startswith(month_prefix))
    return Snapshot(name, round(daily.get(day.strftime("%Y-%m-%d"), 0.0), 6),
                    round(month_total, 6), daily=dict(sorted(daily.items())), **kw)


class Provider:
    name = "base"
    default_key_env = None
    break_actions = ()  # official, documented actions this provider supports

    def __init__(self, cfg, http, env=None):
        self.cfg = cfg or {}
        self.http = http
        self.env = os.environ if env is None else env

    def secret(self, cfg_field="key_env", default=None):
        var = self.cfg.get(cfg_field) or default or self.default_key_env
        val = self.env.get(var, "")
        if not val:
            raise ProviderError(f"{self.name}: environment variable {var} is not set")
        return val

    def fetch(self, now):  # -> Snapshot
        raise NotImplementedError

    def do_break(self, action_cfg, dry_run=False):  # -> list[str] of human-readable results
        raise BreakNotSupported(f"{self.name}: no official break action implemented")
