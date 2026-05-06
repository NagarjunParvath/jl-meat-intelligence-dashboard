# Fabric Notebook: NB_Ingest_USDA
# ---------------------------------
# Attach this notebook to: MeatIntelligenceLH
# Schedule: Daily 2:30 PM ET (18:30 UTC)
# Replaces: update_usda_data.py
#
# HOW TO USE:
#   1. In Fabric workspace -> New -> Notebook -> rename to NB_Ingest_USDA
#   2. Attach MeatIntelligenceLH via the Lakehouse picker (top-left)
#   3. Copy each CELL block below into a separate notebook cell
#   4. Run All on first use; thereafter the pipeline scheduler runs it daily

# =============================================================================
# CELL 1 — Install dependencies
# =============================================================================
# %pip install pypdf requests --quiet


# =============================================================================
# CELL 2 — Imports and PDF parsing helpers
# =============================================================================
import re
import requests
from io import BytesIO
from datetime import datetime, date
from pypdf import PdfReader
from pyspark.sql import SparkSession
from pyspark.sql.types import (
    StructType, StructField, StringType, DoubleType, IntegerType, LongType
)

spark = SparkSession.builder.getOrCreate()

REPORTS = {
    'LM_XB401': 'https://www.ams.usda.gov/mnreports/ams_2451.pdf',
    'LM_XB403': 'https://www.ams.usda.gov/mnreports/ams_2453.pdf',
    'LM_XB405': 'https://www.ams.usda.gov/mnreports/ams_2455.pdf',
    'LM_PK602': 'https://www.ams.usda.gov/mnreports/ams_2498.pdf',
}


def fetch_pdf_text(url: str) -> str:
    r = requests.get(url, timeout=45, headers={'User-Agent': 'JackLinks-MIC/2.0'})
    r.raise_for_status()
    reader = PdfReader(BytesIO(r.content))
    return '\n'.join((p.extract_text() or '') for p in reader.pages)


def extract_report_date(text: str) -> str:
    m = re.search(
        r'(January|February|March|April|May|June|July|August|September|'
        r'October|November|December)\s+(\d{1,2}),\s+(\d{4})',
        text
    )
    return f"{m.group(1)} {m.group(2)}, {m.group(3)}" if m else str(date.today())


# ── Line-based helpers (pypdf extracts USDA tables column-by-column) ──

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
    s = s.strip()
    if not s or s in ('-', '—'):
        return (False, None)
    m = _NUM_RX.match(s)
    if not m:
        return (False, None)
    val = float(m.group(1).replace(',', ''))
    if s.startswith('(') and s.endswith(')'):
        val = -val
    return (True, val)


def _collect_numbers(lines: list, start: int, n: int, max_scan: int = 20) -> list:
    vals = []
    for i in range(start, min(start + max_scan, len(lines))):
        l = lines[i].strip()
        if not l or l in ('-', '—'):
            continue
        is_num, v = _parse_cell(l)
        if is_num:
            vals.append(v)
            if len(vals) >= n:
                break
        else:
            break
    return vals


print("Helpers loaded.")


# =============================================================================
# CELL 3 — PDF parsers (one per USDA report)
# =============================================================================

def _num(s: str) -> float:
    return float(s.replace(',', ''))


def _parse_xb401_section(section_text: str, prefix: str) -> dict:
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
            }
        else:
            out[key] = None
    return out


def parse_xb401(text: str) -> dict:
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


def parse_xb403(text: str) -> dict:
    lines = _clean_lines(text)
    result = {}

    i = _find_line(lines, r'Current Cutout Values')
    if i >= 0:
        nums = _collect_numbers(lines, i + 1, 2)
        if len(nums) == 2:
            result['choice_cutout'] = nums[0]
            result['select_cutout'] = nums[1]

    i = _find_line(lines, r'Change from prior day')
    if i >= 0:
        nums = _collect_numbers(lines, i + 1, 2)
        if len(nums) == 2:
            result['choice_change'] = nums[0]
            result['select_change'] = nums[1]

    i = _find_line(lines, r'Choice/Select spread')
    if i >= 0:
        nums = _collect_numbers(lines, i + 1, 1)
        if nums:
            result['choice_select_spread'] = nums[0]

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
                result[f'{key}_choice'] = nums[0]
                result[f'{key}_select'] = nums[1]

    subs = {
        'ribeye_lipon_bnin':  r'Rib,\s*ribeye,\s*lip-on,\s*bn-in',
        'ribeye_bnls_heavy':  r'Rib,\s*ribeye,\s*bnls,\s*heavy',
        'chuck_roll':         r'Chuck,\s*chuck roll',
        'knuckle_peeled':     r'Round,\s*knuckle,\s*peeled',
        'top_inside_round':   r'Round,\s*top inside round',
        'top_inside_denuded': r'Round,\s*top inside,\s*denuded',
        'eye_of_round':       r'Round,\s*eye of round',
        'strip_loin_bnls':    r'Loin,\s*strip,\s*short-cut.*bnls|Loin,\s*strip,\s*bnls',
        'tenderloin':         r'Loin,\s*tenderloin',
        'brisket_deckle_off': r'Brisket,\s*deckle-off',
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
    lines = _clean_lines(text)
    result = {}

    i = _find_line(lines, r'Current[- ]Cutout Value')
    if i >= 0:
        nums = _collect_numbers(lines, i + 1, 1)
        if nums:
            result['cutout_value'] = nums[0]

    lean_items = {
        'lean_90':          r'^90%\s+lean$',
        'insides_combo':    r'^100%\s+lean\s+inside\s+round$',
        'flats_eyes_combo': r'^100%\s+lean,\s+flats\s+and\s+eyes$',
        'spb_combo':        r'^100%\s+lean,\s+S\.P\.B\.$',
        'chuck_tender':     r'^Chuck\s+Tender$',
        'flank_steak':      r'^Flank\s+Steak$',
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

    bb_items = {
        'bb_ribeye_8_10':        r'Rib,\s*ribeye roll,\s*8-10 lbs',
        'bb_ribeye_10up':        r'Rib,\s*ribeye roll,\s*10-up lbs',
        'bb_chuck_brisket':      r'Chuck,\s*brisket',
        'bb_top_inside_10dn':    r'Round,\s*top inside,\s*10-dn lbs',
        'bb_top_inside_cap_off': r'Round,\s*top inside c-off,\s*10-14 lbs',
        'bb_eye_of_round':       r'Round,\s*eye of round',
        'bb_strip_loin_9up':     r'Loin,\s*strip,\s*bnls,\s*9-up',
        'bb_top_sirloin_butt':   r'Loin,\s*top sirloin butt',
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


def parse_pk602(text: str) -> dict:
    result = {}

    cutout_row = re.search(
        r'(\d{2}/\d{2}/\d{4})\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)',
        text
    )
    if cutout_row:
        result['primal_cutout_carcass'] = float(cutout_row.group(3))
        result['primal_cutout_loin']    = float(cutout_row.group(4))
        result['primal_cutout_butt']    = float(cutout_row.group(5))
        result['primal_cutout_picnic']  = float(cutout_row.group(6))
        result['primal_cutout_rib']     = float(cutout_row.group(7))
        result['primal_cutout_ham']     = float(cutout_row.group(8))
        result['primal_cutout_belly']   = float(cutout_row.group(9))

    for g in ('72', '42', '65', '85'):
        pat = rf'{g}% Trim Combo\s+([\d,]+)\s+([\d.]+)\s*-\s*([\d.]+)\s+([\d.]+)'
        m = re.search(pat, text)
        if m:
            result[f'trim_{g}'] = {
                'pounds': int(m.group(1).replace(',', '')),
                'range_low': float(m.group(2)),
                'range_high': float(m.group(3)),
                'weighted_avg_cwt': float(m.group(4)),
            }

    for size in ('9-13#', '13-17#', '17-19#'):
        pat = rf'Derind Belly {re.escape(size)}\s+([\d,]+)\s+([\d.]+)\s*-\s*([\d.]+)\s+([\d.]+)'
        m = re.search(pat, text)
        if m:
            result[f'belly_{size.replace("#", "")}'] = {
                'pounds': int(m.group(1).replace(',', '')),
                'weighted_avg_cwt': float(m.group(4)),
            }

    m = re.search(r'Bnls CC Strap-on\s+([\d,]+)\s+([\d.]+)\s*-\s*([\d.]+)\s+([\d.]+)', text)
    if m:
        result['loin_bnls_cc_strap_on'] = {
            'pounds': int(m.group(1).replace(',', '')),
            'weighted_avg_cwt': float(m.group(4)),
        }
    return result


PARSERS = {
    'LM_XB401': parse_xb401,
    'LM_XB403': parse_xb403,
    'LM_XB405': parse_xb405,
    'LM_PK602': parse_pk602,
}

print("Parsers loaded.")


# =============================================================================
# CELL 4 — Fetch all 4 reports, flatten to rows
# =============================================================================
fetched_at  = datetime.utcnow().isoformat(timespec='seconds')
report_date = None
rows        = []

for code, url in REPORTS.items():
    try:
        text = fetch_pdf_text(url)
        if not report_date:
            report_date = extract_report_date(text)
        parsed = PARSERS[code](text)

        for metric_name, val in parsed.items():
            if isinstance(val, dict):
                price  = val.get('weighted_avg_cwt')
                trades = val.get('trades')
                pounds = val.get('pounds')
            elif isinstance(val, (int, float)):
                price, trades, pounds = float(val), None, None
            else:
                continue
            if price is not None:
                rows.append((
                    code,
                    report_date or str(date.today()),
                    metric_name,
                    float(price),
                    int(trades) if trades is not None else None,
                    int(pounds) if pounds is not None else None,
                    fetched_at,
                ))

        print(f"  {code}: {sum(1 for v in parsed.values() if v)} metrics parsed")
    except Exception as e:
        print(f"  {code}: FAILED — {type(e).__name__}: {e}")

print(f"\nTotal rows: {len(rows)}")
print(f"Report date: {report_date}")


# =============================================================================
# CELL 5 — Write to Delta table (create on first run, upsert on subsequent)
# =============================================================================
SCHEMA = StructType([
    StructField("report_code",  StringType(),  False),
    StructField("report_date",  StringType(),  False),
    StructField("metric_name",  StringType(),  False),
    StructField("price_cwt",    DoubleType(),  True),
    StructField("trades",       IntegerType(), True),
    StructField("pounds",       LongType(),    True),
    StructField("fetched_at",   StringType(),  True),
])

df = spark.createDataFrame(rows, schema=SCHEMA)

TABLE = "usda_prices_daily"

if not spark.catalog.tableExists(TABLE):
    df.write.format("delta").saveAsTable(TABLE)
    print(f"Table '{TABLE}' created with {df.count()} rows.")
else:
    df.createOrReplaceTempView("_new_prices")
    spark.sql(f"""
        MERGE INTO {TABLE} AS tgt
        USING _new_prices AS src
          ON  tgt.report_code = src.report_code
         AND  tgt.report_date  = src.report_date
         AND  tgt.metric_name  = src.metric_name
        WHEN MATCHED     THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"Upserted {len(rows)} rows into '{TABLE}'.")

spark.sql(f"""
    SELECT report_code, COUNT(*) AS row_count
    FROM {TABLE}
    GROUP BY report_code
    ORDER BY report_code
""").show()
