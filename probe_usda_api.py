#!/usr/bin/env python3
"""
USDA MPR DataMart API Probe
===========================

Runs a small test call against the USDA MPR API using the key in .env,
reports what came back, and saves the raw JSON to .tmp/probe_*.json for
inspection. This is a diagnostic tool, not a backfill — it only pulls
~5 days of recent data to verify auth + structure.

Run:
    python probe_usda_api.py

Safe output: prints status codes, response shape, row counts, and the
FIRST 3 field names. Never prints the API key. Never prints more than 2
sample rows.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Force UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from env_loader import load_env

try:
    import requests
except ImportError:
    print("Missing dependency: python -m pip install requests")
    sys.exit(1)


REPORTS = {
    'LM_XB401': '2451',
    'LM_XB403': '2453',
    'LM_XB405': '2455',
    'LM_PK602': '2498',
}

# USDA MPR DataMart common endpoint patterns. Probe tries them in order.
CANDIDATE_BASES = [
    'https://marsapi.ams.usda.gov/services/v1.2/reports/{slug}',
    'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{slug}',
    'https://mpr.datamart.ams.usda.gov/services/v1.2/reports/{slug}',
]


def _mask(val: str, keep: int = 4) -> str:
    if not val:
        return '(empty)'
    if len(val) <= keep * 2:
        return '*' * len(val)
    return f'{val[:keep]}…{val[-keep:]}  ({len(val)} chars total)'


def probe():
    load_env()
    key = os.environ.get('USDA_MPR_API_KEY', '').strip()
    if not key:
        print("✗ USDA_MPR_API_KEY not found in .env or environment")
        sys.exit(1)
    print(f"✓ API key loaded · {_mask(key)}")

    tmp = SCRIPT_DIR / '.tmp'
    tmp.mkdir(exist_ok=True)

    # Probe just one report with a small date window
    slug = REPORTS['LM_XB401']
    code = 'LM_XB401'
    end = date.today()
    start = end - timedelta(days=14)

    # USDA MPR uses Basic Auth: API key as username, blank password
    auth = (key, '')

    for base in CANDIDATE_BASES:
        url = base.format(slug=slug)
        # Common MPR query formats
        queries = [
            {'q': f'report_begin_date={start.strftime("%m/%d/%Y")}:{end.strftime("%m/%d/%Y")}'},
            {'q': f'published_date={start.isoformat()}:{end.isoformat()}'},
            {},   # just the base endpoint
        ]
        for params in queries:
            print(f"\n→ GET {url}")
            if params:
                print(f"   params: {params}")
            try:
                r = requests.get(url, auth=auth, params=params, timeout=30,
                                 headers={'Accept': 'application/json', 'User-Agent': 'JackLinks-MIC-Probe/1.0'})
            except requests.exceptions.RequestException as e:
                print(f"   ✗ network error: {type(e).__name__}: {e}")
                continue

            print(f"   status: {r.status_code}  ·  content-type: {r.headers.get('content-type','?')}  ·  bytes: {len(r.content)}")

            if r.status_code == 401:
                print("   ✗ 401 Unauthorized — key format or auth scheme is wrong. Move to next candidate.")
                break    # don't retry same base with different query
            if r.status_code == 404:
                print("   ✗ 404 Not Found — this endpoint/version is wrong. Try next candidate.")
                break
            if r.status_code >= 500:
                print("   ✗ server error. Try next candidate.")
                break
            if r.status_code != 200:
                print(f"   ⚠ unexpected status, skipping")
                continue

            # Got a 200 — save and summarize
            try:
                data = r.json()
            except json.JSONDecodeError:
                print("   ✗ not JSON; first 200 chars of body:")
                print("   " + r.text[:200].replace("\n", " "))
                continue

            out = tmp / f'probe_{code}.json'
            out.write_text(json.dumps(data, indent=2)[:500_000], encoding='utf-8')
            print(f"   ✓ 200 OK, JSON parsed, saved to {out.name}")

            # Structural summary (SAFE — prints shape, not content)
            if isinstance(data, dict):
                top_keys = list(data.keys())[:8]
                print(f"   top-level keys: {top_keys}")
                for k in top_keys:
                    v = data[k]
                    if isinstance(v, list):
                        print(f"     · {k}: list of {len(v)} items")
                        if v and isinstance(v[0], dict):
                            fields = list(v[0].keys())[:12]
                            print(f"       first-row fields ({len(v[0])} total): {fields}")
                    elif isinstance(v, dict):
                        print(f"     · {k}: dict with {len(v)} keys")
                    else:
                        print(f"     · {k}: {type(v).__name__}")
            elif isinstance(data, list):
                print(f"   top-level: list of {len(data)} items")
                if data and isinstance(data[0], dict):
                    fields = list(data[0].keys())[:12]
                    print(f"   first-row fields ({len(data[0])} total): {fields}")

            print(f"\n✓ PROBE SUCCESS · working endpoint: {url}")
            print(f"  Full response saved to: {out}")
            print(f"  Paste the console output above (NOT the saved file) back to Claude.")
            return

    print("\n✗ No candidate endpoint returned a usable 200 response.")
    print("  Check:")
    print("    1. Is the API key active? Log into mymarketnews.ams.usda.gov and verify.")
    print("    2. Does the key need a different header format? Some USDA endpoints use 'Authorization: Bearer ...' instead of Basic Auth.")
    print("    3. Is there a CAPTCHA / activation email pending?")
    sys.exit(1)


if __name__ == '__main__':
    probe()
