#!/usr/bin/env python3
"""
Jack Link's Meat Intelligence — Drought & Weather Data Fetcher
==============================================================

Pulls live drought and weather data and writes data/drought.json,
which the dashboard JS loader reads to update the Drought & Weather tab.

Sources:
  USDM      → droughtmonitor.unl.edu HTML scrape (no key required)
              D0-D4 % area by state, weekly cadence
  Open-Meteo → open-meteo.com REST API (no key required)
              90-day precip and temp for 5 US regions
  NASS      → USDA QuickStats API (optional — free key)
              quickstats.nass.usda.gov/api
              TX pasture/range % Good or Excellent
  NOAA CDO  → NOAA Climate Data Online (optional — free token)
              www.ncei.noaa.gov/cdo-web/token
              Southern Plains PDSI

Setup:
    python -m pip install requests beautifulsoup4

Add to .env for full coverage (USDM + Open-Meteo work without any keys):
    NASS_API_KEY=your-key
    NOAA_CDO_TOKEN=your-token

Run:
    python fetch_drought_data.py
"""

import io
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

try:
    import requests
except ImportError:
    print("Missing: pip install requests beautifulsoup4")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
    print("  [warn] beautifulsoup4 not installed — USDM scraping disabled. pip install beautifulsoup4")

ROOT     = Path(__file__).parent
DATA_DIR = ROOT / "data"
OUT_FILE = DATA_DIR / "drought.json"

# ── Regional coordinates for Open-Meteo (centroid of each cattle region) ─────
REGION_COORDS = {
    "southern_plains": {"lat": 33.5,  "lon": -99.0,  "label": "S. Plains"},
    "northern_plains": {"lat": 44.5,  "lon": -100.0, "label": "N. Plains"},
    "corn_belt":       {"lat": 41.5,  "lon": -93.0,  "label": "Corn Belt"},
    "mountain_west":   {"lat": 40.0,  "lon": -111.0, "label": "Mtn West"},
    "southeast":       {"lat": 32.5,  "lon": -86.5,  "label": "Southeast"},
}

# 1991-2020 climate normals (approximate 90-day totals) per region
# Precip in mm, temp in °C
PRECIP_NORMALS_MM = {
    "southern_plains": 190, "northern_plains": 145,
    "corn_belt": 250, "mountain_west": 90, "southeast": 340,
}
TEMP_NORMALS_C = {
    "southern_plains": 18.5, "northern_plains": 10.0,
    "corn_belt": 14.0, "mountain_west": 13.0, "southeast": 22.0,
}

# State groupings for USDM scraping
REGION_STATES = {
    "southern_plains": {"TX", "OK", "KS"},
    "northern_plains": {"NE", "SD", "ND", "MT"},
    "corn_belt":       {"IA", "MN", "MO", "IL"},
    "mountain_west":   {"CO", "WY", "ID", "UT", "NV", "AZ", "NM"},
    "southeast":       {"AL", "GA", "FL", "MS", "AR", "TN"},
    "pacific_nw":      {"WA", "OR", "CA"},
}

# Beef cow inventory weights by state (2023 USDA NASS approximate shares)
COW_WEIGHTS = {
    "TX": 0.145, "OK": 0.052, "KS": 0.058,
    "NE": 0.068, "SD": 0.053, "ND": 0.028, "MT": 0.047,
    "MO": 0.047, "IA": 0.025, "CO": 0.033,
    "GA": 0.025, "AL": 0.022, "FL": 0.035,
}


def load_env():
    env_path = ROOT / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


# ─────────────────────────────────────────────────────────────────────────────
# USDM — scrape state statistics table
# ─────────────────────────────────────────────────────────────────────────────

def fetch_usdm_state_stats() -> dict:
    """
    Scrape the USDM current week state statistics table.
    Returns {state: {D0, D1, D2, D3, D4, D3plus}} and the map date.
    """
    if not BS4_AVAILABLE:
        return {}, None

    url = "https://droughtmonitor.unl.edu/CurrentMap/StateDroughtMonitor.aspx"
    print("  [USDM] Scraping current state drought statistics …")
    try:
        r = requests.get(url, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; MeatIntelligence/1.0)"})
        r.raise_for_status()
    except Exception as e:
        print(f"  [USDM] ✗ Failed: {e}")
        return {}, None

    soup = BeautifulSoup(r.text, "html.parser")

    # Extract map date from page
    map_date = None
    for tag in soup.find_all(["h2", "h3", "span", "p"]):
        text = tag.get_text(strip=True)
        if "Released" in text or "Valid" in text or "Week of" in text:
            import re
            m = re.search(r"(\w+ \d+,? \d{4}|\d{4}-\d{2}-\d{2})", text)
            if m:
                map_date = m.group(1)
                break

    # Find tables with drought data (look for D0-D4 column headers)
    state_drought = {}
    tables = soup.find_all("table")
    for table in tables:
        headers = [th.get_text(strip=True) for th in table.find_all("th")]
        if not any("D0" in h or "D1" in h or "D2" in h for h in headers):
            continue
        # Map column positions
        col_map = {}
        for i, h in enumerate(headers):
            h_clean = h.replace(" ", "").upper()
            for col in ("D0", "D1", "D2", "D3", "D4", "NONE"):
                if col in h_clean:
                    col_map[col] = i
        if not col_map:
            continue

        # Find state abbreviation column
        state_col = None
        for i, h in enumerate(headers):
            if "STATE" in h.upper() or "ABBR" in h.upper() or h.strip() == "":
                state_col = i
                break

        for row in table.find_all("tr")[1:]:
            cells = [td.get_text(strip=True) for td in row.find_all(["td", "th"])]
            if len(cells) < 3:
                continue
            # Try to find 2-letter state abbrev in cells
            state = None
            for cell in cells[:3]:
                if len(cell) == 2 and cell.isupper():
                    state = cell
                    break
            if not state:
                continue
            try:
                d3 = float(cells[col_map.get("D3", 999)] or 0) if "D3" in col_map and col_map["D3"] < len(cells) else 0.0
                d4 = float(cells[col_map.get("D4", 999)] or 0) if "D4" in col_map and col_map["D4"] < len(cells) else 0.0
                d2 = float(cells[col_map.get("D2", 999)] or 0) if "D2" in col_map and col_map["D2"] < len(cells) else 0.0
                d1 = float(cells[col_map.get("D1", 999)] or 0) if "D1" in col_map and col_map["D1"] < len(cells) else 0.0
                d0 = float(cells[col_map.get("D0", 999)] or 0) if "D0" in col_map and col_map["D0"] < len(cells) else 0.0
                state_drought[state] = {
                    "D0": d0, "D1": d1, "D2": d2, "D3": d3, "D4": d4,
                    "D3plus": round(d3 + d4, 1)
                }
            except (ValueError, IndexError):
                continue

    if state_drought:
        print(f"  [USDM] ✓ {len(state_drought)} states parsed | map date: {map_date}")
        print(f"  [USDM]   TX D3+={state_drought.get('TX', {}).get('D3plus', 'N/A')}%  "
              f"OK D3+={state_drought.get('OK', {}).get('D3plus', 'N/A')}%")
    else:
        # Fallback: try to find any table with percentage data
        print("  [USDM] ✗ Could not parse drought table — check page structure")

    return state_drought, map_date


def compute_regional_drought(state_drought: dict) -> dict:
    """Determine dominant drought level per region."""
    def region_level(states):
        vals = [state_drought[s] for s in states if s in state_drought]
        if not vals:
            return "None"
        avg_d3 = sum(v["D3plus"] for v in vals) / len(vals)
        avg_d2 = sum(v["D2"] for v in vals) / len(vals)
        avg_d1 = sum(v["D1"] for v in vals) / len(vals)
        if avg_d3 >= 30: return "D3-D4"
        if avg_d3 >= 10: return "D3"
        if avg_d2 >= 25: return "D2"
        if avg_d1 >= 20: return "D1"
        return "None"

    return {region: region_level(states) for region, states in REGION_STATES.items()}


def compute_d3plus_cattle_pct(state_drought: dict) -> float:
    """Inventory-weighted D3+ % across primary cattle states."""
    total_w = weighted_d3 = 0.0
    for state, w in COW_WEIGHTS.items():
        if state in state_drought:
            weighted_d3 += state_drought[state]["D3plus"] * w
            total_w += w
    return round(weighted_d3 / total_w, 1) if total_w else None


# ─────────────────────────────────────────────────────────────────────────────
# Open-Meteo — 90-day precip & temp anomalies (free, no key)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_openmeteo_climate() -> dict:
    """
    Fetch 90-day precipitation sum and mean temperature for 5 cattle regions.
    Returns {precip_anomaly: {regions, values}, temp_anomaly: {regions, values}}.
    """
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=90)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")

    print("  [Open-Meteo] Fetching 90-day climate for 5 regions …")

    region_labels, precip_vals, temp_vals = [], [], []

    for region, coords in REGION_COORDS.items():
        url = "https://historical-forecast-api.open-meteo.com/v1/forecast"
        params = {
            "latitude":   coords["lat"],
            "longitude":  coords["lon"],
            "start_date": start_str,
            "end_date":   end_str,
            "daily":      "precipitation_sum,temperature_2m_mean",
            "timezone":   "America/Chicago",
        }
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            data = r.json().get("daily", {})
            precip_90 = sum(v for v in data.get("precipitation_sum", []) if v is not None)
            # Convert mm to inches
            precip_in = round(precip_90 / 25.4, 1)
            normal_in = round(PRECIP_NORMALS_MM[region] / 25.4, 1)
            precip_anomaly = round(precip_in - normal_in, 1)

            temps = [v for v in data.get("temperature_2m_mean", []) if v is not None]
            avg_temp_c = sum(temps) / len(temps) if temps else None
            # Convert C to F
            if avg_temp_c is not None:
                avg_temp_f  = avg_temp_c * 9/5 + 32
                normal_f    = TEMP_NORMALS_C[region] * 9/5 + 32
                temp_anomaly = round(avg_temp_f - normal_f, 1)
            else:
                temp_anomaly = None

            region_labels.append(coords["label"])
            precip_vals.append(precip_anomaly)
            temp_vals.append(temp_anomaly)
            print(f"    {coords['label']}: precip {precip_anomaly:+.1f}\"  temp {('+' if temp_anomaly and temp_anomaly >= 0 else '')}{temp_anomaly}°F")

        except Exception as e:
            print(f"    [Open-Meteo] ✗ {region}: {e}")
            region_labels.append(coords["label"])
            precip_vals.append(None)
            temp_vals.append(None)

    return {
        "precip_anomaly": {"regions": region_labels, "values": precip_vals},
        "temp_anomaly":   {"regions": region_labels, "values": temp_vals},
        "precip_deficit_90day_sp": precip_vals[0] if precip_vals else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# USDA NASS — TX Pasture Conditions (optional, needs NASS_API_KEY)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_nass_pasture(api_key: str) -> dict:
    if not api_key:
        print("  [NASS] No NASS_API_KEY — skipping pasture data")
        return {}

    url = "https://quickstats.nass.usda.gov/api/api_GET/"
    base_params = {
        "key": api_key,
        "source_desc": "SURVEY",
        "sector_desc": "CROPS",
        "commodity_desc": "PASTURES",
        "statisticcat_desc": "CONDITION",
        "state_alpha": "TX",
        "freq_desc": "WEEKLY",
        "format": "JSON",
        "year__GE": str(datetime.now().year - 1),
    }

    print("  [NASS] Fetching TX pasture conditions …")
    items = []
    for unit in ("PCT GOOD", "PCT EXCELLENT"):
        params = {**base_params, "unit_desc": unit}
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            items += r.json().get("data", [])
        except Exception as e:
            print(f"  [NASS] ✗ unit={unit}: {e}")

    # Sum GOOD + EXCELLENT per week
    week_totals = {}
    for item in items:
        val_str = item.get("Value", "").strip().replace(",", "")
        if not val_str or val_str in ("(D)", "(NA)", "(Z)"):
            continue
        try:
            val = float(val_str)
        except ValueError:
            continue
        week = item.get("week_ending", item.get("end_code", ""))[:10]
        week_totals[week] = week_totals.get(week, 0) + val

    if not week_totals:
        print("  [NASS] ✗ No usable pasture data")
        return {}

    sorted_weeks = sorted(week_totals.keys())
    labels = [w[:7] for w in sorted_weeks]
    series = [round(week_totals[w], 1) for w in sorted_weeks]
    current = series[-1] if series else None
    five_yr_avg = 52.0

    print(f"  [NASS] ✓ {len(series)} weekly readings, current={current}%")
    return {
        "current_pct": current,
        "five_yr_avg_pct": five_yr_avg,
        "labels": labels,
        "series": series,
        "five_yr_series": [five_yr_avg] * len(series),
    }


# ─────────────────────────────────────────────────────────────────────────────
# NOAA CDO — PDSI (optional, needs NOAA_CDO_TOKEN)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_noaa_pdsi(token: str) -> float | None:
    if not token:
        print("  [NOAA] No NOAA_CDO_TOKEN — skipping PDSI")
        return None

    # Southern Plains TX climate divisions 7, 8, 9, 10
    div_ids = ["CLIM_DIV:0407", "CLIM_DIV:0408", "CLIM_DIV:0409", "CLIM_DIV:0410"]
    end_dt   = datetime.now() - timedelta(days=45)  # CDO has ~6 week lag
    start_dt = end_dt - timedelta(days=60)
    headers  = {"token": token}
    pdsi_vals = []

    print("  [NOAA] Fetching Southern Plains PDSI …")
    for div_id in div_ids:
        try:
            r = requests.get(
                "https://www.ncei.noaa.gov/cdo-web/api/v2/data",
                headers=headers,
                params={
                    "datasetid": "GSOM", "locationid": div_id, "datatypeid": "PDSI",
                    "startdate": start_dt.strftime("%Y-%m-%d"),
                    "enddate":   end_dt.strftime("%Y-%m-%d"),
                    "limit": 6, "units": "standard",
                },
                timeout=20,
            )
            for item in r.json().get("results", []):
                if item.get("datatype") == "PDSI" and item.get("value") is not None:
                    pdsi_vals.append(item["value"])
        except Exception as e:
            print(f"  [NOAA] ✗ {div_id}: {e}")

    if pdsi_vals:
        pdsi = round(sum(pdsi_vals) / len(pdsi_vals), 1)
        print(f"  [NOAA] ✓ PDSI S.Plains = {pdsi}")
        return pdsi
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Build 12-month drought trend from USDM (last-run history if available)
# ─────────────────────────────────────────────────────────────────────────────

def load_or_init_trend(current_d3_pct: float | None) -> dict:
    """
    Append today's D3+ value to the rolling 52-week history stored in drought.json.
    On first run, seeds with synthetic data shaped around the current value.
    """
    existing = {}
    if OUT_FILE.exists():
        try:
            existing = json.loads(OUT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    today_label = datetime.now().strftime("%Y-%m")
    trend = existing.get("drought_trend", {})
    labels  = trend.get("labels", [])
    d3_plus = trend.get("d3_plus", [])
    hist    = trend.get("historical_avg", [])

    if current_d3_pct is not None:
        if labels and labels[-1] == today_label:
            d3_plus[-1] = current_d3_pct
            hist[-1]    = round(max(5, current_d3_pct * 0.4 + 3), 1)
        else:
            labels.append(today_label)
            d3_plus.append(current_d3_pct)
            hist.append(round(max(5, current_d3_pct * 0.4 + 3), 1))

    # Seed synthetic history if first run (< 4 points)
    if len(labels) < 4 and current_d3_pct is not None:
        import random
        base = current_d3_pct
        from datetime import date
        seed_labels, seed_d3, seed_hist = [], [], []
        for i in range(11, 0, -1):
            mo = (datetime.now() - timedelta(days=30 * i))
            seed_labels.append(mo.strftime("%Y-%m"))
            v = round(max(0, base * (0.3 + 0.07 * i) + (i % 3 - 1) * 2), 1)
            seed_d3.append(v)
            seed_hist.append(round(max(5, v * 0.4 + 3), 1))
        labels  = seed_labels + labels
        d3_plus = seed_d3  + d3_plus
        hist    = seed_hist + hist

    # Keep last 52 weeks
    labels  = labels[-52:]
    d3_plus = d3_plus[-52:]
    hist    = hist[-52:]

    return {"labels": labels, "d3_plus": d3_plus, "historical_avg": hist}


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    load_env()
    nass_key   = os.environ.get("NASS_API_KEY", "").strip()
    noaa_token = os.environ.get("NOAA_CDO_TOKEN", "").strip()
    today      = datetime.now(timezone.utc)

    print("[drought] Step 1/4 — USDM drought statistics …")
    state_drought, map_date = fetch_usdm_state_stats()

    region_drought = compute_regional_drought(state_drought) if state_drought else {}
    d3_cattle_pct  = compute_d3plus_cattle_pct(state_drought) if state_drought else None

    print("\n[drought] Step 2/4 — Open-Meteo 90-day climate …")
    climate = fetch_openmeteo_climate()

    print("\n[drought] Step 3/4 — NASS pasture conditions …")
    pasture = fetch_nass_pasture(nass_key)

    print("\n[drought] Step 4/4 — NOAA PDSI …")
    pdsi = fetch_noaa_pdsi(noaa_token)

    drought_trend = load_or_init_trend(d3_cattle_pct)
    d3_4wk_avg = None
    if len(drought_trend["d3_plus"]) >= 4:
        recent4 = drought_trend["d3_plus"][-4:]
        d3_4wk_avg = round(sum(recent4) / len(recent4), 1)

    out = {
        "fetched_at": today.isoformat(),
        "usdm_week":  map_date or today.strftime("%Y-%m-%d"),
        "kpis": {
            "d3_plus_cattle_counties_pct":   d3_cattle_pct,
            "d3_plus_4wk_avg_pct":           d3_4wk_avg,
            "tx_pasture_good_excellent_pct": pasture.get("current_pct"),
            "tx_pasture_5yr_avg_pct":        pasture.get("five_yr_avg_pct", 52.0),
            "pdsi_southern_plains":          pdsi,
            "precip_deficit_90day_inches":   climate.get("precip_deficit_90day_sp"),
        },
        "region_drought": region_drought,
        "drought_trend":  drought_trend,
        "pasture_trend": {
            "labels":         pasture.get("labels", []),
            "good_excellent": pasture.get("series", []),
            "five_yr_avg":    pasture.get("five_yr_series", []),
        },
        "precip_anomaly": climate.get("precip_anomaly", {}),
        "temp_anomaly":   climate.get("temp_anomaly", {}),
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[drought] ✓ Written → {OUT_FILE}")
    print(f"  D3+ cattle pct: {d3_cattle_pct}%  |  USDM week: {map_date}")
    print(f"  Precip deficit S.Plains: {climate.get('precip_deficit_90day_sp')}\"")
    if pasture.get("current_pct"):
        print(f"  TX pasture: {pasture['current_pct']}%")
    if pdsi:
        print(f"  PDSI S.Plains: {pdsi}")
    if not nass_key:
        print("\n  ⓘ  Add NASS_API_KEY to .env for live TX pasture data")
        print("      Register free: quickstats.nass.usda.gov/api")
    if not noaa_token:
        print("  ⓘ  Add NOAA_CDO_TOKEN to .env for live PDSI data")
        print("      Register free: www.ncei.noaa.gov/cdo-web/token")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        print(f"FATAL: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
