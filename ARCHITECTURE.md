# Jack Link's Meat Intelligence Dashboard
## Architecture & Operations

**Last updated:** 2026-04-19
**Pipeline version:** v2 (real-history quantile GBM)
**Author:** Nagarjun Parvath, with Claude

---

## 1. Executive Summary

An end-to-end forecasting system that ingests USDA daily price reports, trains quantile gradient-boosting models on five years of real history, and renders decision-grade forecasts into a static HTML dashboard — all driven by one batch file that runs daily at 3:30 PM Central.

**What's decision-grade today:** 85CL / 50CL / 65CL beef trim prices, forecasts, and 52-week ranges.
**What's still scaffolded:** 100VL items, pork, and boxed-beef primals (awaiting second backfill pass).

---

## 2. System Architecture

```
┌────────────────────────────────────────────────────────────────────────────────────┐
│                          USDA AGRICULTURAL MARKETING SERVICE                       │
│                                                                                    │
│   ┌──────────────────────┐                    ┌──────────────────────┐             │
│   │  Daily PDF Reports   │                    │  MPR DataMart API    │             │
│   │  (ams.usda.gov)      │                    │  (historical JSON)   │             │
│   │                      │                    │                      │             │
│   │  LM_XB401 · Trim     │                    │  GET /reports/{slug} │             │
│   │  LM_XB403 · Boxed    │                    │      /{section}      │             │
│   │  LM_XB405 · Cow/100VL│                    │  Basic Auth (API key)│             │
│   │  LM_PK602 · Pork     │                    │                      │             │
│   └──────────┬───────────┘                    └──────────┬───────────┘             │
│              │ daily 2 PM ET                              │ on-demand              │
└──────────────│──────────────────────────────────────────│────────────────────────┘
               │                                            │
               ▼                                            ▼
 ┌───────────────────────────────┐           ┌──────────────────────────────────┐
 │  update_usda_data.py          │           │  backfill_history.py             │
 │                               │           │                                  │
 │  1. Fetch 4 PDFs (requests)   │           │  1. Discover sections per report │
 │  2. Parse with pypdf          │           │  2. Year-chunked date range pulls│
 │  3. Regex → structured JSON   │           │  3. Regex-match item_desc → cut  │
 │  4. Write data/latest.json    │           │  4. Merge into data/history.json │
 └───────────────┬───────────────┘           └──────────────────┬───────────────┘
                 │                                              │
                 └────────────────────┬─────────────────────────┘
                                      ▼
                     ┌────────────────────────────────────┐
                     │     forecast_cuts.py               │
                     │                                    │
                     │     per cut:                       │
                     │       history >= 180 obs? ─── yes ─┼──► quantile GBM (sklearn)
                     │            │                       │      train 3 models (α=0.1,0.5,0.9)
                     │            │                       │      walk-forward vs seasonal-naïve
                     │            │                       │      if beats → publish
                     │            no                      │
                     │            ▼                       │
                     │     parametric seasonal-drift      │
                     │                                    │
                     │     enrichments (always):          │
                     │       prior_day, 52-week range,    │
                     │       6-month chart history        │
                     │                                    │
                     │     write data/forecasts.json      │
                     └───────────────────┬────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────┐
                     │     remixed-8840b12b.html          │
                     │     (browser)                      │
                     │                                    │
                     │     on page load, two IIFEs run:   │
                     │       1. Fetch data/latest.json    │
                     │          → update Market Close     │
                     │            tiles, KPI cards,       │
                     │            availability strips     │
                     │       2. Fetch data/forecasts.json │
                     │          → update Best/Base/Worst  │
                     │            tiles, chart forecast   │
                     │            lines, chart hist,      │
                     │            prior_day, 52-week      │
                     │                                    │
                     │     floating pill shows            │
                     │     "LIVE USDA DATA · REFRESHED X" │
                     └────────────────────────────────────┘
```

---

## 3. Data Flow — End to End

```
  ┌─ INGESTION ──────────────────────────────────────────────────────────┐
  │                                                                      │
  │  2:00 PM ET:  USDA AMS publishes 4 PDFs + MPR DataMart JSON updates  │
  │  3:30 PM CT:  Windows Task Scheduler fires run_usda_updater.bat      │
  │                                                                      │
  │  update_usda_data.py:                                                │
  │  ┌────────────────────────────────────────────────────────────────┐  │
  │  │ for each report in [XB401, XB403, XB405, PK602]:               │  │
  │  │   r = requests.get(pdf_url, timeout=45)                        │  │
  │  │   text = pypdf.PdfReader(pdf).pages → extract_text()           │  │
  │  │   parsed = PARSERS[code](text)   # report-specific regex       │  │
  │  │   data['reports'][code] = parsed                               │  │
  │  │ write data/latest.json                                         │  │
  │  └────────────────────────────────────────────────────────────────┘  │
  │                                                                      │
  └──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌─ ENRICHMENT & FORECASTING ───────────────────────────────────────────┐
  │                                                                      │
  │  forecast_cuts.py:                                                   │
  │  ┌────────────────────────────────────────────────────────────────┐  │
  │  │ history = load data/history.json                               │  │
  │  │ append today's anchor from latest.json to history (idempotent) │  │
  │  │                                                                │  │
  │  │ for each cut in CUTS:                                          │  │
  │  │   n = len(history[cut])                                        │  │
  │  │   if n >= 180 AND sklearn_ok:                                  │  │
  │  │     for h in [7, 28, 91, 182, ...]:                            │  │
  │  │       X, y = build_features(history[cut])                      │  │
  │  │       split 80/20 walk-forward                                 │  │
  │  │       m10 = GradientBoostingRegressor(loss='quantile',α=0.1)   │  │
  │  │       m50 = same, α=0.5                                        │  │
  │  │       m90 = same, α=0.9                                        │  │
  │  │       .fit(X_train, y_train)                                   │  │
  │  │       gbm_rmse = RMSE(m50.predict(X_test), y_test)             │  │
  │  │       naive_rmse = RMSE(price[i-365+h], y_test)                │  │
  │  │       if gbm_rmse < naive_rmse:   ← honesty gate               │  │
  │  │         retrain on full dataset                                │  │
  │  │         publish (p10, p50, p90) for horizon h                  │  │
  │  │       else: fall through to parametric                         │  │
  │  │                                                                │  │
  │  │   enrich_from_history(series, anchor):                         │  │
  │  │     prior_day    = series[-2] if series[-1] is today           │  │
  │  │     52w_low/high = min/max of last 365 days                    │  │
  │  │     chart_hist   = last observation of each of past 6 months   │  │
  │  │                                                                │  │
  │  │ write data/forecasts.json                                      │  │
  │  └────────────────────────────────────────────────────────────────┘  │
  │                                                                      │
  └──────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
  ┌─ PRESENTATION ───────────────────────────────────────────────────────┐
  │                                                                      │
  │  Browser opens remixed-8840b12b.html                                 │
  │  Two IIFEs in <script> block at end of file run in sequence:         │
  │                                                                      │
  │    (async function loadUSDA() {                                      │
  │      const d = await fetch('./data/latest.json').then(r=>r.json());  │
  │      patch Market Close tile current prices                          │
  │      patch beef trim KPI cards                                       │
  │      patch fat/trim availability strip                               │
  │      buildMktCloseGrid() + buildExecCutGrid()                        │
  │      render floating pill: LIVE USDA DATA · REFRESHED <time>         │
  │    })();                                                             │
  │                                                                      │
  │    (async function loadForecasts() {                                 │
  │      const f = await fetch('./data/forecasts.json').then(r=>r.json());│
  │      for each .cut-card matching CUT_MAP:                            │
  │        overwrite Best/Base/Worst tiles with P10/P50/P90              │
  │        overwrite chart datasets[1..3] with curve P10/P50/P90         │
  │        overwrite chart dataset[0] with real chart_hist               │
  │      for each Market Close tile:                                     │
  │        patch prevClose with prior_day_cwt                            │
  │        patch wk52lo/wk52hi with range_52w_low/high                   │
  │      call buildMktCloseGrid() + buildExecCutGrid() to re-render      │
  │    })();                                                             │
  │                                                                      │
  └──────────────────────────────────────────────────────────────────────┘
```

---

## 4. Model Training — The Honest Details

### 4.1 Why quantile GBM (and not XGBoost, LSTM, or ARIMA)

| Option | Why not |
|--------|---------|
| ARIMAX | Can't capture non-linear regime shifts (drought × herd cycle); weak with exogenous features |
| LSTM / TFT | Data-hungry; ~1,200 daily obs is too few for deep models to beat simpler ones |
| XGBoost | Marginally faster than sklearn but adds dependency; sklearn wheels exist for ARM64 |
| **sklearn GradientBoostingRegressor** | **✅ Native quantile loss, ARM64 wheels, ~0.5s fit on 1,200×15 matrix** |

### 4.2 Features (15 per observation)

```python
def _build_features_at(prices, dates, i):
    return [
        prices[i-1],                               # lag_1  (yesterday)
        prices[i-5],                               # lag_5  (1 week ago)
        prices[i-20],                              # lag_20 (1 month ago)
        prices[i-60],                              # lag_60 (quarter ago)
        float(np.mean(prices[i-5:i])),             # roll5_mean
        float(np.std(prices[i-5:i])),              # roll5_std
        float(np.mean(prices[i-20:i])),            # roll20_mean
        float(np.std(prices[i-20:i])),             # roll20_std
        float(np.mean(prices[i-60:i])),            # roll60_mean
        (prices[i-1] / prices[i-5]) - 1.0,         # ret_5   (recent return)
        (prices[i-1] / prices[i-20]) - 1.0,        # ret_20  (monthly return)
        math.sin(2 * math.pi * doy / 365.0),       # doy_sin (seasonality)
        math.cos(2 * math.pi * doy / 365.0),       # doy_cos
        dates[i].weekday(),                        # dow
        dates[i].month,                            # month
    ]
```

**What's deliberately excluded (and why):**
- **Exogenous variables** (drought index, cattle-on-feed, corn futures) — v3 work. Not in v2 to keep the baseline honest and debuggable.
- **Lagged forecasts from other models** — keep it single-model for now.
- **Price levels** from other cuts — could leak across cuts at inference time.

### 4.3 Direct multi-step forecasting (not recursive)

For each forecast horizon `h ∈ {7, 28, 91, 182}` we train a **separate** set of 3 quantile models:

```
training pair at time t:
  X_t = features(prices[0:t], dates[0:t])
  y_t = prices[t + h]

model_{h,α}.fit(X_train, y_train)
```

At prediction time we pass **today's** features into `model_{h,0.5}` to get P50, and into `model_{h,0.1}` / `model_{h,0.9}` for the band. This is simpler and more honest than recursive prediction (which compounds errors).

### 4.4 Backtest gate (the honesty mechanism)

Before any GBM forecast ships, it must beat the seasonal-naïve baseline on held-out data:

```python
split = int(len(X) * 0.80)               # last 20% reserved
X_tr, X_te = X[:split], X[split:]
y_tr, y_te = y[:split], y[split:]

m50.fit(X_tr, y_tr)
gbm_preds = m50.predict(X_te)
gbm_rmse = RMSE(gbm_preds, y_te)

# Seasonal naïve: predict y[i+h] ≈ price at same day 1 year ago
baseline_preds = [prices[i + h - 365] for i in test_indices]
naive_rmse = RMSE(baseline_preds, y_te)

if gbm_rmse < naive_rmse:
    publish GBM forecast
else:
    fall back to parametric for this cut + horizon
```

Every forecast carries its backtest evidence in `forecasts.json`:

```json
"backtest": {
  "1w":  { "gbm_rmse_cwt": 48.64, "seasonal_naive_rmse_cwt": 86.04, "gbm_beats_baseline": true, "test_size": 230 },
  "4w":  { "gbm_rmse_cwt": 49.79, "seasonal_naive_rmse_cwt": 86.53, "gbm_beats_baseline": true, "test_size": 226 },
  "13w": { "gbm_rmse_cwt": 67.75, "seasonal_naive_rmse_cwt": 87.77, "gbm_beats_baseline": true, "test_size": 213 },
  "26w": { "gbm_rmse_cwt": 45.51, "seasonal_naive_rmse_cwt": 84.34, "gbm_beats_baseline": true, "test_size": 195 }
}
```

### 4.5 Parametric fallback (for cuts without history)

For cuts where we don't yet have ≥180 observations, we use a closed-form parametric model with business-interpretable priors:

```python
def parametric_forecast(anchor, cfg, today, horizon_days):
    years = horizon_days / 365.25
    today_doy  = today.timetuple().tm_yday
    target_doy = (today_doy + horizon_days) % 365

    # Strip current seasonality, apply drift, re-apply target-date seasonality
    base = anchor / _seasonality(cfg, today_doy)
    p50  = base * (1 + cfg['drift_annual'])**years * _seasonality(cfg, target_doy)

    # Widen bands with sqrt(time)
    sigma = min(cfg['vol_monthly'] * math.sqrt(horizon_days/30), 0.25)
    p10   = p50 * (1 - 1.2816 * sigma)    # 1.2816 = inverse normal CDF at 0.9
    p90   = p50 * (1 + 1.2816 * sigma)
    return {'p10_cwt': p10, 'p50_cwt': p50, 'p90_cwt': p90}
```

Every parameter (`drift_annual`, `vol_monthly`, `seasonality_amp`, `seasonality_peak_doy`) is a business-challengeable prior set in `forecast_cuts.py:CUTS`. See [Section 8](#8-tuning-the-business-priors) for how to challenge them.

### 4.6 Results from today's training (85CL / 50CL / 65CL)

| Cut | n obs | 1w RMSE (GBM vs naïve) | 4w (GBM vs naïve) | 13w | 26w |
|-----|------:|-----------------------:|------------------:|----:|----:|
| 85CL | 1,214 | **$48.64 vs $86.04** (+44%) | $49.79 vs $86.53 (+42%) | $67.75 vs $87.77 (+23%) | $45.51 vs $84.34 (+46%) |
| 50CL | 1,178 | **$27.32 vs $86.59** (+68%) | $51.84 vs $87.45 (+41%) | $72.25 vs $89.19 (+19%) | $65.54 vs $83.05 (+21%) |
| 65CL |   697 | **$18.50 vs $70.43** (+74%) | $41.47 vs $70.17 (+41%) | $61.46 vs $68.52 (+10%) | $47.87 vs $68.58 (+30%) |

All three cuts beat baseline on every horizon. 1-week forecasts show the largest lift (+44–74%) because short-horizon features (lag_1, roll5) capture the most actionable signal.

---

## 5. Code Structure

```
Meat Inteligence Dashboard/
├── remixed-8840b12b.html          — dashboard SPA (12 tabs, Chart.js lines, Tailwind-ish)
├── update_usda_data.py            — daily PDF → latest.json
├── forecast_cuts.py               — GBM+parametric → forecasts.json + history enrichments
├── backfill_history.py            — 5yr USDA MPR API → history.json
├── env_loader.py                  — minimal .env reader (no external dep)
├── run_usda_updater.bat           — one-click daily pipeline
├── .env                           — USDA_MPR_API_KEY (gitignored)
├── .env.example                   — template for teammates
├── .gitignore                     — excludes secrets + local data
├── ARCHITECTURE.md                — this file
├── README_USDA_UPDATER.md         — operational runbook
├── data/
│   ├── latest.json                — today's USDA values (auto)
│   ├── history.json               — accumulated 5yr series (auto)
│   └── forecasts.json             — model output for dashboard (auto)
├── .tmp/                          — transient PDFs + API probe dumps (auto-cleaned)
└── update.log                     — rolling run log (auto)
```

### 5.1 Module responsibilities

| Module | Role | Called by |
|--------|------|-----------|
| `env_loader.py` | Read `.env`, populate `os.environ` without a dependency | backfill, forecast |
| `update_usda_data.py` | PDF download + parsing → `latest.json` + triggers forecaster | `run_usda_updater.bat` |
| `forecast_cuts.py` | History append + GBM train + parametric fallback + enrichments → `forecasts.json` | `update_usda_data.py` |
| `backfill_history.py` | USDA MPR API backfill (one-time per report) → `history.json` | Manual |
| `remixed-8840b12b.html` | Dashboard; reads `latest.json` + `forecasts.json` at load | Browser |

### 5.2 Key data contracts

#### `data/latest.json`
```json
{
  "fetched_at": "2026-04-19T00:14:27",
  "report_date": "April 17, 2026",
  "reports": {
    "LM_XB401": {
      "fresh_85": { "trades": 22, "pounds": 586265, "weighted_avg_cwt": 392.92, "price_lb": 3.9292 },
      ...
    },
    "LM_XB403": { "choice_cutout": 381.06, "primal_rib": { "choice": 520.76, "select": 502.40 }, ... },
    "LM_XB405": { "cutout_value": 347.33, "insides_combo": { "weighted_avg_cwt": 515.06, ... } },
    "LM_PK602": { "primal_cutout": { "loin": 90.55, ... }, "trim_72": { "weighted_avg_cwt": 102.34, ... } }
  }
}
```

#### `data/history.json`
```json
{
  "85CL": [
    { "date": "2021-04-19", "price_cwt": 288.5, "range_low": 280.0, "range_high": 295.0, "trades": 12, "pounds": 450000 },
    { "date": "2021-04-20", "price_cwt": 290.2, ... },
    ...
    { "date": "2026-04-19", "price_cwt": 392.92 }
  ],
  "50CL": [ ... ],
  ...
}
```

#### `data/forecasts.json`
```json
{
  "generated_at": "2026-04-19T00:14:27",
  "report_date": "April 17, 2026",
  "model_family": "mixed",
  "cuts_on_gbm": ["85CL", "50CL", "65CL"],
  "cuts_on_parametric": ["insides", "flats", "eyes", "pork_trim_72", ...],
  "forecasts": {
    "85CL": {
      "anchor_cwt": 392.92,
      "model": "quantile_gbm_v1",
      "history_points": 1214,
      "horizons": {
        "1w":  { "p10_cwt": 379.36, "p50_cwt": 388.30, "p90_cwt": 393.19, ... },
        "4w":  { ... },
        "13w": { ... },
        "26w": { "p10_cwt": 368.28, "p50_cwt": 388.45, "p90_cwt": 390.71, ... }
      },
      "curve": [
        { "week": 0,  "p10_cwt": 392.92, ... },
        { "week": 4,  "p10_cwt": ..., ... },
        ...
        { "week": 26, "p10_cwt": 368.28, ... }
      ],
      "backtest": {
        "1w":  { "gbm_rmse_cwt": 48.64, "seasonal_naive_rmse_cwt": 86.04, "gbm_beats_baseline": true },
        ...
      },
      "prior_day_cwt": 392.92,
      "range_52w_low_cwt": 314.28,
      "range_52w_high_cwt": 405.42,
      "chart_hist_cwt": [364.51, 356.24, 370.48, 385.49, 396.86, 392.92]
    }
  }
}
```

---

## 6. Operational Runbook

### 6.1 Daily automated run

Task Scheduler (`taskschd.msc`) runs `run_usda_updater.bat` at 3:30 PM Central. The bat:

```batch
@echo off
cd /d "C:\Users\nagar\Downloads\Meat Inteligence Dashboard"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
echo. >> update.log
echo Run started: %date% %time% >> update.log
"C:\Users\nagar\AppData\Local\Programs\Python\Python312-arm64\python.exe" update_usda_data.py >> update.log 2>&1
echo Run finished: %date% %time% >> update.log
```

### 6.2 Manual run (any time)

```powershell
cd "C:\Users\nagar\Downloads\Meat Inteligence Dashboard"
.\run_usda_updater.bat
```

Result in `update.log`:
```
→ LM_XB401   ✓ parsed 4 items
→ LM_XB403   ✓ parsed 20 items
→ LM_XB405   ✓ parsed 14 items
→ LM_PK602   ✓ parsed 7 items
✓ forecast_cuts: 10 cuts → forecasts.json
  GBM active:  85CL, 50CL, 65CL
  parametric:  insides, flats, eyes, pork_trim_72, pork_trim_42, pork_belly_13_17, pork_loin
```

### 6.3 One-time backfill (when USDA API is up)

```powershell
python backfill_history.py               # 5 years, all reports, ~15 min
python backfill_history.py --only XB405  # just one report
python backfill_history.py --verbose     # show per-chunk progress
```

After a successful backfill, re-run `python forecast_cuts.py` — new cuts with ≥180 obs will auto-upgrade from parametric to GBM.

### 6.4 Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Missing dependencies` | Fresh install | `python -m pip install pypdf requests scikit-learn numpy` |
| `cryptography` / Rust compile errors | Bad package install attempt | `pypdf` is pure Python — use it, not `pdfplumber` |
| `UnicodeEncodeError` with cp1252 in log | Windows default encoding vs → ✓ chars | Ensure `.bat` sets `PYTHONIOENCODING=utf-8` |
| `HTTP 500` on every MPR endpoint | USDA API maintenance (typically weekends) | Retry on a business day |
| `USDA_MPR_API_KEY missing` | `.env` not loaded | Confirm file exists and line is `USDA_MPR_API_KEY=...` with no quotes |
| Dashboard shows hardcoded values | `forecasts.json` not loading | Hard-refresh browser (Ctrl+Shift+R); check console for `[Forecast loader]` messages |
| Green "LIVE" pill missing | CSS rule hiding fixed-position bottom divs | Pill has `data-usda-live-indicator` attribute; the hide rule now excludes it |

---

## 7. Data Quality & Honesty

Every data element on the dashboard falls into one of three tiers:

| Tier | Share | Trust level | Examples |
|------|------:|-------------|----------|
| 🟢 Decision-grade | ~52% | Procure against these numbers | Today's USDA prices, backtested 85CL/50CL/65CL forecasts, their 52-week ranges, chart histories |
| 🟡 Directional | ~20% | Trust direction, not exact values | Parametric forecasts for Insides, Flats, Eyes, pork cuts (until those histories are backfilled) |
| 🔴 Narrative / placeholder | ~28% | Context only | International sourcing tables, drought indices, procurement playbook text, alerts |

**The dashboard explicitly tags which is which.** A "CONFIRMED" green badge on a tile means the number came from today's USDA PDF. A `model: "quantile_gbm_v1"` field in `forecasts.json` means that cut passed the backtest gate. A `model: "parametric_seasonal_drift_v1"` means it's still an informed estimate.

### 7.1 Example — how to tell what's real for any tile

- **Green pill "LIVE USDA DATA · REFRESHED <timestamp>"** in bottom-right → today's pipeline completed successfully.
- **"CONFIRMED" badge** on a cut card → the anchor price is from today's USDA PDF.
- **Chart history line (solid)** → real if `forecasts.json.forecasts.<cut>.chart_hist_cwt` has 6 non-null values; fabricated if any are null.
- **Best/Base/Worst tile values** → real quantile GBM if `forecasts.json.forecasts.<cut>.model == "quantile_gbm_v1"`; parametric otherwise.
- **Market Close tile 52-week range** → real if `forecasts.json.forecasts.<cut>.range_52w_points >= 100`; fabricated otherwise.

---

## 8. Tuning the Business Priors

Every parametric forecast uses four business-interpretable priors set in `forecast_cuts.py:CUTS`:

```python
CUTS = {
    '85CL': {
        'drift_annual':       0.08,    # +8% annualized drift (herd-low regime)
        'vol_monthly':        0.035,   # ~3.5% monthly 1σ
        'seasonality_amp':    0.04,    # ±4% peak-to-trough
        'seasonality_peak_doy': 180,   # late June grilling season
    },
    ...
}
```

**How to challenge them in review:**
- `drift_annual`: Is 85CL genuinely drifting +8%/yr, or is the herd-low regime steeper?
- `vol_monthly`: Does 3.5% monthly σ look right on your 5-year chart? Higher in 2025 than 2021.
- `seasonality_amp`: Is peak-trough 4% for 85CL, or closer to 2%?
- `seasonality_peak_doy`: Does the mid-year surge peak on June 29 (DOY 180) or earlier for some cuts?

Edit the values, re-run `python forecast_cuts.py`, refresh the dashboard. Takes ~60 seconds.

---

## 9. Evolution Path

| Version | Status | Description |
|---------|--------|-------------|
| v1 — parametric only | ✅ shipped | Seasonal-drift model with industry priors |
| v2 — GBM on 85CL/50CL/65CL | ✅ shipped | Real history for beef trim; quantile GBM with backtest gate |
| **v2.1 — all 10 cuts on GBM** | 🚧 blocked (USDA API) | Backfill LM_XB405 + LM_PK602 + LM_XB403 |
| v2.2 — recency-weighted training | 📋 planned | Weight 2025-2026 rows 5× to fix long-horizon pessimism |
| v3 — exogenous features | 📋 planned | Drought index (USDM), CME live cattle futures, corn futures, cattle-on-feed |
| v3.1 — multi-cut joint model | 📋 planned | TFT or DeepAR across all cuts; leverage cross-cut correlations |
| v4 — ensemble | 📋 planned | Stack ARIMA + GBM + TFT with ridge meta-learner (M5 competition pattern) |

---

## 10. Quick Reference

### One command you'll actually run
```powershell
.\run_usda_updater.bat
```

### Inspect latest forecast output
```powershell
Get-Content data\forecasts.json | Select-String -Pattern "anchor_cwt|model|backtest|gbm_rmse" | Select-Object -First 20
```

### See last 15 lines of any run
```powershell
Get-Content update.log -Tail 15
```

### Reset forecasts.json (fallback to hardcoded values)
```powershell
Remove-Item data\forecasts.json   # dashboard loader gracefully falls back
```

### Force parametric-only mode
Set `MIN_GBM_OBS = 99999` in `forecast_cuts.py` and re-run — no GBM will engage.

---

**Questions?**  The code is the documentation. Every module has a header docstring explaining its role, and every function has a single clear job. Start at `run_usda_updater.bat` and follow the control flow to `remixed-8840b12b.html`.
