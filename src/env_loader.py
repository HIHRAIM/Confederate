"""Load KEY=VALUE pairs from the .env file next to this module - standard library only.

Values already present in the process environment take precedence: the file
only fills in variables that are missing. Blank lines and lines starting
with '#' are skipped; a missing .env file is not an error.
"""
import os

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

def load_env(path=_ENV_PATH):
    try:
        with open(path, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)
