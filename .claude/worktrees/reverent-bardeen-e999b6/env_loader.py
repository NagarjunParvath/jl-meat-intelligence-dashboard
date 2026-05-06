"""
Tiny .env file loader — no third-party dependency.

Reads KEY=value pairs from ./.env (next to this script) into os.environ.
Skips blank lines and `#` comments. Does not overwrite pre-existing env vars
(so Windows user-level env variables take precedence if set).

Usage:
    from env_loader import load_env
    load_env()
    import os
    key = os.environ['USDA_MPR_API_KEY']
"""
from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path | None = None) -> dict:
    """Load KEY=VALUE pairs from .env into os.environ. Returns loaded keys (names only)."""
    if path is None:
        path = Path(__file__).resolve().parent / '.env'
    path = Path(path)
    loaded = {}
    if not path.exists():
        return loaded

    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        # Strip optional surrounding quotes (user may have added them)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = len(value)   # store length only, never the value
    return loaded
