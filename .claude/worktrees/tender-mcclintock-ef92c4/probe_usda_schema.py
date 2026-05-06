#!/usr/bin/env python3
"""
USDA MPR API Schema Probe
=========================

Hits each report with NO query filter to discover the real column names.
Prints the field names (which includes whatever the date column is actually
called) so we can build a correct backfill.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from env_loader import load_env

import requests

REPORTS = {
    'LM_XB401': '2451',
    'LM_XB403': '2453',
    'LM_XB405': '2455',
    'LM_PK602': '2498',
}

BASE = 'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{slug}'


def probe():
    load_env()
    key = os.environ.get('USDA_MPR_API_KEY', '').strip()
    if not key:
        print("✗ USDA_MPR_API_KEY missing")
        sys.exit(1)

    tmp = SCRIPT_DIR / '.tmp'
    tmp.mkdir(exist_ok=True)

    for code, slug in REPORTS.items():
        url = BASE.format(slug=slug)
        print(f"\n→ {code}  GET {url}")
        r = requests.get(url, auth=(key, ''), timeout=60,
                         headers={'Accept': 'application/json', 'User-Agent': 'JL-MIC/1.0'})
        print(f"  status: {r.status_code}  ·  bytes: {len(r.content)}")
        if r.status_code != 200:
            print(f"  body: {r.text[:300]}")
            continue

        try:
            data = r.json()
        except Exception as e:
            print(f"  non-JSON: {e}")
            continue

        out = tmp / f'schema_{code}.json'
        # Save a trimmed copy — first 5 rows per list, max 300KB
        trimmed = _trim(data, max_list=5)
        out.write_text(json.dumps(trimmed, indent=2)[:300_000], encoding='utf-8')

        # Report structure + column names
        _describe(data, prefix='  ')

        print(f"  → trimmed schema saved: {out.name}")


def _trim(obj, max_list=5, depth=0):
    if depth > 6:
        return '...'
    if isinstance(obj, list):
        return [_trim(x, max_list, depth+1) for x in obj[:max_list]]
    if isinstance(obj, dict):
        return {k: _trim(v, max_list, depth+1) for k, v in obj.items()}
    return obj


def _describe(obj, prefix=''):
    if isinstance(obj, dict):
        keys = list(obj.keys())
        print(f"{prefix}top-level dict keys ({len(keys)}): {keys[:12]}")
        # If any value is a list of dicts, show its column names
        for k, v in obj.items():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                cols = list(v[0].keys())
                print(f"{prefix}  '{k}' → list of {len(v)} dicts; first-row columns ({len(cols)}):")
                for c in cols:
                    sample = v[0][c]
                    sample_str = str(sample)[:60]
                    print(f"{prefix}    - {c}: {type(sample).__name__} · sample={sample_str!r}")
                break
            elif isinstance(v, dict):
                inner = list(v.keys())[:12]
                print(f"{prefix}  '{k}' → dict with keys {inner}")
    elif isinstance(obj, list):
        print(f"{prefix}top-level list of {len(obj)} items")
        if obj and isinstance(obj[0], dict):
            cols = list(obj[0].keys())
            print(f"{prefix}  first-row columns ({len(cols)}):")
            for c in cols:
                sample = obj[0][c]
                sample_str = str(sample)[:60]
                print(f"{prefix}    - {c}: {type(sample).__name__} · sample={sample_str!r}")


if __name__ == '__main__':
    probe()
