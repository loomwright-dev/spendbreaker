"""Spend-rate, cap and anomaly evaluation. Pure functions over snapshots + state."""
import calendar
import datetime as dt
import statistics
from dataclasses import dataclass


@dataclass
class Event:
    kind: str          # daily_warn|daily_breach|monthly_warn|monthly_breach|monthly_projected|anomaly|fetch_error|break
    provider: str      # provider name or "total"
    message: str
    dedupe_key: str
    severity: str = "warn"   # info|warn|critical
    cooldown_hours: float = 0  # 0 = once per dedupe_key forever (key includes the period)


def _iso(t):
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def record_sample(pstate, snap, now):
    """Append a sample and compute the hourly spend rate since the previous sample."""
    samples = pstate.setdefault("samples", [])
    today = now.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
    rate = None
    if samples:
        prev = samples[-1]
        hours = (now - _parse(prev["ts"])).total_seconds() / 3600
        if hours > 0.05:
            if prev["date"] == today:
                delta = snap.day_total - prev["day"]
            else:  # day rolled over: spend since midnight, plus late-arriving spend on prev day if visible
                prev_day_now = snap.daily.get(prev["date"], prev["day"])
                delta = snap.day_total + max(0.0, prev_day_now - prev["day"])
            rate = max(0.0, delta) / hours
    samples.append({"ts": _iso(now), "date": today, "day": snap.day_total,
                    "month": snap.month_total, "rate": rate})
    return rate


def anomaly_event(name, pstate, rate, acfg, now, currency):
    if not acfg.get("enabled", True) or rate is None:
        return None
    hist = [s["rate"] for s in pstate.get("samples", [])[:-1] if s.get("rate") is not None]
    hist = hist[-int(acfg.get("window_samples", 168)):]
    if len(hist) < int(acfg.get("min_samples", 6)):
        return None
    if rate < float(acfg.get("min_hourly_amount", 1.0)):
        return None
    med = statistics.median(hist)
    mult = float(acfg.get("multiplier", 3.0))
    if rate <= mult * med:
        return None
    ratio = "from ~0" if med == 0 else f"{rate / med:.1f}x trailing median {med:.2f}/h"
    return Event("anomaly", name, f"{name}: spending {rate:.2f} {currency}/h ({ratio})",
                 f"anomaly:{name}", "warn", float(acfg.get("cooldown_hours", 6)))


def cap_events(name, day_total, month_total, pcfg, warn_at, now, currency):
    ev = []
    day = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    for period, total, cap, key in (("daily", day_total, pcfg.get("daily_cap"), day),
                                    ("monthly", month_total, pcfg.get("monthly_cap"), month)):
        if cap is None:
            continue
        cap = float(cap)
        if total >= cap:
            ev.append(Event(f"{period}_breach", name,
                            f"{name}: {period} spend {total:.2f} {currency} >= cap {cap:.2f}",
                            f"{period}_breach:{name}:{key}", "critical"))
        elif cap > 0 and total >= warn_at * cap:
            ev.append(Event(f"{period}_warn", name,
                            f"{name}: {period} spend {total:.2f} {currency} is {total / cap:.0%} of cap {cap:.2f}",
                            f"{period}_warn:{name}:{key}", "warn"))
    mcap = pcfg.get("monthly_cap")
    if mcap and month_total < float(mcap):
        days_in = calendar.monthrange(now.year, now.month)[1]
        elapsed = (now - now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 86400
        if elapsed >= 3:  # projections in the first days are noise
            proj = month_total / elapsed * days_in
            if proj > float(mcap):
                ev.append(Event("monthly_projected", name,
                                f"{name}: on pace for {proj:.2f} {currency} this month (cap {float(mcap):.2f})",
                                f"monthly_projected:{name}:{month}", "info"))
    return ev


def should_send(event, sent, now):
    last = sent.get(event.dedupe_key)
    if last is None:
        return True
    if event.cooldown_hours <= 0:
        return False
    return (now - _parse(last)).total_seconds() / 3600 >= event.cooldown_hours


def mark_sent(event, sent, now):
    sent[event.dedupe_key] = _iso(now)
    # prune keys older than ~62 days so the state file stays small
    cutoff = now - dt.timedelta(days=62)
    for k in [k for k, v in sent.items() if _parse(v) < cutoff]:
        del sent[k]
