#!/usr/bin/env python3
"""
USDA MPR History Backfill — v2 (all four reports)
==================================================

Pulls multi-year daily price history from USDA MPR DataMart for every
cut the dashboard tracks, writes per-cut series to data/history.json.

Reports covered:
    LM_XB401  Beef Trim                    → 85CL, 50CL, 65CL, 81CL
    LM_XB405  Cow Cutout & 100VL           → insides, flats, eyes, lean_90
    LM_PK602  Pork FOB Plant               → 72% / 42% trim, belly 13-17#, loin primal
    LM_XB403  Boxed Beef Cutout            → Choice/Select cutout, primals

Section auto-discovery
----------------------
For each report we first query the default Summary endpoint to learn
reportSections, then pull each listed section and match rows by item_desc.
Cuts we can't match are silently skipped (logged at --verbose).

Idempotent — merges new rows into existing history.json by date, never
deletes anything.

Usage
-----
    python backfill_history.py                 # 5 years, all reports
    python backfill_history.py --years 3       # shorter
    python backfill_history.py --only XB405    # one report at a time
    python backfill_history.py --verbose       # per-row match debugging
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, timedelta
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

try:
    import requests
except ImportError:
    print("Missing dependency: python -m pip install requests")
    sys.exit(1)

DATA_DIR = SCRIPT_DIR / 'data'
HISTORY_PATH = DATA_DIR / 'history.json'
API_BASE = 'https://mpr.datamart.ams.usda.gov/services/v1.1/reports'

# ──────────────────────────────────────────────────────────────────────
# REPORT & CUT CONFIG
# Each cut: list of (section_filter_regex, item_desc_regex) pairs.
# Matching: any section whose name matches AND any row whose item_desc
# matches. First match wins. Case-insensitive.
# ──────────────────────────────────────────────────────────────────────

REPORTS = {
    'LM_XB401': {
        'slug': '2451',
        'cuts': {
            '85CL': [(r'National', r'Chemical Lean.*Fresh.*85')],
            '50CL': [(r'National', r'Chemical Lean.*Fresh.*50')],
            '65CL': [(r'National', r'Chemical Lean.*Fresh.*65')],
            '81CL': [(r'National', r'Chemical Lean.*Fresh.*81')],
        },
    },
    'LM_XB405': {
        'slug': '2455',
        'cuts': {
            'insides':   [(r'100.*Lean.*Item|Lean Item', r'(100% lean inside|inside round)')],
            'flats':     [(r'100.*Lean.*Item|Lean Item', r'(flats and eyes|flats & eyes)')],
            'eyes':      [(r'Boner|Breaker|BONER', r'(eye of round|171C)')],
            'lean_90':   [(r'100.*Lean.*Item|Lean Item', r'^90% lean$')],
            'spb':       [(r'100.*Lean.*Item|Lean Item', r'(S\.P\.B|SPB)')],
        },
    },
    'LM_PK602': {
        'slug': '2498',
        'cuts': {
            'pork_trim_72':     [(r'.*', r'72%\s*Trim\s*Combo|72%\s*Combo')],
            'pork_trim_42':     [(r'.*', r'42%\s*Trim\s*Combo|42%\s*Combo')],
            'pork_belly_13_17': [(r'Bell|Derind|.*', r'(Derind Belly 13-?17|Belly.*13-?17)')],
            'pork_loin':        [(r'Primal|Cutout|.*',  r'^Loin$')],   # primal-cutout loin value
        },
    },
    'LM_XB403': {
        'slug': '2453',
        'cuts': {
            'choice_cutout': [(r'Cutout|Summary|.*',  r'Choice Cutout|600-?900 Choice')],
            'select_cutout': [(r'Cutout|Summary|.*',  r'Select Cutout|600-?900 Select')],
            'ribeye_lipon':  [(r'Choice|Cuts',       r'ribeye.*lip-?on.*bn-?in')],
            'chuck_roll':    [(r'Choice|Cuts',       r'chuck\s*roll')],
            'strip_loin':    [(r'Choice|Cuts',       r'strip.*bnls|strip.*bon')],
            'top_inside':    [(r'Choice|Cuts',       r'top inside')],
            'eye_of_round_boxed': [(r'Choice|Cuts',  r'eye of round')],
        },
    },
}


# ──────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────

def _mask(v: str) -> str:
    return f'{v[:4]}…{v[-4:]}  ({len(v)} chars)' if v and len(v) >= 8 else '(empty)'


def _to_float(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s or s == '-' or s == '.00' or s == '0.00':
        return None
    try:
        return float(s.replace(',', ''))
    except ValueError:
        return None


def _to_int(s) -> int | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s or s == '-':
        return None
    try:
        return int(s.replace(',', ''))
    except ValueError:
        return None


def _parse_date(s: str) -> str | None:
    if not s:
        return None
    try:
        m, d, y = s.split('/')
        return f'{int(y):04d}-{int(m):02d}-{int(d):02d}'
    except Exception:
        return None


# Known section fallback — used when Summary endpoint 500s on section discovery.
# From USDA PDF structure inspection; safe to keep extending as we learn more.
SECTION_FALLBACK = {
    '2451': ['National', 'Central'],
    '2455': ['100% Lean Items', 'BONER/BREAKER', 'Boner/Breaker'],
    '2498': ['Daily National Carlot Pork Report', 'National Daily Pork Report - Afternoon',
             'Bellies', 'Trimmings', 'Loins'],
    '2453': ['Choice Cuts', 'Select Cuts', 'Current Cutout Values',
             'Composite Primal Values', 'Trimmings'],
}


def _fetch(key: str, url: str, params: dict, verbose: bool = False, retries: int = 3) -> dict | None:
    last_err = None
    for attempt in range(retries):
        if verbose:
            print(f'      GET {url}  ·  {params}  (try {attempt+1}/{retries})')
        try:
            r = requests.get(url, auth=(key, ''), params=params, timeout=300,
                             headers={'Accept': 'application/json',
                                      'User-Agent': 'JL-MIC-Backfill/2.0'})
        except requests.exceptions.RequestException as e:
            last_err = f'network: {e}'
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                print(f'      ✗ non-JSON: {r.text[:150]}')
                return None
            if isinstance(data, str):
                if verbose:
                    print(f'      ⚠ {data[:200]}')
                return None
            return data
        last_err = f'HTTP {r.status_code}: {r.text[:150]}'
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        # 4xx — not retrying
        print(f'      ✗ {last_err}')
        return None
    print(f'      ✗ {last_err} (after {retries} tries)')
    return None


def _discover_sections(key: str, slug: str, verbose: bool) -> list:
    """Fetch the Summary endpoint (no filter — more reliable) and return reportSections."""
    url = f'{API_BASE}/{slug}'
    data = _fetch(key, url, {}, verbose)
    if data:
        sections = data.get('reportSections') or []
        found = [s for s in sections if isinstance(s, str) and s.lower() != 'summary']
        if found:
            return found
    # Fallback to known section names for this slug
    fb = SECTION_FALLBACK.get(slug, [])
    if fb:
        print(f'  (using hardcoded section fallback: {fb})')
    return fb


def _section_matches_cut(section: str, patterns: list) -> list:
    """Return list of item_desc_regex for patterns whose section_filter matches."""
    matched = []
    for sec_re, item_re in patterns:
        if re.search(sec_re, section, re.IGNORECASE):
            matched.append(item_re)
    return matched


def _match_any(item_desc: str, item_patterns: list) -> bool:
    for p in item_patterns:
        if re.search(p, item_desc, re.IGNORECASE):
            return True
    return False


def _fetch_section_year(key: str, slug: str, section: str, start: date, end: date, verbose: bool = False) -> list:
    url = f'{API_BASE}/{slug}/{quote(section)}'
    params = {'q': f'report_date={start.strftime("%m/%d/%Y")}:{end.strftime("%m/%d/%Y")}'}
    data = _fetch(key, url, params, verbose)
    if not data:
        return []
    return data.get('results') or []


# ──────────────────────────────────────────────────────────────────────
# REPORT BACKFILL
# ──────────────────────────────────────────────────────────────────────

def backfill_report(key: str, report_code: str, cfg: dict, years: int, verbose: bool) -> dict:
    """Return {cut_key: [{date, price_cwt, ...}, ...]} for one report."""
    slug = cfg['slug']
    cuts = cfg['cuts']
    print(f'\n== {report_code}  (slug {slug}) ==')

    sections = _discover_sections(key, slug, verbose)
    if not sections:
        print(f'  ⚠ could not discover sections — skipping')
        return {}
    print(f'  sections discovered: {sections}')

    collected: dict[str, dict[str, dict]] = {k: {} for k in cuts}
    today = date.today()
    start = today.replace(year=today.year - years)

    for section in sections:
        # Which cuts might this section serve?
        relevant_cuts = {}
        for cut_key, patterns in cuts.items():
            item_regexes = _section_matches_cut(section, patterns)
            if item_regexes:
                relevant_cuts[cut_key] = item_regexes
        if not relevant_cuts:
            if verbose:
                print(f'  (skip {section!r} — no cuts match)')
            continue

        print(f'  → section {section!r}  targets: {list(relevant_cuts.keys())}')

        # Year-chunk the pull
        chunk_start = start
        while chunk_start < today:
            chunk_end = chunk_start.replace(year=chunk_start.year + 1) - timedelta(days=1)
            if chunk_end > today:
                chunk_end = today
            t0 = time.time()
            rows = _fetch_section_year(key, slug, section, chunk_start, chunk_end, verbose)
            dt = time.time() - t0
            print(f'    chunk {chunk_start.isoformat()}→{chunk_end.isoformat()}  {len(rows):>5} rows ({dt:.1f}s)')

            matched_count = 0
            for row in rows:
                item_desc = (row.get('item_desc') or '').strip()
                iso = _parse_date(row.get('report_date', ''))
                if not iso:
                    continue
                wa = _to_float(row.get('price_range_avg')) or _to_float(row.get('weighted_avg'))
                if wa is None or wa <= 0:
                    continue
                for cut_key, item_patterns in relevant_cuts.items():
                    if _match_any(item_desc, item_patterns):
                        collected[cut_key][iso] = {
                            'date': iso,
                            'price_cwt': wa,
                            'range_low': _to_float(row.get('price_range_low')),
                            'range_high': _to_float(row.get('price_range_high')),
                            'trades': _to_int(row.get('number_trades')),
                            'pounds': _to_int(row.get('total_pounds')),
                        }
                        matched_count += 1
                        break
            if verbose:
                print(f'      matched {matched_count} rows to cuts')

            chunk_start = chunk_end + timedelta(days=1)

    # Flatten dict-by-date → sorted list
    out = {}
    for k, by_date in collected.items():
        series = sorted(by_date.values(), key=lambda x: x['date'])
        out[k] = series
        if series:
            print(f'  {k:20s}  {len(series):>5} obs  {series[0]["date"]} → {series[-1]["date"]}')
        else:
            print(f'  {k:20s}  0 obs  (no matches — check regexes)')

    return out


def _merge(new: dict, history_path: Path) -> None:
    existing = {}
    if history_path.exists():
        try:
            existing = json.loads(history_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            print('  ⚠ existing history.json invalid — starting fresh')

    for cut, rows in new.items():
        if not rows:
            continue
        by_date = {r['date']: r for r in existing.get(cut, []) if isinstance(r, dict) and r.get('date')}
        for r in rows:
            by_date[r['date']] = r
        existing[cut] = sorted(by_date.values(), key=lambda x: x['date'])

    history_path.parent.mkdir(exist_ok=True)
    history_path.write_text(json.dumps(existing, indent=2), encoding='utf-8')
    print(f'\n✓ merged → {history_path}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--years', type=int, default=5)
    ap.add_argument('--only', type=str, default=None, help='restrict to one report, e.g. XB405')
    ap.add_argument('--verbose', '-v', action='store_true')
    args = ap.parse_args()

    load_env()
    key = os.environ.get('USDA_MPR_API_KEY', '').strip()
    if not key:
        print('✗ USDA_MPR_API_KEY missing')
        sys.exit(1)
    print(f'✓ Key loaded · {_mask(key)}\n  Pulling {args.years} years of USDA MPR history')

    all_new = {}
    for code, cfg in REPORTS.items():
        if args.only and args.only.upper() not in code.upper():
            continue
        report_data = backfill_report(key, code, cfg, args.years, args.verbose)
        all_new.update(report_data)

    _merge(all_new, HISTORY_PATH)
    print('\nNext: python forecast_cuts.py  (quantile-GBM auto-engages on cuts that passed backtest)')


if __name__ == '__main__':
    main()
