---
name: refresh-dashboard
description: Run the Jack Link's Meat Intelligence Dashboard data pipeline end-to-end. Probes USDA MPR API health first, then runs the daily refresh (download USDA PDFs, parse prices, retrain quantile gradient boosting forecasts, update data/forecasts.json), and reports a clean summary. Invoke when the user says any of "refresh the dashboard", "update USDA data", "run the pipeline", "pull latest prices", "retrain forecasts", "check USDA health", "is USDA up", or types the slash command /refresh-dashboard.
---

# Refresh Dashboard Skill

This skill runs the full MIC data pipeline for the Jack Link's Meat Intelligence Dashboard. It first checks whether USDA infrastructure is healthy, then runs the daily refresh if safe.

## Context

- Project root: `C:\Users\nagar\Downloads\Meat Inteligence Dashboard`
- Pipeline scripts are at the project root
- Daily output: `data/latest.json`, `data/forecasts.json`, `data/history.json`, `update.log`
- Secrets in `.env` (USDA_MPR_API_KEY) — never read or print this value

## Execution steps

Run the steps below in order. **Always run Step 1 first** — it takes ~10 seconds and tells you whether USDA is responding at all.

### Step 1 — Probe USDA health

```powershell
cd "C:\Users\nagar\Downloads\Meat Inteligence Dashboard"
python probe_usda_health.py
```

Capture the "VERDICT" line from the output. The possible verdicts:

| Verdict | Action |
|---|---|
| `API FULLY HEALTHY` | Proceed to Step 2 (full pipeline including backfill is safe) |
| `MPR API BACKEND OUTAGE` | Skip historical backfill — daily PDFs still work. Proceed to Step 2 but warn the user that `backfill_history.py` would fail today |
| `AUTH REJECTED` | **Stop.** Report to user: API key invalid/expired. They need to regenerate at mymarketnews.ams.usda.gov |
| `PARTIAL OUTAGE` | Proceed cautiously. Some reports may fail. Run Step 2 and report which reports succeeded |
| `LOCAL NETWORK ISSUE` | **Stop.** Report to user: check internet/firewall |

### Step 2 — Run the daily pipeline

```powershell
.\run_usda_updater.bat
```

This wraps `update_usda_data.py` (PDF → `latest.json`) and automatically calls `forecast_cuts.py` (retrain GBMs → `forecasts.json`). Takes about 3 minutes total. Output appends to `update.log`.

### Step 3 — Tail the log and report

```powershell
Get-Content update.log -Tail 20
```

Parse and summarize the last run:
- Which USDA reports parsed successfully (look for `✓ parsed N items` lines)
- Which cuts are on GBM vs parametric (look for `GBM active: ...` line)
- Any `✗ FAILED` lines — include the error verbatim

### Step 4 (optional) — Refresh browser preview

If a preview server is running for the dashboard, reload it so the user sees the fresh numbers:

```
window.location.reload()
```

Use the `mcp__Claude_Preview__preview_eval` tool if available; otherwise tell the user to hard-refresh manually (Ctrl+Shift+R).

## Reporting format

Report back to the user in this shape (keep it tight):

```
Pipeline run · <timestamp from latest.json fetched_at>

USDA health:         <verdict>
Reports parsed:      LM_XB401 ✓  LM_XB403 ✓  LM_XB405 ✓  LM_PK602 ✓
Forecast models:     GBM: 85CL, 50CL, 65CL · Parametric: insides, flats, eyes, pork_*
Dashboard:           Reload at http://localhost:3000/remixed-8840b12b.html
```

If anything failed, lead with the failure line.

## Do NOT

- **Do not** run `backfill_history.py` unless the user explicitly asks for a backfill. The daily pipeline does not need it — the backfill is a one-time historical pull.
- **Do not** print the USDA API key value anywhere. If you need to confirm the key is loaded, check length only: `$env:USDA_MPR_API_KEY.Length`.
- **Do not** modify `forecast_cuts.py` priors or the cut config as part of a routine refresh. Those are separate tasks the user must request explicitly.
- **Do not** commit any output files. The pipeline writes JSON that's regenerated each run.

## Troubleshooting shortcuts

| Symptom | Fix |
|---|---|
| `Missing dependencies` | `python -m pip install pypdf requests scikit-learn numpy` |
| `UnicodeEncodeError cp1252` | The `.bat` already sets `PYTHONIOENCODING=utf-8`; if running Python directly, prefix with `$env:PYTHONIOENCODING='utf-8'` |
| Dashboard shows old values after refresh | Hard-refresh browser (Ctrl+Shift+R) — browser cache |
| Green LIVE pill missing after reload | CSS exclusion rule may have been removed — check line ~955 in the HTML for `body > div[style*="position: fixed"][style*="bottom"]:not([data-usda-live-indicator])` |
