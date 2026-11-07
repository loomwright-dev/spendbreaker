"""Command line entry point. Designed for cron / systemd timers (one pass, then exit)."""
import argparse
import datetime as dt
import json
import os
import sys

from . import __version__
from .alerts import build_sinks
from .config import ConfigError, load
from .engine import Event
from .http import Client
from .runner import run_once


def main(argv=None):
    ap = argparse.ArgumentParser(prog="spendbreaker", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, hlp in (("check", "poll providers once, alert, optionally break"),
                      ("validate", "validate config and env vars, no network"),
                      ("test-alert", "send one test notification to configured sinks")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("-c", "--config", default=os.environ.get("SPENDBREAKER_CONFIG", "spendbreaker.yaml"))
        if name == "check":
            p.add_argument("--state", help="override state_file")
            p.add_argument("--dry-run", action="store_true", help="no state writes, breaks only simulated")
            p.add_argument("--no-break", action="store_true", help="never run break actions this pass")
            p.add_argument("--json", action="store_true", help="print machine-readable report")
    a = ap.parse_args(argv)
    try:
        cfg = load(a.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if a.cmd == "validate":
        from .providers import REGISTRY
        missing = []
        for name, pc in cfg["providers"].items():
            var = pc.get("key_env") or REGISTRY[name].default_key_env
            if not os.environ.get(var):
                missing.append(f"{name}: {var}")
        _, sink_errors = build_sinks(cfg["alerts"], Client(), os.environ)
        print(f"providers: {', '.join(cfg['providers'])}")
        print(f"break.enabled: {cfg['break']['enabled']}")
        for m in missing:
            print(f"missing env var -> {m}")
        for e in sink_errors:
            print(f"alert sink -> {e}")
        return 0 if not missing and not sink_errors else 1

    if a.cmd == "test-alert":
        sinks, errs = build_sinks(cfg["alerts"], Client(), os.environ)
        for e in errs:
            print(e, file=sys.stderr)
        ev = Event("test", "spendbreaker", "test alert - if you can read this, alerts work", "test", "info")
        for s in sinks:
            try:
                s.send(ev)
            except Exception as e:  # noqa: BLE001
                errs.append(f"{s.name}: {e}")
                print(f"{s.name}: {e}", file=sys.stderr)
        return 1 if errs else 0

    rep = run_once(cfg, now=dt.datetime.now(dt.timezone.utc), state_path=a.state,
                   dry_run=a.dry_run, no_break=a.no_break, stream=sys.stderr if a.json else sys.stdout)
    if a.json:
        print(json.dumps(rep, indent=1, default=str))
    else:
        for n, s in rep["snapshots"].items():
            est = " (estimate)" if s["estimated"] else ""
            r = "n/a" if s["hourly_rate"] is None else f"{s['hourly_rate']:.2f}/h"
            print(f"{n:<11} today {s['day']:>10.2f}  month {s['month']:>10.2f} {s['currency']}  rate {r}{est}")
        for b in rep["breaks"]:
            print(f"break: {b}")
        for e in rep["errors"]:
            print(f"error: {e}", file=sys.stderr)
    return 3 if rep["errors"] else 0
