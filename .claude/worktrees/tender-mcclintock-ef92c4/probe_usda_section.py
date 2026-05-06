#!/usr/bin/env python3
"""
USDA MPR Section Probe — figure out how to request a specific section
so we get actual price rows (not just Summary metadata).
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

load_env()
KEY = os.environ['USDA_MPR_API_KEY'].strip()

SLUG = 2451     # LM_XB401
SECTION = 'National'

# Common ways USDA MPR v1.1 exposes sections
ATTEMPTS = [
    ('URL path', f'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{SLUG}/{SECTION}', None),
    ('section query',  f'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{SLUG}', {'section': SECTION}),
    ('report_section query', f'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{SLUG}', {'report_section': SECTION}),
    ('q param eq', f'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{SLUG}', {'q': f'report_section={SECTION}'}),
    ('q param colon', f'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{SLUG}', {'q': f'report_section:{SECTION}'}),
]

tmp = SCRIPT_DIR / '.tmp'
tmp.mkdir(exist_ok=True)

for label, url, params in ATTEMPTS:
    print(f'\n▸ {label}')
    print(f'  GET {url}')
    if params: print(f'  params: {params}')
    try:
        r = requests.get(url, auth=(KEY, ''), params=params, timeout=60,
                         headers={'Accept': 'application/json', 'User-Agent': 'JL-MIC/1.0'})
    except requests.exceptions.RequestException as e:
        print(f'  ✗ network: {e}')
        continue
    print(f'  status: {r.status_code}  ·  bytes: {len(r.content)}')
    if r.status_code != 200:
        print(f'  body: {r.text[:200]}')
        continue

    try:
        data = r.json()
    except Exception:
        print(f'  non-JSON: {r.text[:200]}')
        continue

    # Inspect: does results contain price-looking fields?
    results = data.get('results', [])
    section = data.get('reportSection', '?')
    print(f'  reportSection: {section!r}  ·  results: {len(results)} rows')
    if results:
        cols = list(results[0].keys())
        price_hints = [c for c in cols if any(w in c.lower() for w in ('price','wtd','avg','weighted','range','load','low','high','trade'))]
        print(f'  {len(cols)} columns; price-looking: {price_hints}')
        # Save trimmed sample
        out = tmp / f'section_{label.replace(" ","_")}.json'
        out.write_text(json.dumps({'reportSection': section, 'sample_rows': results[:3]}, indent=2), encoding='utf-8')
        print(f'  → saved sample: {out.name}')
        if price_hints:
            print(f'  ✓ LOOKS LIKE PRICE DATA — this is the right access method!')
            break
    else:
        print('  (empty results)')
