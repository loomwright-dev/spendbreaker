"""One polling pass: fetch -> evaluate -> alert -> (optionally) break -> save state."""
import datetime as dt
import os

from . import engine
from . import state as state_mod
from .alerts import build_sinks
from .http import Client
from .providers import REGISTRY
from .providers.base import Snapshot


def _min_interval(name, pcfg):
    v = pcfg.get("min_interval_minutes")
    if v is None:
        v = getattr(REGISTRY[name], "default_min_interval_minutes", 0)
    return float(v)


def run_once(cfg, now=None, http=None, env=None, state_path=None, dry_run=False, no_break=False,
             provider_factory=None, stream=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    env = os.environ if env is None else env
    http = http or Client()
    state_path = state_path or cfg["state_file"]
    st = state_mod.load(state_path)
    sinks, sink_errors = build_sinks(cfg["alerts"], http, env, stream)
    report = {"snapshots": {}, "events": [], "sent": [], "breaks": [], "errors": list(sink_errors)}
    events = []
    totals = {"day": 0.0, "month": 0.0, "currencies": set()}

    for name, pcfg in cfg["providers"].items():
        pstate = st["providers"].setdefault(name, {})
        last = pstate.get("last_fetch")
        if last and (now - engine._parse(last)).total_seconds() / 60 < _min_interval(name, pcfg):
            s = pstate.get("last_snapshot") or {}
            snap = Snapshot(name, s.get("day_total", 0.0), s.get("month_total", 0.0), s.get("currency", "USD"),
                            s.get("estimated", False), "cached (min_interval_minutes)")
            # A cached value must only count towards today if it was taken today.
            if s.get("date") != now.strftime("%Y-%m-%d"):
                snap.day_total = 0.0
            rate = None
        else:
            try:
                prov = (provider_factory or (lambda n, c: REGISTRY[n](c, http, env)))(name, pcfg)
                snap = prov.fetch(now)
            except Exception as e:  # noqa: BLE001 - one bad provider must not silence the others
                msg = f"{name}: fetch failed: {e}"
                report["errors"].append(msg)
                events.append(engine.Event("fetch_error", name, msg, f"fetch_error:{name}", "warn", 6))
                continue
            pstate["last_fetch"] = engine._iso(now)
            pstate["last_snapshot"] = {"day_total": snap.day_total, "month_total": snap.month_total,
                                       "currency": snap.currency, "estimated": snap.estimated,
                                       "date": now.strftime("%Y-%m-%d")}
            rate = engine.record_sample(pstate, snap, now)
        report["snapshots"][name] = {"day": snap.day_total, "month": snap.month_total, "currency": snap.currency,
                                     "estimated": snap.estimated, "hourly_rate": rate, "note": snap.note}
        totals["day"] += snap.day_total
        totals["month"] += snap.month_total
        totals["currencies"].add(snap.currency)
        events += engine.cap_events(name, snap.day_total, snap.month_total, pcfg, cfg["warn_at"], now, snap.currency)
        acfg = dict(cfg["anomaly"], **(pcfg.get("anomaly") or {}))
        if name == "aws" and "anomaly" not in pcfg:
            acfg["enabled"] = False  # CE data lags ~24h; hourly anomaly would be noise
        ev = engine.anomaly_event(name, pstate, rate, acfg, now, snap.currency)
        if ev:
            events.append(ev)

    tcfg = cfg.get("total") or {}
    if tcfg and report["snapshots"]:
        cur = "/".join(sorted(totals["currencies"]))
        events += engine.cap_events("total", totals["day"], totals["month"], tcfg, cfg["warn_at"], now, cur)

    # Break actions: only with the global switch ON, a per-provider action, a matching event, and not dry-run.
    break_on = cfg["break"].get("enabled") is True and not no_break
    for ev in list(events):
        pcfg = cfg["providers"].get(ev.provider) or {}
        bcfg = pcfg.get("break")
        if not bcfg or ev.kind not in bcfg.get("on", []):
            continue
        bkey = f"break:{ev.dedupe_key}"
        if bkey in st["breaks"]:
            continue
        if not break_on:
            report["breaks"].append(f"{ev.provider}: break configured but disabled (break.enabled=false or --no-break)")
            continue
        try:
            prov = (provider_factory or (lambda n, c: REGISTRY[n](c, http, env)))(ev.provider, pcfg)
            results = prov.do_break(bcfg, dry_run=dry_run)
            report["breaks"] += results
            if not dry_run:
                st["breaks"][bkey] = engine._iso(now)
            events.append(engine.Event("break", ev.provider, "; ".join(results), bkey, "critical"))
        except Exception as e:  # noqa: BLE001 - report every break failure loudly
            msg = f"{ev.provider}: BREAK FAILED: {e}"
            report["errors"].append(msg)
            events.append(engine.Event("break", ev.provider, msg, bkey + ":failed", "critical", 1))

    for ev in events:
        report["events"].append({"kind": ev.kind, "provider": ev.provider, "message": ev.message})
        if not engine.should_send(ev, st["sent"], now):
            continue
        # Mark as sent only if a push sink delivered it, so a Telegram/ntfy outage or missing push
        # credentials means we retry next pass instead of going silent. Stdout alone never counts.
        remote = [s for s in sinks if s.name != "stdout"]
        if not remote:
            report["errors"].append("no push alert channel available (check Telegram/ntfy settings); "
                                    "alerts are only printed and will repeat every run")
        delivered = False
        for s in sinks:
            try:
                s.send(ev)
                delivered = delivered or s in remote
            except Exception as e:  # noqa: BLE001
                report["errors"].append(f"alert via {s.name} failed: {e}")
        if delivered and not dry_run:
            engine.mark_sent(ev, st["sent"], now)
            report["sent"].append(ev.dedupe_key)

    if not dry_run:
        state_mod.save(state_path, st)
    return report
