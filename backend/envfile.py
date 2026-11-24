"""
envfile.py - loads settings from a `.env` file in the project folder (next to README.md).

Why: one file for every setting (Groq key, admin token, database, limits) instead of several key files.
Copy `.env.example` to `.env` and fill it in. `.env` is in .gitignore - never commit it.

Rules (same as most .env tools):
  - KEY=value per line; blank lines and lines starting with # are ignored; quotes around the value are removed
  - a variable already set in the real environment WINS (so Render / CI settings are never overridden by a file)
  - no extra packages needed

Imported first thing by app.py, before any module reads os.environ.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(path=None):
    """Read .env into os.environ (without overriding existing values). Returns the keys it set.
    VR_RAG_NO_ENVFILE=1 switches it off (the test suite uses this, so a developer's .env never changes test results)."""
    if os.environ.get("VR_RAG_NO_ENVFILE") == "1" and path is None:
        return []
    f = Path(path) if path else ROOT / ".env"
    if not f.is_file():
        return []
    added = []
    for raw in f.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:                      # inline comment: KEY=value  # note
            value = value.split(" #", 1)[0].rstrip()
        if key and value and key not in os.environ:
            os.environ[key] = value
            added.append(key)
    return added


LOADED = load()
