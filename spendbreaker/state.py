"""Tiny JSON state file: samples per provider, alert de-duplication, breaks done."""
import json
import os
import tempfile

MAX_SAMPLES = 24 * 40


def load(path):
    path = os.path.expanduser(path)
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except FileNotFoundError:
        st = {}
    except (OSError, ValueError):
        # Corrupt state must not stop alerting; start fresh but keep the bad file.
        os.replace(path, path + ".corrupt")
        st = {}
    st.setdefault("providers", {})
    st.setdefault("sent", {})
    st.setdefault("breaks", {})
    return st


def save(path, st):
    path = os.path.expanduser(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    for p in st["providers"].values():
        p["samples"] = p.get("samples", [])[-MAX_SAMPLES:]
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".state.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1, sort_keys=True)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
