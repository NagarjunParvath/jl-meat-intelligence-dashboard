# Fabric Notebook: NB_Backfill_History
# --------------------------------------
# Attach this notebook to: MeatIntelligenceLH
# Run: ONCE to load 5 years of history (then never again — NB_Ingest_USDA handles daily appends)
# Replaces: backfill_history.py
#
# HOW TO USE:
#   1. In Fabric workspace -> New -> Notebook -> rename to NB_Backfill_History
#   2. Attach MeatIntelligenceLH via the Lakehouse picker (top-left)
#   3. Copy each CELL block below into a separate notebook cell
#   4. Set your USDA API key in CELL 2 (via Key Vault or direct for testing)
#   5. Run All — takes ~10-15 minutes for 5 years across all reports

# =============================================================================
# CELL 1 — Install dependencies
# =============================================================================
# %pip install requests --quiet


# =============================================================================
# CELL 2 — API key setup
# =============================================================================
# OPTION A (recommended): Azure Key Vault via notebookutils
#   - Store secret named "USDA-MPR-API-KEY" in your Key Vault
#   - Grant the Fabric workspace managed identity "Key Vault Secrets User" role
#
# from notebookutils import mssparkutils
# USDA_KEY = mssparkutils.credentials.getSecret(
#     "https://<your-keyvault-name>.vault.azure.net/",
#     "USDA-MPR-API-KEY"
# )

# OPTION B (quick test only — remove before scheduling):
USDA_KEY = ""   # paste your key here for a one-off test run

if not USDA_KEY:
    raise ValueError("Set USDA_KEY above before running.")

print(f"Key loaded: {USDA_KEY[:4]}...{USDA_KEY[-4:]} ({len(USDA_KEY)} chars)")


# =============================================================================
# CELL 3 — Report + cut config (identical logic to backfill_history.py)
# =============================================================================
import re
import time
import requests
from datetime import date, timedelta
from urllib.parse import quote
from pyspark.sql import SparkSession
from pyspark.sql.types import (
    StructType, StructField, StringType, DoubleType, IntegerType, LongType
)

spark = SparkSession.builder.getOrCreate()

API_BASE = 'https://mpr.datamart.ams.usda.gov/services/v1.1/reports'

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
            'insides':  [(r'100.*Lean|Lean Item|Items', r'(100% lean inside|inside round|inside)')],
            'flats':    [(r'100.*Lean|Lean Item|Items', r'(flats and eyes|flats\s*&\s*eyes|flats)')],
            'eyes':     [(r'Boner|Breaker|BONER',       r'(eye of round|171C)')],
            'lean_90':  [(r'100.*Lean|Lean Item|Items', r'90%\s*lean')],
            'spb':      [(r'100.*Lean|Lean Item|Items', r'(S\.P\.B|SPB)')],
        },
    },
    'LM_PK602': {
        'slug': '2498',
        'cuts': {
            'pork_trim_72':     [(r'Trim',             r'72%\s*Trim\s*Combo')],
            'pork_trim_42':     [(r'Trim',             r'42%\s*Trim\s*Combo')],
            'pork_belly_13_17': [(r'Bell',             r'Derind\s*Belly\s*13-?17#?')],
            'pork_loin':        [(r'Cutout and Primal', None, 'pork_loin')],
        },
    },
    'LM_XB403': {
        'slug': '2453',
        'cuts': {
            'choice_cutout':      [(r'Cutout|Summary|.*', r'Choice Cutout|600-?900 Choice')],
            'select_cutout':      [(r'Cutout|Summary|.*', r'Select Cutout|600-?900 Select')],
            'ribeye_lipon':       [(r'Choice|Cuts',       r'ribeye.*lip-?on.*bn-?in')],
            'chuck_roll':         [(r'Choice|Cuts',       r'chuck\s*roll')],
            'strip_loin':         [(r'Choice|Cuts',       r'strip.*bnls|strip.*bon')],
            'top_inside':         [(r'Choice|Cuts',       r'top inside')],
            'eye_of_round_boxed': [(r'Choice|Cuts',       r'eye of round')],
        },
    },
}

SECTION_FALLBACK = {
    '2451': ['National', 'Central'],
    '2455': ['100% Lean Items', 'BONER/BREAKER', 'Boner/Breaker'],
    '2498': ['Daily National Carlot Pork Report', 'National Daily Pork Report - Afternoon',
             'Bellies', 'Trimmings', 'Loins'],
    '2453': ['Choice Cuts', 'Select Cuts', 'Current Cutout Values',
             'Composite Primal Values', 'Trimmings'],
}


def _to_float(s):
    if s is None: return None
    s = str(s).strip()
    if not s or s in ('-', '.00', '0.00'): return None
    try: return float(s.replace(',', ''))
    except ValueError: return None


def _to_int(s):
    if s is None: return None
    s = str(s).strip()
    if not s or s == '-': return None
    try: return int(s.replace(',', ''))
    except ValueError: return None


def _parse_date(s: str):
    if not s: return None
    try:
        m, d, y = s.split('/')
        return f'{int(y):04d}-{int(m):02d}-{int(d):02d}'
    except Exception: return None


def _fetch(key: str, url: str, params: dict, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.get(url, auth=(key, ''), params=params, timeout=300,
                             headers={'Accept': 'application/json',
                                      'User-Agent': 'JL-MIC-Fabric/2.0'})
        except requests.exceptions.RequestException as e:
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                return None
            if isinstance(data, str):
                return None
            return data
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        print(f"  HTTP {r.status_code}: {r.text[:150]}")
        return None
    return None


def _discover_sections(key: str, slug: str) -> list:
    data = _fetch(key, f'{API_BASE}/{slug}', {})
    if data:
        sections = data.get('reportSections') or []
        found = [s for s in sections if isinstance(s, str) and s.lower() != 'summary']
        if found:
            return found
    fb = SECTION_FALLBACK.get(slug, [])
    if fb:
        print(f"  (fallback sections: {fb})")
    return fb


def _fetch_section_year(key, slug, section, start, end) -> list:
    url = f'{API_BASE}/{slug}/{quote(section)}'
    params = {'q': f'report_date={start.strftime("%m/%d/%Y")}:{end.strftime("%m/%d/%Y")}'}
    data = _fetch(key, url, params)
    if not data: return []
    return data.get('results') or []


print("Config and helpers loaded.")


# =============================================================================
# CELL 4 — Run backfill for all reports (5 years)
# =============================================================================
YEARS = 5
today = date.today()
start_date = today.replace(year=today.year - YEARS)

all_rows = []   # (report_code, cut_key, obs_date, price_cwt, range_low, range_high, trades, pounds)

for report_code, cfg in REPORTS.items():
    slug = cfg['slug']
    cuts = cfg['cuts']
    print(f"\n== {report_code} (slug {slug}) ==")

    sections = _discover_sections(USDA_KEY, slug)
    if not sections:
        print("  No sections found — skipping.")
        continue
    print(f"  Sections: {sections}")

    # collected[cut_key][iso_date] = row dict — dedup by date
    collected = {k: {} for k in cuts}

    for section in sections:
        relevant_cuts = {}
        for cut_key, patterns in cuts.items():
            matched = [p for p in patterns if re.search(p[0], section, re.IGNORECASE)]
            if matched:
                relevant_cuts[cut_key] = matched

        if not relevant_cuts:
            continue
        print(f"  -> {section!r}  targets: {list(relevant_cuts.keys())}")

        # Year-chunk the pull to avoid timeouts
        chunk_start = start_date
        while chunk_start < today:
            chunk_end = min(
                chunk_start.replace(year=chunk_start.year + 1) - timedelta(days=1),
                today
            )
            t0 = time.time()
            rows_raw = _fetch_section_year(USDA_KEY, slug, section, chunk_start, chunk_end)
            dt = time.time() - t0
            print(f"    {chunk_start} -> {chunk_end}: {len(rows_raw)} rows ({dt:.1f}s)")

            wide_cuts    = {k: v for k, v in relevant_cuts.items()
                            if any(len(p) == 3 and p[1] is None for p in v)}
            regular_cuts = {k: v for k, v in relevant_cuts.items() if k not in wide_cuts}

            for row in rows_raw:
                item_desc = (row.get('item_desc') or row.get('Item_Description') or '').strip()
                iso = _parse_date(row.get('report_date', ''))
                if not iso:
                    continue

                # Wide-column cuts (e.g. pork_loin)
                for cut_key, matched_pats in wide_cuts.items():
                    for p in matched_pats:
                        if len(p) == 3 and p[1] is None:
                            val = _to_float(row.get(p[2]))
                            if val and val > 0:
                                collected[cut_key][iso] = {
                                    'date': iso, 'price_cwt': val,
                                    'range_low': None, 'range_high': None,
                                    'trades': None, 'pounds': None,
                                }

                # Regular item_desc cuts
                wa = (_to_float(row.get('price_range_avg'))
                      or _to_float(row.get('weighted_avg'))
                      or _to_float(row.get('weighted_average')))
                if not item_desc or wa is None or wa <= 0:
                    continue

                for cut_key, matched_pats in regular_cuts.items():
                    item_regexes = [p[1] for p in matched_pats
                                    if len(p) >= 2 and p[1] is not None]
                    if any(re.search(rx, item_desc, re.IGNORECASE) for rx in item_regexes):
                        collected[cut_key][iso] = {
                            'date': iso,
                            'price_cwt': wa,
                            'range_low': _to_float(row.get('price_range_low')),
                            'range_high': _to_float(row.get('price_range_high')),
                            'trades': _to_int(row.get('number_trades')),
                            'pounds': _to_int(row.get('total_pounds')),
                        }
                        break

            chunk_start = chunk_end + timedelta(days=1)

    # Flatten and add to all_rows
    for cut_key, by_date in collected.items():
        series = sorted(by_date.values(), key=lambda x: x['date'])
        print(f"  {cut_key:25s}  {len(series):>5} obs")
        for obs in series:
            all_rows.append((
                report_code,
                cut_key,
                obs['date'],
                obs.get('price_cwt'),
                obs.get('range_low'),
                obs.get('range_high'),
                obs.get('trades'),
                obs.get('pounds'),
            ))

print(f"\nTotal rows collected: {len(all_rows)}")


# =============================================================================
# CELL 5 — Write to Delta table
# =============================================================================
SCHEMA = StructType([
    StructField("report_code", StringType(),  False),
    StructField("cut_key",     StringType(),  False),
    StructField("obs_date",    StringType(),  False),
    StructField("price_cwt",   DoubleType(),  True),
    StructField("range_low",   DoubleType(),  True),
    StructField("range_high",  DoubleType(),  True),
    StructField("trades",      IntegerType(), True),
    StructField("pounds",      LongType(),    True),
])

df = spark.createDataFrame(all_rows, schema=SCHEMA)

TABLE = "usda_price_history"

df.write.format("delta") \
    .mode("overwrite") \
    .option("overwriteSchema", "true") \
    .saveAsTable(TABLE)

print(f"\nWrote {df.count()} rows to '{TABLE}'.")
spark.sql(f"""
    SELECT cut_key,
           COUNT(*)      AS obs,
           MIN(obs_date) AS earliest,
           MAX(obs_date) AS latest
    FROM {TABLE}
    GROUP BY cut_key
    ORDER BY cut_key
""").show(50)
