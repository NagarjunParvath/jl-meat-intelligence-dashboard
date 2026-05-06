# Fabric Notebook: NB_Forecast
# ------------------------------
# Attach this notebook to: MeatIntelligenceLH
# Runs AFTER NB_Ingest_USDA completes (chained in the Data Pipeline)
# Replaces: forecast_cuts.py
#
# HOW TO USE:
#   1. In Fabric workspace -> New -> Notebook -> rename to NB_Forecast
#   2. Attach MeatIntelligenceLH via the Lakehouse picker (top-left)
#   3. Copy each CELL block below into a separate notebook cell
#   4. This notebook is triggered automatically by PL_Daily_USDA_Refresh pipeline

# =============================================================================
# CELL 1 — Install dependencies
# =============================================================================
# %pip install scikit-learn numpy scipy --quiet


# =============================================================================
# CELL 2 — Imports and cut configuration
# =============================================================================
import math
from datetime import datetime, date, timedelta
from pyspark.sql import SparkSession
from pyspark.sql.types import (
    StructType, StructField, StringType, DoubleType, IntegerType
)

spark = SparkSession.builder.getOrCreate()

try:
    import numpy as np
    from sklearn.ensemble import GradientBoostingRegressor
    SKLEARN_OK = True
except ImportError:
    SKLEARN_OK = False
    print("WARNING: scikit-learn not available — all cuts will use parametric fallback.")

# Per-cut configuration: parametric priors + path into usda_prices_daily
CUTS = {
    '85CL': {
        'name': '85CL Lean Ground Beef',
        'source_report': 'LM_XB401', 'metric_name': 'fresh_85',
        'price_lb_divisor': 100,
        'drift_annual': 0.08, 'vol_monthly': 0.035,
        'seasonality_amp': 0.04, 'seasonality_peak_doy': 180,
    },
    '50CL': {
        'name': '50CL Fat Trim',
        'source_report': 'LM_XB401', 'metric_name': 'fresh_50',
        'price_lb_divisor': 100,
        'drift_annual': 0.05, 'vol_monthly': 0.055,
        'seasonality_amp': 0.08, 'seasonality_peak_doy': 180,
    },
    '65CL': {
        'name': '65CL Lean',
        'source_report': 'LM_XB401', 'metric_name': 'fresh_65',
        'price_lb_divisor': 100,
        'drift_annual': 0.07, 'vol_monthly': 0.045,
        'seasonality_amp': 0.05, 'seasonality_peak_doy': 180,
    },
    'insides': {
        'name': 'Insides — 100VL Inside Round',
        'source_report': 'LM_XB405', 'metric_name': 'insides_combo',
        'price_lb_divisor': 100,
        'drift_annual': 0.06, 'vol_monthly': 0.035,
        'seasonality_amp': 0.05, 'seasonality_peak_doy': 150,
    },
    'flats': {
        'name': 'Flats & Eyes Combo — 100VL',
        'source_report': 'LM_XB405', 'metric_name': 'flats_eyes_combo',
        'price_lb_divisor': 100,
        'drift_annual': 0.06, 'vol_monthly': 0.04,
        'seasonality_amp': 0.05, 'seasonality_peak_doy': 150,
    },
    'eyes': {
        'name': 'Eye of Round — 171C',
        'source_report': 'LM_XB405', 'metric_name': 'eye_of_round',
        'price_lb_divisor': 100,
        'drift_annual': 0.06, 'vol_monthly': 0.05,
        'seasonality_amp': 0.05, 'seasonality_peak_doy': 150,
    },
    'pork_trim_72': {
        'name': '72% Pork Trim',
        'source_report': 'LM_PK602', 'metric_name': 'trim_72',
        'price_lb_divisor': 100,
        'drift_annual': 0.02, 'vol_monthly': 0.06,
        'seasonality_amp': 0.10, 'seasonality_peak_doy': 180,
    },
    'pork_trim_42': {
        'name': '42% Pork Trim',
        'source_report': 'LM_PK602', 'metric_name': 'trim_42',
        'price_lb_divisor': 100,
        'drift_annual': 0.02, 'vol_monthly': 0.08,
        'seasonality_amp': 0.10, 'seasonality_peak_doy': 180,
    },
    'pork_belly_13_17': {
        'name': 'Pork Belly Derind 13-17#',
        'source_report': 'LM_PK602', 'metric_name': 'belly_13-17',
        'price_lb_divisor': 100,
        'drift_annual': 0.00, 'vol_monthly': 0.12,
        'seasonality_amp': 0.15, 'seasonality_peak_doy': 165,
    },
    'pork_loin': {
        'name': 'Pork Loin Primal',
        'source_report': 'LM_PK602', 'metric_name': 'primal_cutout_loin',
        'price_lb_divisor': 100,
        'drift_annual': 0.02, 'vol_monthly': 0.05,
        'seasonality_amp': 0.07, 'seasonality_peak_doy': 180,
    },
}

HORIZONS      = {'1w': 7, '4w': 28, '13w': 91, '26w': 182}
CURVE_WEEKS   = [0, 4, 8, 13, 17, 21, 26]
Z_80          = 1.2816
SIGMA_CAP     = 0.25
MIN_GBM_OBS   = 180
MIN_LAG       = 60
TEST_FRACTION = 0.20
TRAINING_START_DATE = date(2023, 1, 1)
RECENCY_WEIGHTS_BY_YEAR = {2026: 5.0, 2025: 3.0, 2024: 2.0, 2023: 1.0}

print("Config loaded. sklearn available:", SKLEARN_OK)


# =============================================================================
# CELL 3 — Read inputs from Delta tables
# =============================================================================
# History: full series per cut from backfill + daily appends
history_pdf = spark.sql("""
    SELECT cut_key, obs_date, price_cwt
    FROM usda_price_history
    WHERE price_cwt IS NOT NULL
    ORDER BY cut_key, obs_date
""").toPandas()

# Today's anchor prices from the ingest notebook
anchor_pdf = spark.sql("""
    SELECT report_code, metric_name, price_cwt
    FROM usda_prices_daily
    WHERE fetched_at = (SELECT MAX(fetched_at) FROM usda_prices_daily)
      AND price_cwt IS NOT NULL
""").toPandas()

# Build lookup: (report_code, metric_name) -> price_cwt
anchor_map = {
    (row['report_code'], row['metric_name']): row['price_cwt']
    for _, row in anchor_pdf.iterrows()
}

# Build history lookup: cut_key -> list of {date, price_cwt}
history_map = {}
for cut_key, grp in history_pdf.groupby('cut_key'):
    history_map[cut_key] = [
        {'date': r['obs_date'], 'price_cwt': r['price_cwt']}
        for _, r in grp.iterrows()
    ]

today = date.today()
print(f"Anchors available: {len(anchor_map)}")
print(f"History series:    {len(history_map)}")
print(f"Forecast date:     {today}")


# =============================================================================
# CELL 4 — Parametric and GBM forecast functions
# =============================================================================

def _seasonality(cfg, doy: int) -> float:
    angle = 2 * math.pi * (doy - cfg['seasonality_peak_doy']) / 365.0
    return 1.0 + cfg['seasonality_amp'] * math.cos(angle)


def parametric_forecast(anchor: float, cfg: dict, today: date, horizon_days: int) -> dict:
    years      = horizon_days / 365.25
    months     = horizon_days / 30.0
    today_doy  = today.timetuple().tm_yday
    target_doy = ((today_doy - 1 + horizon_days) % 365) + 1
    base       = anchor / _seasonality(cfg, today_doy)
    p50        = base * ((1 + cfg['drift_annual']) ** years) * _seasonality(cfg, target_doy)
    sigma      = min(cfg['vol_monthly'] * math.sqrt(max(months, 1 / 30.0)), SIGMA_CAP)
    p10        = p50 * (1 - Z_80 * sigma)
    p90        = p50 * (1 + Z_80 * sigma)
    div        = cfg['price_lb_divisor']
    return {
        'p10_cwt': round(p10, 2), 'p50_cwt': round(p50, 2), 'p90_cwt': round(p90, 2),
        'p10_lb':  round(p10 / div, 4), 'p50_lb': round(p50 / div, 4), 'p90_lb': round(p90 / div, 4),
    }


def _recency_weight(d: date) -> float:
    return RECENCY_WEIGHTS_BY_YEAR.get(d.year, 1.0)


def _build_features_at(prices, dates, i: int) -> list:
    p       = prices
    w5, w20, w60 = p[i-5:i], p[i-20:i], p[i-60:i]
    d       = dates[i]
    doy     = d.timetuple().tm_yday
    return [
        p[i-1], p[i-5], p[i-20], p[i-60],
        float(np.mean(w5)), float(np.std(w5)),
        float(np.mean(w20)), float(np.std(w20)),
        float(np.mean(w60)),
        (p[i-1] / p[i-5])  - 1.0 if p[i-5]  else 0,
        (p[i-1] / p[i-20]) - 1.0 if p[i-20] else 0,
        math.sin(2 * math.pi * doy / 365.0),
        math.cos(2 * math.pi * doy / 365.0),
        d.weekday(),
        d.month,
    ]


def _fit_quantile(X, y, alpha: float, sample_weight=None):
    return GradientBoostingRegressor(
        loss='quantile', alpha=alpha,
        n_estimators=200, max_depth=4, learning_rate=0.05,
        min_samples_leaf=15, subsample=0.8, random_state=42,
    ).fit(X, y, sample_weight=sample_weight)


def _rmse(a, b) -> float:
    a, b = np.asarray(a), np.asarray(b)
    return float(np.sqrt(np.mean((a - b) ** 2)))


def gbm_forecast(series: list, cfg: dict, today: date):
    if not SKLEARN_OK:
        return None
    series = [r for r in series if date.fromisoformat(r['date']) >= TRAINING_START_DATE]
    n = len(series)
    if n < MIN_GBM_OBS:
        return None

    prices = np.array([r['price_cwt'] for r in series], dtype=float)
    dates  = [date.fromisoformat(r['date']) for r in series]

    horizons_all = dict(HORIZONS)
    for w in CURVE_WEEKS:
        horizons_all[f'curve_{w}w'] = w * 7

    result     = {'horizons': {}, 'curve': [], 'backtest': {}}
    passed_any = False

    for label, h in horizons_all.items():
        min_train = MIN_LAG + h + 30
        if n < min_train:
            continue

        X, y = [], []
        for i in range(MIN_LAG, n - h):
            X.append(_build_features_at(prices, dates, i))
            y.append(prices[i + h])
        X = np.array(X)
        y = np.array(y)

        split = int(len(X) * (1 - TEST_FRACTION))
        X_tr, X_te = X[:split], X[split:]
        y_tr, y_te = y[:split], y[split:]
        if len(X_tr) < 50 or len(X_te) < 10:
            continue

        train_weights = np.array([_recency_weight(dates[MIN_LAG + j]) for j in range(len(X_tr))])
        m50 = _fit_quantile(X_tr, y_tr, 0.5, sample_weight=train_weights)
        m10 = _fit_quantile(X_tr, y_tr, 0.1, sample_weight=train_weights)
        m90 = _fit_quantile(X_tr, y_tr, 0.9, sample_weight=train_weights)

        baseline_preds = []
        for i in range(MIN_LAG + split, n - h):
            offset = i + h - 365
            baseline_preds.append(prices[offset] if 0 <= offset < n else prices[i])
        baseline_preds = np.array(baseline_preds[:len(y_te)])

        gbm_preds = m50.predict(X_te)
        gbm_rmse  = _rmse(gbm_preds, y_te)
        base_rmse = _rmse(baseline_preds, y_te) if len(baseline_preds) == len(y_te) else float('inf')
        gbm_wins  = gbm_rmse < base_rmse

        if label in HORIZONS:
            result['backtest'][label] = {
                'gbm_rmse_cwt': round(gbm_rmse, 2),
                'seasonal_naive_rmse_cwt': round(base_rmse, 2),
                'gbm_beats_baseline': bool(gbm_wins),
                'test_size': int(len(y_te)),
            }

        if not gbm_wins:
            continue
        passed_any = True

        last_idx = n - 1
        if last_idx < MIN_LAG:
            continue
        feat_now     = np.array([_build_features_at(prices, dates, last_idx)])
        full_weights = np.array([_recency_weight(dates[MIN_LAG + j]) for j in range(len(X))])
        full_m50     = _fit_quantile(X, y, 0.5, sample_weight=full_weights)
        full_m10     = _fit_quantile(X, y, 0.1, sample_weight=full_weights)
        full_m90     = _fit_quantile(X, y, 0.9, sample_weight=full_weights)
        p10, p50, p90 = sorted([
            float(full_m10.predict(feat_now)[0]),
            float(full_m50.predict(feat_now)[0]),
            float(full_m90.predict(feat_now)[0]),
        ])

        div  = cfg['price_lb_divisor']
        pack = {
            'p10_cwt': round(p10, 2), 'p50_cwt': round(p50, 2), 'p90_cwt': round(p90, 2),
            'p10_lb':  round(p10 / div, 4), 'p50_lb': round(p50 / div, 4), 'p90_lb': round(p90 / div, 4),
        }
        if label in HORIZONS:
            result['horizons'][label] = pack
        else:
            week = int(label.replace('curve_', '').replace('w', ''))
            result['curve'].append({'week': week, **pack})

    if not passed_any:
        return None
    result['curve'].sort(key=lambda x: x['week'])
    return result


def enrich_from_history(series: list, cfg: dict, anchor: float) -> dict:
    if not series:
        return {}
    div = cfg['price_lb_divisor']

    prior_cwt = None
    if len(series) >= 2:
        last = series[-1]
        prior_cwt = float(
            series[-2]['price_cwt'] if last.get('date') == date.today().isoformat()
            else series[-1]['price_cwt']
        )

    cutoff = (date.today() - timedelta(days=365)).isoformat()
    recent = [r for r in series if r.get('date', '') >= cutoff and r.get('price_cwt') is not None]
    range_low  = min(r['price_cwt'] for r in recent) if recent else None
    range_high = max(r['price_cwt'] for r in recent) if recent else None

    chart_hist_cwt = []
    today_d = date.today()
    for i in range(5, -1, -1):
        y, m = today_d.year, today_d.month - i
        while m <= 0:
            m += 12; y -= 1
        month_end = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) - timedelta(days=1)
        month_end_iso = month_end.isoformat()
        cands = [r for r in series if r.get('date', '') <= month_end_iso and r.get('price_cwt') is not None]
        chart_hist_cwt.append(round(float(cands[-1]['price_cwt']), 2) if cands else None)
    if chart_hist_cwt:
        chart_hist_cwt[-1] = round(float(anchor), 2)

    return {
        'prior_day_cwt':     round(prior_cwt, 2) if prior_cwt else None,
        'prior_day_lb':      round(prior_cwt / div, 4) if prior_cwt else None,
        'range_52w_low_cwt': round(range_low, 2) if range_low else None,
        'range_52w_high_cwt':round(range_high, 2) if range_high else None,
        'delta_day_cwt':     round(anchor - prior_cwt, 2) if prior_cwt else None,
        'delta_day_pct':     round((anchor / prior_cwt - 1) * 100, 2) if prior_cwt else None,
        'chart_hist_cwt':    chart_hist_cwt,
    }


print("Forecast functions loaded.")


# =============================================================================
# CELL 5 — Run forecasts for all cuts
# =============================================================================
import json as _json

generated_at   = datetime.utcnow().isoformat(timespec='seconds')
forecast_rows  = []   # rows for Delta table
skipped        = []
gbm_active     = []
parametric_only = []

for cut_key, cfg in CUTS.items():
    anchor = anchor_map.get((cfg['source_report'], cfg['metric_name']))
    if anchor is None or anchor <= 0:
        skipped.append(cut_key)
        continue

    series   = history_map.get(cut_key, [])
    hist_len = len(series)
    enrich   = enrich_from_history(series, cfg, anchor)

    gbm_out = gbm_forecast(series, cfg, today) if hist_len >= MIN_GBM_OBS else None

    if gbm_out:
        model_type = 'quantile_gbm_v1'
        horizons   = gbm_out['horizons']
        backtest   = gbm_out['backtest']
        gbm_active.append(cut_key)
    else:
        model_type = 'parametric_seasonal_drift_v1'
        horizons   = {lbl: parametric_forecast(anchor, cfg, today, days) for lbl, days in HORIZONS.items()}
        backtest   = {}
        parametric_only.append(cut_key)

    for horizon_label, h_vals in horizons.items():
        bt = backtest.get(horizon_label, {})
        forecast_rows.append((
            cut_key,
            cfg['name'],
            cfg['source_report'],
            str(today),
            generated_at,
            horizon_label,
            HORIZONS.get(horizon_label, 0),
            float(anchor),
            round(float(anchor) / cfg['price_lb_divisor'], 4),
            h_vals.get('p10_cwt'),
            h_vals.get('p50_cwt'),
            h_vals.get('p90_cwt'),
            h_vals.get('p10_lb'),
            h_vals.get('p50_lb'),
            h_vals.get('p90_lb'),
            model_type,
            int(hist_len),
            enrich.get('prior_day_cwt'),
            enrich.get('delta_day_cwt'),
            enrich.get('delta_day_pct'),
            enrich.get('range_52w_low_cwt'),
            enrich.get('range_52w_high_cwt'),
            bt.get('gbm_rmse_cwt'),
            bt.get('seasonal_naive_rmse_cwt'),
            bt.get('gbm_beats_baseline'),
        ))

print(f"Forecast rows built: {len(forecast_rows)}")
print(f"  GBM active:   {gbm_active}")
print(f"  Parametric:   {parametric_only}")
print(f"  Skipped:      {skipped}")


# =============================================================================
# CELL 6 — Write forecasts to Delta table
# =============================================================================
SCHEMA = StructType([
    StructField("cut_key",               StringType(),  False),
    StructField("cut_name",              StringType(),  True),
    StructField("source_report",         StringType(),  True),
    StructField("forecast_date",         StringType(),  False),
    StructField("generated_at",          StringType(),  True),
    StructField("horizon_label",         StringType(),  False),
    StructField("horizon_days",          IntegerType(), True),
    StructField("anchor_cwt",            DoubleType(),  True),
    StructField("anchor_lb",             DoubleType(),  True),
    StructField("p10_cwt",               DoubleType(),  True),
    StructField("p50_cwt",               DoubleType(),  True),
    StructField("p90_cwt",               DoubleType(),  True),
    StructField("p10_lb",                DoubleType(),  True),
    StructField("p50_lb",                DoubleType(),  True),
    StructField("p90_lb",                DoubleType(),  True),
    StructField("model_type",            StringType(),  True),
    StructField("history_points",        IntegerType(), True),
    StructField("prior_day_cwt",         DoubleType(),  True),
    StructField("delta_day_cwt",         DoubleType(),  True),
    StructField("delta_day_pct",         DoubleType(),  True),
    StructField("range_52w_low_cwt",     DoubleType(),  True),
    StructField("range_52w_high_cwt",    DoubleType(),  True),
    StructField("backtest_gbm_rmse",     DoubleType(),  True),
    StructField("backtest_naive_rmse",   DoubleType(),  True),
    StructField("backtest_gbm_wins",     StringType(),  True),   # "True"/"False"/None
])

fdf = spark.createDataFrame(
    [(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10],
      r[11], r[12], r[13], r[14], r[15], r[16], r[17], r[18], r[19], r[20],
      r[21], r[22], r[23], str(r[24]) if r[24] is not None else None)
     for r in forecast_rows],
    schema=SCHEMA
)

TABLE = "usda_forecasts"

if not spark.catalog.tableExists(TABLE):
    fdf.write.format("delta").saveAsTable(TABLE)
    print(f"Table '{TABLE}' created.")
else:
    fdf.createOrReplaceTempView("_new_forecasts")
    spark.sql(f"""
        MERGE INTO {TABLE} AS tgt
        USING _new_forecasts AS src
          ON  tgt.cut_key       = src.cut_key
         AND  tgt.forecast_date = src.forecast_date
         AND  tgt.horizon_label = src.horizon_label
        WHEN MATCHED     THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)
    print(f"Upserted {fdf.count()} rows into '{TABLE}'.")

spark.sql(f"""
    SELECT cut_key, model_type, horizon_label,
           anchor_cwt, p10_cwt, p50_cwt, p90_cwt
    FROM {TABLE}
    WHERE forecast_date = (SELECT MAX(forecast_date) FROM {TABLE})
    ORDER BY cut_key, horizon_days
""").show(100, truncate=False)
