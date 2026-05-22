"""
generate_narrative.py
---------------------
Reads data/latest.json and data/forecasts.json, builds a concise market
data summary, calls Claude API to generate a structured JSON market narrative,
and writes the result to data/narrative.json.

Usage:
    python generate_narrative.py

Requires:
    ANTHROPIC_API_KEY environment variable (or .env file via python-dotenv)
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── resolve project root (same directory as this script) ──────────────────────
ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
LATEST_JSON    = DATA_DIR / "latest.json"
FORECASTS_JSON = DATA_DIR / "forecasts.json"
NARRATIVE_JSON = DATA_DIR / "narrative.json"


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def safe_get(obj, *keys, default=None):
    """Safely traverse nested dict/None with a series of keys."""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


def pct_change(new_val, anchor):
    """Return (new_val - anchor) / anchor * 100, or None if anchor is 0/None."""
    if not anchor or not new_val:
        return None
    return round((new_val - anchor) / anchor * 100, 2)


# ─────────────────────────────────────────────────────────────────────────────
# data extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_data_summary(latest: dict, forecasts: dict) -> str:
    """Build a concise plain-text data summary for the Claude prompt."""
    lines = []
    report_date = latest.get("report_date", "unknown date")
    lines.append(f"REPORT DATE: {report_date}")
    lines.append("")

    # ── Beef Manufacturing Cuts (LM_XB401) ────────────────────────────────────
    reports = latest.get("reports", {})
    xb401   = reports.get("LM_XB401", {})

    fresh_85 = xb401.get("fresh_85") or {}
    val_85   = fresh_85.get("weighted_avg_cwt")

    fresh_50 = xb401.get("fresh_50") or {}
    val_50   = fresh_50.get("weighted_avg_cwt")

    # day delta from forecasts.json anchor vs prior_day
    fc_cuts = forecasts.get("forecasts", {})
    fc_85   = fc_cuts.get("85CL", {})
    fc_50   = fc_cuts.get("50CL", {})

    delta_85_pct = fc_85.get("delta_day_pct")   # already computed in forecasts.json
    delta_50_pct = fc_50.get("delta_day_pct")

    lines.append("=== BEEF MANUFACTURING CUTS ===")
    lines.append(f"85CL Lean Ground Beef: ${val_85:.2f}/cwt" + (f"  (day Δ {delta_85_pct:+.2f}%)" if delta_85_pct is not None else "") if val_85 else "85CL: N/A")
    lines.append(f"50CL Fat Trim: ${val_50:.2f}/cwt" + (f"  (day Δ {delta_50_pct:+.2f}%)" if delta_50_pct is not None else "") if val_50 else "50CL: N/A")

    # ── Cow/Bull Combo Cuts (LM_XB405) ───────────────────────────────────────
    xb405       = reports.get("LM_XB405", {})
    insides     = xb405.get("insides_combo") or {}
    flats_eyes  = xb405.get("flats_eyes_combo") or {}

    val_insides    = insides.get("weighted_avg_cwt")
    chg_insides    = insides.get("change_cwt")
    val_flats      = flats_eyes.get("weighted_avg_cwt")
    chg_flats      = flats_eyes.get("change_cwt")

    lines.append(f"Insides Combo: ${val_insides:.2f}/cwt  (day Δ ${chg_insides:+.2f}/cwt)" if val_insides else "Insides Combo: N/A")
    lines.append(f"Flats & Eyes Combo: ${val_flats:.2f}/cwt  (day Δ ${chg_flats:+.2f}/cwt)" if val_flats else "Flats & Eyes Combo: N/A")

    # ── Choice Cutout (LM_XB403) ──────────────────────────────────────────────
    xb403         = reports.get("LM_XB403", {})
    choice_cutout = xb403.get("choice_cutout")
    choice_change = xb403.get("choice_change")

    lines.append(f"Choice Beef Cutout: ${choice_cutout:.2f}/cwt  (day Δ ${choice_change:+.2f}/cwt)" if choice_cutout else "Choice Cutout: N/A")
    lines.append("")

    # ── Pork (LM_PK602) ───────────────────────────────────────────────────────
    pk602          = reports.get("LM_PK602", {})
    ham_insides    = (pk602.get("ham_insides") or {}).get("weighted_avg_cwt")
    ham_knuckles   = (pk602.get("ham_knuckles") or {}).get("weighted_avg_cwt")
    ham_outsides   = (pk602.get("ham_outsides") or {}).get("weighted_avg_cwt")
    pork_carcass   = safe_get(pk602, "primal_cutout", "carcass")

    lines.append("=== PORK CUTS ===")
    lines.append(f"Ham Insides: ${ham_insides:.2f}/cwt" if ham_insides else "Ham Insides: N/A")
    lines.append(f"Ham Knuckles: ${ham_knuckles:.2f}/cwt" if ham_knuckles else "Ham Knuckles: N/A")
    lines.append(f"Ham Outsides: ${ham_outsides:.2f}/cwt" if ham_outsides else "Ham Outsides: N/A")
    lines.append(f"Pork Carcass Cutout: ${pork_carcass:.2f}/cwt" if pork_carcass else "Pork Carcass Cutout: N/A")
    lines.append("")

    # ── 6-Month Forecasts ─────────────────────────────────────────────────────
    lines.append("=== 6-MONTH (26-WEEK) FORECASTS ===")

    forecast_cuts = {
        "85CL":    ("85CL Lean Ground Beef",  fc_cuts.get("85CL")),
        "50CL":    ("50CL Fat Trim",           fc_cuts.get("50CL")),
        "insides": ("Insides Combo",           fc_cuts.get("insides")),
        "flats":   ("Flats & Eyes Combo",      fc_cuts.get("flats")),
    }

    for key, (label, fc) in forecast_cuts.items():
        if not fc:
            lines.append(f"{label}: forecast data not available")
            continue
        anchor    = fc.get("anchor_cwt")
        h26       = fc.get("horizons", {}).get("26w", {})
        p50_26w   = h26.get("p50_cwt")
        high_52w  = fc.get("range_52w_high_cwt")

        if anchor and p50_26w:
            pct = pct_change(p50_26w, anchor)
            at_high = "  ⚠ AT/NEAR 52-WEEK HIGH" if (high_52w and anchor and anchor >= high_52w * 0.98) else ""
            lines.append(f"{label}: anchor ${anchor:.2f}/cwt → 26w P50 ${p50_26w:.2f}/cwt ({pct:+.1f}%){at_high}")
        else:
            lines.append(f"{label}: insufficient forecast data")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Claude API call
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a meat market intelligence analyst generating a procurement briefing "
    "for Jack Link's, a major beef and pork manufacturer. Write in concise, "
    "data-driven, executive style. Analysis is used by procurement directors making "
    "sourcing decisions worth millions of dollars. Always ground analysis in specific "
    "numbers provided. Be direct, specific, actionable. No filler phrases."
)

USER_PROMPT_TEMPLATE = """\
Using the market data below, return ONLY valid JSON (no markdown, no code fences, no commentary — raw JSON only) with this exact structure:

{{
  "situation_headline": "One sentence max 15 words summarizing the single most important market signal",
  "situation_paragraphs": [
    "First paragraph: beef manufacturing cuts current conditions with specific prices and % changes. 2-3 sentences.",
    "Second paragraph: 6-month outlook based on forecasts and structural signals. Specific forecast prices. 2-3 sentences."
  ],
  "actions": {{
    "immediate": ["action 1 (0-4 week horizon)", "action 2", "action 3", "action 4"],
    "near_term": ["action 1 (4-12 week horizon)", "action 2", "action 3", "action 4"],
    "strategic": ["action 1 (12+ week horizon)", "action 2", "action 3"]
  }},
  "regional_risk": [
    {{"region": "Southern Plains", "drought": "D2-D3", "liquidation": "+12%", "lean_avail": 74, "signal": "WATCH / HEDGE", "signal_class": "act-hedge"}},
    {{"region": "Northern Plains", "drought": "None", "liquidation": "Normal", "lean_avail": 99, "signal": "BUY FORWARD ✓", "signal_class": "act-buy"}},
    {{"region": "Corn Belt", "drought": "None", "liquidation": "Normal", "lean_avail": 97, "signal": "MONITOR", "signal_class": "act-watch"}},
    {{"region": "Mountain West", "drought": "D1-D2", "liquidation": "+6%", "lean_avail": 85, "signal": "HEDGE / WATCH", "signal_class": "act-hedge"}},
    {{"region": "Southeast", "drought": "D0-D1", "liquidation": "+2%", "lean_avail": 93, "signal": "NORMAL", "signal_class": "act-watch"}}
  ]
}}

For signal_class use: act-avoid (high risk), act-hedge (moderate), act-buy (opportunity), act-watch (normal).

All regional_risk numbers and signal values should reflect current market intelligence based on the data provided. Use the data below to calibrate your headline, paragraphs, and actions — be specific.

--- MARKET DATA ---
{data_summary}
--- END DATA ---
"""


def call_claude(data_summary: str, api_key: str) -> dict:
    """Call Claude API and return parsed JSON response."""
    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic package not installed. Run: pip install anthropic>=0.25.0", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    user_prompt = USER_PROMPT_TEMPLATE.format(data_summary=data_summary)

    try:
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:
        print(f"ERROR: Claude API call failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    raw_text = message.content[0].text.strip()

    # Strip accidental markdown code fences if Claude added them despite instructions
    if raw_text.startswith("```"):
        lines = raw_text.splitlines()
        # Remove first line (```json or ```) and last ``` line
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw_text = "\n".join(lines).strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Claude returned non-JSON response. Parsing failed: {exc}", file=sys.stderr)
        print(f"Raw response:\n{raw_text}", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # ── 1. Check for API key ──────────────────────────────────────────────────
    # Support loading from .env if python-dotenv is available
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable is not set.", file=sys.stderr)
        print("Set it in your shell or in the .env file at the project root.", file=sys.stderr)
        sys.exit(1)

    # ── 2. Load input data ────────────────────────────────────────────────────
    if not LATEST_JSON.exists():
        print(f"ERROR: {LATEST_JSON} not found. Run update_usda_data.py first.", file=sys.stderr)
        sys.exit(1)
    if not FORECASTS_JSON.exists():
        print(f"ERROR: {FORECASTS_JSON} not found. Run forecast_cuts.py first.", file=sys.stderr)
        sys.exit(1)

    with open(LATEST_JSON, "r", encoding="utf-8") as f:
        latest = json.load(f)
    with open(FORECASTS_JSON, "r", encoding="utf-8") as f:
        forecasts = json.load(f)

    report_date = latest.get("report_date", "unknown")

    # ── 3. Build data summary ─────────────────────────────────────────────────
    print(f"[generate_narrative] Building data summary for {report_date} …")
    data_summary = extract_data_summary(latest, forecasts)
    print(data_summary)
    print()

    # ── 4. Call Claude API ────────────────────────────────────────────────────
    print("[generate_narrative] Calling Claude API (claude-opus-4-5, max_tokens=1500) …")
    narrative = call_claude(data_summary, api_key)

    # ── 5. Enrich with metadata ───────────────────────────────────────────────
    narrative["generated_at"] = datetime.now(timezone.utc).isoformat()
    narrative["report_date"]  = report_date

    # ── 6. Write output ───────────────────────────────────────────────────────
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(NARRATIVE_JSON, "w", encoding="utf-8") as f:
        json.dump(narrative, f, indent=2, ensure_ascii=False)

    print(f"[generate_narrative] ✓ Written to {NARRATIVE_JSON}")
    print(f"  Headline: {narrative.get('situation_headline', '')}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        import traceback
        print(f"FATAL: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
