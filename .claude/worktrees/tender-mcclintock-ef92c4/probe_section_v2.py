#!/usr/bin/env python3
"""
Probe v2 — URL-path section endpoint + date filter, with longer timeout.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from env_loader import load_env
import requests

load_env()
KEY = os.environ['USDA_MPR_API_KEY'].strip()

SLUG = 2451
SECTION = 'National'

# Path-based with date filter to keep response small
URL = f'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{SLUG}/{quote(SECTION)}'
PARAMS = {'q': 'report_date=04/17/2026'}

print(f'▸ GET {URL}')
print(f'  params: {PARAMS}')
print(f'  (timeout 300s)')

try:
    r = requests.get(URL, auth=(KEY, ''), params=PARAMS, timeout=300,
                     headers={'Accept': 'application/json', 'User-Agent': 'JL-MIC/1.0'})
except requests.exceptions.RequestException as e:
    print(f'✗ {type(e).__name__}: {e}')
    sys.exit(1)

print(f'  status: {r.status_code}  ·  bytes: {len(r.content)}')
if r.status_code != 200:
    print(f'  body: {r.text[:400]}')
    sys.exit(1)

try:
    data = r.json()
except Exception:
    print(f'  non-JSON body: {r.text[:400]}')
    sys.exit(1)

# Might be dict or string (error)
if isinstance(data, str):
    print(f'  ⚠ API returned string: {data[:300]}')
    sys.exit(1)

section = data.get('reportSection', '?')
results = data.get('results', [])
print(f'  reportSection: {section!r}')
print(f'  results: {len(results)} rows')

if results:
    cols = list(results[0].keys())
    price_hints = [c for c in cols if any(w in c.lower() for w in ('price','wtd','avg','weighted','range','load','low','high','trade','volume','pound'))]
    print(f'  {len(cols)} columns · price-looking: {price_hints}')
    print(f'\n  FIRST ROW (all fields):')
    for k, v in results[0].items():
        sample_str = str(v)[:80]
        print(f'    {k}: {type(v).__name__} · {sample_str!r}')

    out = SCRIPT_DIR / '.tmp' / f'section_v2_{SECTION}.json'
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        'reportSection': section,
        'total_rows': len(results),
        'sample_rows': results[:5]
    }, indent=2), encoding='utf-8')
    print(f'\n  → saved sample: {out.name}')

    if price_hints:
        print(f'\n✓ REAL PRICE DATA FOUND — backfill strategy confirmed!')
