#!/usr/bin/env python3
"""
USDA MPR API Health Check
=========================

Runs 6 independent probes and reports a verdict on whether USDA is in
maintenance, having a partial outage, or only failing for your account.

    python probe_usda_health.py
"""
from __future__ import annotations

import os
import sys
import time
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
KEY = os.environ.get('USDA_MPR_API_KEY', '').strip()

UA = {'User-Agent': 'JL-MIC-HealthCheck/1.0', 'Accept': 'application/json'}

def probe(label, url, auth=None, expect_200=False, params=None):
    t0 = time.time()
    try:
        r = requests.get(url, auth=auth, params=params, timeout=30, headers=UA,
                         allow_redirects=True)
        dt = (time.time() - t0) * 1000
        tag = '✓' if (r.status_code == 200 if expect_200 else r.status_code < 500) else '✗'
        print(f'  {tag}  [{r.status_code}] {label:42s} {dt:>6.0f}ms')
        return r.status_code
    except requests.exceptions.Timeout:
        print(f'  ✗  [TIMEOUT] {label}')
        return -1
    except requests.exceptions.RequestException as e:
        print(f'  ✗  [NETWORK] {label}  —  {type(e).__name__}: {e}')
        return -2


print('━' * 72)
print(' USDA MPR API Health Check ·', time.strftime('%Y-%m-%d %H:%M:%S'))
print('━' * 72)

# 1. DNS + connectivity
print('\n[1] Basic reachability')
usda_main     = probe('USDA.gov main site',               'https://www.usda.gov')
ams_site      = probe('ams.usda.gov',                     'https://www.ams.usda.gov')
portal        = probe('mymarketnews.ams.usda.gov portal', 'https://mymarketnews.ams.usda.gov')

# 2. API server (no auth — expect 401 if server healthy)
print('\n[2] MPR API reachability (no auth — 401 = healthy)')
noauth = probe('MPR /services/v1.1/reports/2451 (no auth)',
               'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/2451')

# 3. Authenticated calls across all 4 reports we use
print('\n[3] Authenticated calls across all 4 reports')
if not KEY:
    print('  ⚠ USDA_MPR_API_KEY missing from .env — skipping auth probes')
else:
    slugs = {'LM_XB401': 2451, 'LM_XB403': 2453, 'LM_XB405': 2455, 'LM_PK602': 2498}
    auth_codes = []
    for code, slug in slugs.items():
        c = probe(f'{code}  /reports/{slug}',
                  f'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/{slug}',
                  auth=(KEY, ''))
        auth_codes.append(c)

    # 4. Single-day section call (the one backfill actually uses)
    print('\n[4] Section call (same pattern backfill uses)')
    probe(f'LM_XB401 /reports/2451/National + date filter',
          'https://mpr.datamart.ams.usda.gov/services/v1.1/reports/2451/National',
          auth=(KEY, ''),
          params={'q': 'report_date=04/17/2026'})

# 5. Verdict
print('\n' + '━' * 72)
print(' VERDICT')
print('━' * 72)

def verdict():
    # Check AUTH results first (most diagnostic signal)
    auth_list = globals().get('auth_codes') or []
    if auth_list:
        if all(c == 200 for c in auth_list):
            return 'API FULLY HEALTHY — you can run backfill now.'
        if all(c >= 500 for c in auth_list):
            return ('MPR API BACKEND OUTAGE — every authenticated call returns 500. '
                    'Servers are responding (not your network, not your key) but the '
                    'MPR service is failing. Retry on a business day.')
        if any(c == 401 or c == 403 for c in auth_list):
            return ('AUTH REJECTED — your API key is invalid, expired, or rate-limited. '
                    'Log into mymarketnews.ams.usda.gov to check key status.')
        if any(c == 200 for c in auth_list) and any(c >= 500 for c in auth_list):
            return 'PARTIAL OUTAGE — some reports work, others don\'t. Safe to backfill working ones only.'

    # Fallback — network/DNS issues (only if we couldn\'t even reach AMS)
    reachable_count = sum(1 for c in [usda_main, ams_site, portal, noauth] if c >= 200)
    if reachable_count == 0:
        return 'LOCAL NETWORK ISSUE — cannot reach any USDA endpoint. Check internet / DNS / firewall.'
    if ams_site < 0 and portal < 0 and (noauth < 0 or noauth >= 500):
        return 'USDA-WIDE OUTAGE — multiple USDA subdomains unreachable. Wait.'
    if noauth >= 500:
        return ('MPR API SERVER ERROR — API layer returning 500 without auth. '
                'Backend issue. Retry business day.')

    return 'MIXED SIGNALS — review the status codes above manually.'

print(f'\n  {verdict()}\n')
