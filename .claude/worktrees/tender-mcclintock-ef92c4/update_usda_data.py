#!/usr/bin/env python3
"""
Jack Link's Beef Market Intelligence — USDA Live Data Updater
=============================================================

Pulls the 4 USDA AMS daily PDFs, parses the key prices, writes data/latest.json
alongside the HTML dashboard. The dashboard reads this JSON on page load to
override hardcoded values with live numbers — no manual editing required.

Reports pulled:
  LM_XB401 → National Daily Beef Trim        (https://www.ams.usda.gov/mnreports/ams_2451.pdf)
  LM_XB403 → National Daily Boxed Beef       (https://www.ams.usda.gov/mnreports/ams_2453.pdf)
  LM_XB405 → 5-Day Cow Cutout / 100VL        (https://www.ams.usda.gov/mnreports/ams_2455.pdf)
  LM_PK602 → National Daily Pork             (https://www.ams.usda.gov/mnreports/ams_2498.pdf)

Setup (one-time):
    python -m pip install pypdf requests

Run manually:
    python update_usda_data.py

Schedule daily (Windows Task Scheduler):
    Action:   python "C:\\Users\\nagar\\Downloads\\Meat Inteligence Dashboard\\update_usda_data.py"
    Trigger:  Daily at 3:30 PM Central (after USDA 2 PM ET release)

Usage:
    python update_usda_data.py            # normal run
    python update_usda_data.py --verbose  # print parsed data
    python update_usda_data.py --keep     # keep downloaded PDFs in .tmp/
"""

import json
import re
import sys
import argparse
from datetime import datetime
from pathlib import Path

# Force UTF-8 on stdout/stderr so the → ✓ ✗ symbols survive redirection on
# Windows, where the default is cp1252 and a UnicodeEncodeError otherwise
# crashes the whole script.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

try:
    from pypdf import PdfReader
    import requests
except ImportError:
    print("Missing dependencies. Install with:")
    print("    python -m pip install pypdf requests")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent

REPORTS = {
    'LM_XB401': {
        'url': 'https://www.ams.usda.gov/mnreports/ams_2451.pdf',
        'name': 'National Daily Boneless Processing Beef & Beef Trimmings',
    },
    'LM_XB403': {
        'url': 'https://www.ams.usda.gov/mnreports/ams_2453.pdf',
        'name': 'National Daily Boxed Beef Cutout & Cuts',
    },
    'LM_XB405': {
        'url': 'https://www.ams.usda.gov/mnreports/ams_2455.pdf',
        'name': 'National 5-Day Rolling Cutter Cow Cutout & 100VL',
    },
    'LM_PK602': {
        'url': 'https://www.ams.usda.gov/mnreports/ams_2498.pdf',
        'name': 'National Daily Pork FOB Plant',
    },
}

# ──────────────────────────────────────────────────────────────
# FETCH
# ──────────────────────────────────────────────────────────────

def fetch_pdf(url: str, dest: Path) -> None:
    r = requests.get(url, timeout=45, headers={'User-Agent': 'JackLinks-MIC/1.0'})
    r.raise_for_status()
    dest.write_bytes(r.content)


def extract_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    return '\n'.join((p.extract_text() or '') for p in reader.pages)


# ──────────────────────────────────────────────────────────────
# PARSERS — one per report
# ──────────────────────────────────────────────────────────────

def _num(s: str) -> float:
    return float(s.replace(',', ''))


def _parse_xb401_section(section_text: str, prefix: str) -> dict:
    """Parse one FOB Plant section (National or Central). Returns {prefix}_grade → dict."""
    out = {}
    grades = ['92-94', '90', '85', '81', '75', '73', '65', '50']
    for g in grades:
        pat = rf'Fresh\s+{re.escape(g)}%\s+(\d+)\s+([\d,]+)\s+([\d.]+)\s*-\s*([\d.]+)\s+([\d.]+)'
        m = re.search(pat, section_text)
        key = f'{prefix}_{g}'
        if m and _num(m.group(1)) > 0:
            out[key] = {
                'trades': int(m.group(1)),
                'pounds': int(m.group(2).replace(',', '')),
                'range_low': float(m.group(3)),
                'range_high': float(m.group(4)),
                'weighted_avg_cwt': float(m.group(5)),
                'price_lb': round(float(m.group(5)) / 100, 4),
            }
        else:
            out[key] = None   # 0 trades = not established
    return out


def parse_xb401(text: str) -> dict:
    """LM_XB401 — Pulls BOTH FOB Plant - National AND FOB Plant - Central sections.

    National keys kept as `fresh_XX` (unchanged for backward compatibility).
    Central keys added as `central_XX`.
    """
    result = {}

    nat_idx = text.find('FOB Plant - National')
    if nat_idx >= 0:
        result.update(_parse_xb401_section(text[nat_idx:nat_idx + 4000], 'fresh'))
    else:
        for g in ['92-94', '90', '85', '81', '75', '73', '65', '50']:
            result[f'fresh_{g}'] = None

    cen_idx = text.find('FOB Plant - Central')
    if cen_idx >= 0:
        result.update(_parse_xb401_section(text[cen_idx:cen_idx + 4000], 'central'))
    else:
        for g in ['92-94', '90', '85', '81', '75', '73', '65', '50']:
            result[f'central_{g}'] = None

    return result


def parse_pk602(text: str) -> dict:
    """LM_PK602 — National Daily Pork. Pulls Primal Cutout + Trim + Belly + Loin."""
    result = {}

    # Primal Cutout line: DATE LOADS CARCASS LOIN BUTT PIC RIB HAM BELLY
    cutout_row = re.search(
        r'(\d{2}/\d{2}/\d{4})\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)',
        text
    )
    if cutout_row:
        result['primal_cutout'] = {
            'date': cutout_row.group(1),
            'loads': float(cutout_row.group(2)),
            'carcass': float(cutout_row.group(3)),
            'loin': float(cutout_row.group(4)),
            'butt': float(cutout_row.group(5)),
            'picnic': float(cutout_row.group(6)),
            'rib': float(cutout_row.group(7)),
            'ham': float(cutout_row.group(8)),
            'belly': float(cutout_row.group(9)),
        }

    # Trim grades
    for g in ('72', '42', '65', '85'):
        pat = rf'{g}% Trim Combo\s+([\d,]+)\s+([\d.]+)\s*-\s*([\d.]+)\s+([\d.]+)'
        m = re.search(pat, text)
        if m:
            result[f'trim_{g}'] = {
                'pounds': int(m.group(1).replace(',', '')),
                'range_low': float(m.group(2)),
                'range_high': float(m.group(3)),
                'weighted_avg_cwt': float(m.group(4)),
                'price_lb': round(float(m.group(4)) / 100, 4),
            }

    # Belly sizes
    for size in ('9-13#', '13-17#', '17-19#'):
        pat = rf'Derind Belly {re.escape(size)}\s+([\d,]+)\s+([\d.]+)\s*-\s*([\d.]+)\s+([\d.]+)'
        m = re.search(pat, text)
        if m:
            result[f'belly_{size.replace("#","")}'] = {
                'pounds': int(m.group(1).replace(',', '')),
                'weighted_avg_cwt': float(m.group(4)),
            }

    # Loin: Bnls CC Strap-on (biggest-volume loin item)
    m = re.search(r'Bnls CC Strap-on\s+([\d,]+)\s+([\d.]+)\s*-\s*([\d.]+)\s+([\d.]+)', text)
    if m:
        result['loin_bnls_cc_strap_on'] = {
            'pounds': int(m.group(1).replace(',', '')),
            'weighted_avg_cwt': float(m.group(4)),
        }
    return result


# ── Helpers for line-based table parsing ──
# pypdf extracts these USDA tables column-by-column, one cell per line, so
# regex on flat text fails. These walk the line array, locate a label, and
# pull the next N numeric cells.

def _clean_lines(text: str) -> list:
    return [l.strip() for l in text.split('\n')]


def _find_line(lines: list, pattern: str, start: int = 0) -> int:
    rx = re.compile(pattern)
    for i in range(start, len(lines)):
        if rx.search(lines[i]):
            return i
    return -1


_NUM_RX = re.compile(r'^\(?\$?\s*([-+]?[\d,]+(?:\.\d+)?)\s*\)?$')


def _parse_cell(s: str):
    """Return (True, float) if cell is a number (handles $, commas, parens for neg), else (False, None)."""
    s = s.strip()
    if not s or s == '-' or s == '—':
        return (False, None)
    m = _NUM_RX.match(s)
    if not m:
        return (False, None)
    val = float(m.group(1).replace(',', ''))
    if s.startswith('(') and s.endswith(')'):
        val = -val
    return (True, val)


def _collect_numbers(lines: list, start: int, n: int, max_scan: int = 20) -> list:
    """From line `start`, collect up to `n` numeric cells, skipping dashes,
    stopping if we hit a non-numeric non-dash text cell (= next table row)."""
    vals = []
    end = min(start + max_scan, len(lines))
    for i in range(start, end):
        l = lines[i].strip()
        if not l:
            continue
        if l == '-' or l == '—':
            continue
        is_num, v = _parse_cell(l)
        if is_num:
            vals.append(v)
            if len(vals) >= n:
                break
        else:
            # hit another label line; stop
            break
    return vals


def parse_xb403(text: str) -> dict:
    """LM_XB403 — Boxed Beef Cutout & Cuts. Vertical-column PDF layout."""
    lines = _clean_lines(text)
    result = {}

    # Current Cutout Values — Choice then Select on next two numeric lines
    i = _find_line(lines, r'Current Cutout Values')
    if i >= 0:
        nums = _collect_numbers(lines, i + 1, 2)
        if len(nums) == 2:
            result['choice_cutout'] = nums[0]
            result['select_cutout'] = nums[1]

    # Change from prior day (Choice, Select)
    i = _find_line(lines, r'Change from prior day')
    if i >= 0:
        nums = _collect_numbers(lines, i + 1, 2)
        if len(nums) == 2:
            result['choice_change'] = nums[0]
            result['select_change'] = nums[1]

    # Choice/Select spread
    i = _find_line(lines, r'Choice/Select spread')
    if i >= 0:
        nums = _collect_numbers(lines, i + 1, 1)
        if nums:
            result['choice_select_spread'] = nums[0]

    # Composite Primal Values — each primal row is Name, then Choice & Select
    primals_map = {
        'primal_rib':         r'^Primal Rib$',
        'primal_chuck':       r'^Primal Chuck$',
        'primal_round':       r'^Primal Round$',
        'primal_loin':        r'^Primal Loin$',
        'primal_brisket':     r'^Primal Brisket$',
        'primal_short_plate': r'^Primal Short Plate$',
        'primal_flank':       r'^Primal Flank$',
    }
    for key, pat in primals_map.items():
        i = _find_line(lines, pat)
        if i >= 0:
            nums = _collect_numbers(lines, i + 1, 2)
            if len(nums) == 2:
                result[key] = {'choice': nums[0], 'select': nums[1]}

    # Sub-primal rows — after the description line we expect:
    # trades, pounds, range_low, (dash), range_high, weighted_avg = 5 numeric cells
    subs = {
        'ribeye_lipon_bnin':    r'Rib,\s*ribeye,\s*lip-on,\s*bn-in',
        'ribeye_bnls_heavy':    r'Rib,\s*ribeye,\s*bnls,\s*heavy',
        'chuck_roll':           r'Chuck,\s*chuck roll',
        'knuckle_peeled':       r'Round,\s*knuckle,\s*peeled',
        'top_inside_round':     r'Round,\s*top inside round',
        'top_inside_denuded':   r'Round,\s*top inside,\s*denuded',
        'eye_of_round':         r'Round,\s*eye of round',
        'strip_loin_bnls':      r'Loin,\s*strip,\s*short-cut.*bnls|Loin,\s*strip,\s*bnls',
        'tenderloin':           r'Loin,\s*tenderloin',
        'brisket_deckle_off':   r'Brisket,\s*deckle-off',
    }
    for key, pat in subs.items():
        i = _find_line(lines, pat)
        if i < 0:
            continue
        nums = _collect_numbers(lines, i + 1, 5)
        if len(nums) == 5 and nums[0] > 0:
            result[key] = {
                'trades': int(nums[0]),
                'pounds': int(nums[1]),
                'range_low': nums[2],
                'range_high': nums[3],
                'weighted_avg_cwt': nums[4],
            }
    return result


def parse_xb405(text: str) -> dict:
    """LM_XB405 — 5-Day Rolling Cutter Cow Cutout. Pulls 100VL + BONER/BREAKER items."""
    lines = _clean_lines(text)
    result = {}

    # Current-Cutout Value
    i = _find_line(lines, r'Current[- ]Cutout Value')
    if i >= 0:
        nums = _collect_numbers(lines, i + 1, 1)
        if nums:
            result['cutout_value'] = nums[0]

    # 100% Lean Items — each row is Name → Price, Value, Change (3 numeric cells)
    lean_items = {
        'lean_90':           r'^90%\s+lean$',
        'insides_combo':     r'^100%\s+lean\s+inside\s+round$',
        'flats_eyes_combo':  r'^100%\s+lean,\s+flats\s+and\s+eyes$',
        'spb_combo':         r'^100%\s+lean,\s+S\.P\.B\.$',
        'chuck_tender':      r'^Chuck\s+Tender$',
        'flank_steak':       r'^Flank\s+Steak$',
    }
    for key, pat in lean_items.items():
        i = _find_line(lines, pat)
        if i < 0:
            continue
        nums = _collect_numbers(lines, i + 1, 3)
        if nums:
            result[key] = {
                'weighted_avg_cwt': nums[0],
                'contribution': nums[1] if len(nums) > 1 else None,
                'change_cwt': nums[2] if len(nums) > 2 else None,
            }

    # BONER/BREAKER sub-primals (page 2+) — each row: trades, pounds, wa, change (4 cells, no range)
    bb_items = {
        'bb_ribeye_8_10':       r'Rib,\s*ribeye roll,\s*8-10 lbs',
        'bb_ribeye_10up':       r'Rib,\s*ribeye roll,\s*10-up lbs',
        'bb_chuck_brisket':     r'Chuck,\s*brisket',
        'bb_top_inside_10dn':   r'Round,\s*top inside,\s*10-dn lbs',
        'bb_top_inside_cap_off': r'Round,\s*top inside c-off,\s*10-14 lbs',
        'eye_of_round':         r'Round,\s*eye of round',
        'bb_strip_loin_9up':    r'Loin,\s*strip,\s*bnls,\s*9-up',
        'bb_top_sirloin_butt':  r'Loin,\s*top sirloin butt',
    }
    for key, pat in bb_items.items():
        i = _find_line(lines, pat)
        if i < 0:
            continue
        nums = _collect_numbers(lines, i + 1, 4)
        if len(nums) >= 3 and nums[0] > 0:
            result[key] = {
                'trades': int(nums[0]),
                'pounds': int(nums[1]),
                'weighted_avg_cwt': nums[2],
                'change_cwt': nums[3] if len(nums) > 3 else None,
            }

    return result


PARSERS = {
    'LM_XB401': parse_xb401,
    'LM_XB403': parse_xb403,
    'LM_XB405': parse_xb405,
    'LM_PK602': parse_pk602,
}


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def extract_report_date(text: str) -> str:
    m = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})',
        text
    )
    if m:
        return f"{m.group(1)} {m.group(2)}, {m.group(3)}"
    return None


def main():
    ap = argparse.ArgumentParser(description='Pull USDA AMS reports → data/latest.json')
    ap.add_argument('--verbose', '-v', action='store_true', help='print parsed data')
    ap.add_argument('--keep', action='store_true', help='keep downloaded PDFs in .tmp/')
    args = ap.parse_args()

    out_dir = SCRIPT_DIR / 'data'
    tmp_dir = SCRIPT_DIR / '.tmp'
    out_dir.mkdir(exist_ok=True)
    tmp_dir.mkdir(exist_ok=True)

    data = {
        'fetched_at': datetime.now().isoformat(timespec='seconds'),
        'report_date': None,
        'reports': {},
    }

    print(f"Jack Link's MIC — USDA updater · {data['fetched_at']}\n")

    for code, meta in REPORTS.items():
        url = meta['url']
        pdf_path = tmp_dir / f'{code}.pdf'
        print(f"  → {code:10s} {meta['name']}")
        try:
            fetch_pdf(url, pdf_path)
            text = extract_text(pdf_path)
            if not data['report_date']:
                data['report_date'] = extract_report_date(text)
            parsed = PARSERS[code](text)
            data['reports'][code] = parsed
            print(f"    ✓ parsed {sum(1 for v in parsed.values() if v)} items")
            if args.verbose:
                print(json.dumps(parsed, indent=2))
        except Exception as e:
            print(f"    ✗ FAILED: {type(e).__name__}: {e}")
            data['reports'][code] = {'error': f'{type(e).__name__}: {e}'}

    out_file = out_dir / 'latest.json'
    out_file.write_text(json.dumps(data, indent=2), encoding='utf-8')

    print(f"\n✓ Wrote: {out_file}")
    print(f"  Report date: {data['report_date']}")

    # ── Run forecast engine ───────────────────────────────────────────
    try:
        import forecast_cuts
        forecast_cuts.main()
    except Exception as e:
        print(f"  ✗ forecast_cuts failed: {type(e).__name__}: {e}")

    print(f"  Open the dashboard in a browser — values refresh automatically.")

    if not args.keep:
        for p in tmp_dir.glob('*.pdf'):
            p.unlink()
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


if __name__ == '__main__':
    main()
