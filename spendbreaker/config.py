"""Load and validate the single YAML config. Secrets are never allowed inline."""
import re

import yaml

from .providers import REGISTRY


class ConfigError(Exception):
    pass


SECRET_KEY_RE = re.compile(r"(api_?key|token|secret|password)$", re.I)
SECRET_VALUE_RE = re.compile(r"^(sk-|sk_|xox[bp]-|AKIA[0-9A-Z]{12}|ghp_|vercel_|\d{6,}:[A-Za-z0-9_-]{30,})")

DEFAULTS = {
    "state_file": "~/.local/state/spendbreaker/state.json",
    "warn_at": 0.8,
    "anomaly": {"enabled": True, "multiplier": 3.0, "window_samples": 168,
                "min_samples": 6, "min_hourly_amount": 1.0, "cooldown_hours": 6},
    "alerts": {"stdout": True},
    "break": {"enabled": False},
    "providers": {},
}


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _reject_inline_secrets(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else str(k)
            if SECRET_KEY_RE.search(str(k)) and not str(k).endswith("_env") and isinstance(v, str) and v:
                raise ConfigError(f"{p}: secrets must not be in the config file; use an env var "
                                  f"and reference its NAME via '{k}_env'")
            if isinstance(v, str) and SECRET_VALUE_RE.match(v):
                raise ConfigError(f"{p}: value looks like a secret; put it in an env var instead")
            _reject_inline_secrets(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _reject_inline_secrets(v, f"{path}[{i}]")


def _num(v, where, allow_none=True):
    if v is None and allow_none:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
        raise ConfigError(f"{where}: expected a non-negative number, got {v!r}")
    return float(v)


def parse(raw):
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")
    _reject_inline_secrets(raw)
    cfg = _merge(DEFAULTS, raw)
    if not isinstance(cfg["break"].get("enabled"), bool):
        raise ConfigError("break.enabled must be true or false")
    _num(cfg["warn_at"], "warn_at")
    for k in ("daily_cap", "monthly_cap"):
        _num((cfg.get("total") or {}).get(k), f"total.{k}")
    provs = {}
    for name, pc in (cfg["providers"] or {}).items():
        pc = dict(pc or {})
        if name not in REGISTRY:
            raise ConfigError(f"providers.{name}: unknown provider (known: {', '.join(sorted(REGISTRY))})")
        if pc.get("enabled", True) is False:
            continue
        for k in ("daily_cap", "monthly_cap", "min_interval_minutes"):
            _num(pc.get(k), f"providers.{name}.{k}")
        br = pc.get("break")
        if br:
            if not isinstance(br, dict) or "action" not in br:
                raise ConfigError(f"providers.{name}.break: needs an 'action'")
            if br["action"] not in REGISTRY[name].break_actions:
                raise ConfigError(f"providers.{name}.break.action {br['action']!r} not supported; "
                                  f"supported: {list(REGISTRY[name].break_actions) or 'none'}")
            br.setdefault("on", ["daily_breach", "monthly_breach"])
        provs[name] = pc
    if not provs:
        raise ConfigError("no providers enabled")
    cfg["providers"] = provs
    return cfg


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except OSError as e:
        raise ConfigError(f"cannot read config {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e
    return parse(raw)
