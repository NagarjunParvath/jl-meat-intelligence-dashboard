#!/usr/bin/env python3
"""
Jack Link's MIC — Forecast Engine v2.2
=======================================

Per-cut forecaster with two paths:

    1. Quantile Gradient Boosting (sklearn)   ← used when history >= MIN_GBM_OBS
       AND the model beats seasonal-naïve on a held-out walk-forward set.

    2. Parametric seasonal-drift (v1 fallback) ← used when insufficient
       history OR the trained model fails the seasonal-naïve benchmark.

Each cut's forecast is tagged with `model` in the output JSON so you can
see which path was used.

Inputs:
    data/latest.json        anchor prices (from update_usda_data.py)
    data/history.json       accumulated history (from backfill_history.py
                            and daily append_today_to_history)

Output:
    data/forecasts.json     P10/P50/P90 at 1w, 4w, 13w, 26w + 6-month curve

Honest promise:
    No cut's GBM forecast is published unless it first beats the seasonal-
    naïve baseline on held-out data. If the model loses, we say so in the
    `backtest_result` field and fall back to parametric — transparently.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

# Force UTF-8
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / 'data'
HISTORY_PATH = DATA_DIR / 'history.json'
LATEST_PATH = DATA_DIR / 'latest.json'
OUT_PATH = DATA_DIR / 'forecasts.json'

# sklearn + numpy are optional; if missing we gracefully fall back to parametric
try:
    import numpy as np
    from sklearn.ensemble import GradientBoostingRegressor
    SKLEARN_OK = True
except ImportError:
    SKLEARN_OK = False

# ──────────────────────────────────────────────────────────────────────
# CUT CONFIGURATION  (parametric priors per cut; still used as fallback)
# ──────────────────────────────────────────────────────────────────────

CUTS = {
    '85CL': {
        'name': '85CL Lean Ground Beef',
        'source_report': 'LM_XB401',
        'path': ['fresh_85', 'weighted_avg_cwt'],
        'unit': 'cwt',
        'price_lb_divisor': 100,
        'drift_annual': 0.08, 'vol_monthly': 0.035,
        'seasonality_amp': 0.04, 'seasonality_peak_doy': 180,
    },
    '50CL': {
        'name': '50CL Fat Trim',
        'source_report': 'LM_XB401',
        'path': ['fresh_50', 'weighted_avg_cwt'],
        'unit': 'cwt', 'price_lb_divisor': 100,
        'drift_annual': 0.05, 'vol_monthly': 0.055,
        'seasonality_amp': 0.08, 'seasonality_peak_doy': 180,
    },
    '65CL': {
        'name': '65CL Lean',
        'source_report': 'LM_XB401',
        'path': ['fresh_65', 'weighted_avg_cwt'],
        'unit': 'cwt', 'price_lb_divisor': 100,
        'drift_annual': 0.07, 'vol_monthly': 0.045,
        'seasonality_amp': 0.05, 'seasonality_peak_doy': 180,
    },
    'insides': {
        'name': 'Insides — 100VL Inside Round',
        'source_report': 'LM_XB405',
        'path': ['insides_combo', 'weighted_avg_cwt'],
        'unit': 'cwt', 'price_lb_divisor': 100,
        'drift_annual': 0.06, 'vol_monthly': 0.035,
        'seasonality_amp': 0.05, 'seasonality_peak_doy': 150,
    },
    'flats': {
        'name': 'Flats & Eyes Combo — 100VL',
        'source_report': 'LM_XB405',
        'path': ['flats_eyes_combo', 'weighted_avg_cwt'],
        'unit': 'cwt', 'price_lb_divisor': 100,
        'drift_annual': 0.06, 'vol_monthly': 0.04,
        'seasonality_amp': 0.05, 'seasonality_peak_doy': 150,
    },
    'eyes': {
        'name': 'Eye of Round — 171C',
        'source_report': 'LM_XB405',
        'path': ['eye_of_round', 'weighted_avg_cwt'],
        'unit': 'cwt', 'price_lb_divisor': 100,
        'drift_annual': 0.06, 'vol_monthly': 0.05,
        'seasonality_amp': 0.05, 'seasonality_peak_doy': 150,
    },
    'pork_trim_72': {
        'name': '72% Pork Trim',
        'source_report': 'LM_PK602',
        'path': ['trim_72', 'weighted_avg_cwt'],
        'unit': 'cwt', 'price_lb_divisor': 100,
        'drift_annual': 0.02, 'vol_monthly': 0.06,
        'seasonality_amp': 0.10, 'seasonality_peak_doy': 180,
    },
    'pork_trim_42': {
        'name': '42% Pork Trim',
        'source_report': 'LM_PK602',
        'path': ['trim_42', 'weighted_avg_cwt'],
        'unit': 'cwt', 'price_lb_divisor': 100,
        'drift_annual': 0.02, 'vol_monthly': 0.08,
        'seasonality_amp': 0.10, 'seasonality_peak_doy': 180,
    },
    'pork_belly_13_17': {
        'name': 'Pork Belly Derind 13-17#',
        'source_report': 'LM_PK602',
        'path': ['belly_13-17', 'weighted_avg_cwt'],
        'unit': 'cwt', 'price_lb_divisor': 100,
        'drift_annual': 0.00, 'vol_monthly': 0.12,
        'seasonality_amp': 0.15, 'seasonality_peak_doy': 165,
    },
    'pork_loin': {
        'name': 'Pork Loin Primal',
        'source_report': 'LM_PK602',
        'path': ['primal_cutout', 'loin'],
        'unit': 'cwt', 'price_lb_divisor': 100,
        'drift_annual': 0.02, 'vol_monthly': 0.05,
        'seasonality_amp': 0.07, 'seasonality_peak_doy': 180,
    },
}

HORIZONS = {'1w': 7, '4w': 28, '13w': 91, '26w': 182}
CURVE_WEEKS = [0, 4, 8, 13, 17, 21, 26]

Z_80 = 1.2816
SIGMA_CAP = 0.25
MIN_GBM_OBS = 180          # need 6 months of history before we'll even try GBM
MIN_LAG = 60               # longest feature lag
TEST_FRACTION = 0.20       # last 20% reserved for backtest

# v2.2 — Training-data policy
# Only fit on observations from 2023 onward. Pre-2023 data reflects a very
# different supply regime (different herd inventory, different slaughter
# capacity constraints) and would bias the model toward reversion patterns
# that no longer apply. Anything before the cutoff is kept in history.json
# for auditing but excluded from training.
TRAINING_START_DATE = date(2023, 1, 1)

# v2.2 — Recency weighting
# Within the training window, weight the most recent rows more heavily so
# the model favors the current regime's dynamics. Applied as sample_weight
# to every .fit() call.
RECENCY_WEIGHTS_BY_YEAR = {
    2026: 5.0,
    2025: 3.0,
    2024: 2.0,
    2023: 1.0,
}


def _recency_weight(d: date) -> float:
    return RECENCY_WEIGHTS_BY_YEAR.get(d.year, 1.0)


# ──────────────────────────────────────────────────────────────────────
# PARAMETRIC MODEL (v1 fallback — unchanged)
# ──────────────────────────────────────────────────────────────────────

def _dig(obj, path):
    for k in path:
        if obj is None or not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _seasonality(cfg, doy: int) -> float:
    angle = 2 * math.pi * (doy - cfg['seasonality_peak_doy']) / 365.0
    return 1.0 + cfg['seasonality_amp'] * math.cos(angle)


def parametric_forecast(anchor: float, cfg: dict, today: date, horizon_days: int) -> dict:
    years = horizon_days / 365.25
    months = horizon_days / 30.0
    today_doy = today.timetuple().tm_yday
    target_doy = ((today_doy - 1 + horizon_days) % 365) + 1
    base = anchor / _seasonality(cfg, today_doy)
    p50 = base * ((1 + cfg['drift_annual']) ** years) * _seasonality(cfg, target_doy)
    sigma = min(cfg['vol_monthly'] * math.sqrt(max(months, 1 / 30.0)), SIGMA_CAP)
    p10 = p50 * (1 - Z_80 * sigma)
    p90 = p50 * (1 + Z_80 * sigma)
    div = cfg['price_lb_divisor']
    return {
        'p10_cwt': round(p10, 2), 'p50_cwt': round(p50, 2), 'p90_cwt': round(p90, 2),
        'p10_lb': round(p10 / div, 4), 'p50_lb': round(p50 / div, 4), 'p90_lb': round(p90 / div, 4),
    }


# ──────────────────────────────────────────────────────────────────────
# QUANTILE GBM MODEL (new)
# ──────────────────────────────────────────────────────────────────────

FEATURE_NAMES = [
    'lag_1', 'lag_5', 'lag_20', 'lag_60',
    'roll5_mean', 'roll5_std', 'roll20_mean', 'roll20_std', 'roll60_mean',
    'ret_5', 'ret_20',
    'doy_sin', 'doy_cos', 'dow', 'month',
]


def _build_features_at(prices, dates, i: int) -> list:
    """Compute feature vector for observation at index i (requires i >= MIN_LAG)."""
    p = prices
    window5  = p[i-5:i]
    window20 = p[i-20:i]
    window60 = p[i-60:i]
    d = dates[i]
    doy = d.timetuple().tm_yday
    return [
        p[i-1],                                    # lag_1
        p[i-5],                                    # lag_5
        p[i-20],                                   # lag_20
        p[i-60],                                   # lag_60
        float(np.mean(window5)),                   # roll5_mean
        float(np.std(window5)),                    # roll5_std
        float(np.mean(window20)),                  # roll20_mean
        float(np.std(window20)),                   # roll20_std
        float(np.mean(window60)),                  # roll60_mean
        (p[i-1] / p[i-5]) - 1.0 if p[i-5] else 0,  # ret_5
        (p[i-1] / p[i-20]) - 1.0 if p[i-20] else 0,# ret_20
        math.sin(2 * math.pi * doy / 365.0),       # doy_sin
        math.cos(2 * math.pi * doy / 365.0),       # doy_cos
        d.weekday(),                               # dow
        d.month,                                   # month
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


def gbm_forecast_for_cut(series: list, cfg: dict, today: date, verbose: bool = False) -> dict:
    """Return {horizon: {p10_cwt, p50_cwt, p90_cwt, ...}, backtest: {...}} or None if GBM failed backtest."""
    if not SKLEARN_OK:
        return None

    # v2.2 — Filter to recent-regime training window
    series = [r for r in series if date.fromisoformat(r['date']) >= TRAINING_START_DATE]
    n = len(series)
    if n < MIN_GBM_OBS:
        return None

    prices = np.array([r['price_cwt'] for r in series], dtype=float)
    dates = [date.fromisoformat(r['date']) for r in series]

    horizons = HORIZONS
    result = {'horizons': {}, 'curve': [], 'backtest': {}}

    latest_anchor = float(prices[-1])

    horizons_all = dict(horizons)
    # Add curve points so we can overwrite chart lines too
    for w in CURVE_WEEKS:
        key = f'curve_{w}w'
        horizons_all[key] = w * 7

    passed_any = False

    for label, h in horizons_all.items():
        # Need at least MIN_LAG + h + 30-sample test window
        min_train = MIN_LAG + h + 30
        if n < min_train:
            continue

        # Build training set: X[i] = features(i), y[i] = prices[i + h]
        X, y = [], []
        for i in range(MIN_LAG, n - h):
            X.append(_build_features_at(prices, dates, i))
            y.append(prices[i + h])
        X = np.array(X)
        y = np.array(y)

        # Walk-forward split: last TEST_FRACTION as test
        split = int(len(X) * (1 - TEST_FRACTION))
        X_tr, X_te = X[:split], X[split:]
        y_tr, y_te = y[:split], y[split:]

        if len(X_tr) < 50 or len(X_te) < 10:
            continue

        # v2.2 — recency weights on TRAIN split only (test stays uniform-weighted for honest RMSE)
        # Feature row at index (MIN_LAG + j) has its date at dates[MIN_LAG + j]
        train_weights = np.array([_recency_weight(dates[MIN_LAG + j]) for j in range(len(X_tr))])

        # Fit 3 quantile models
        m50 = _fit_quantile(X_tr, y_tr, 0.5, sample_weight=train_weights)
        m10 = _fit_quantile(X_tr, y_tr, 0.1, sample_weight=train_weights)
        m90 = _fit_quantile(X_tr, y_tr, 0.9, sample_weight=train_weights)

        # Seasonal-naïve baseline: price[t - 365 + h] as prediction for price[t+h]
        # which in our feature layout is: naive[i] = prices[i + h - 365] if available else prices[i]
        baseline_preds = []
        for i in range(MIN_LAG + split, n - h):
            target_offset = i + h - 365
            if 0 <= target_offset < n:
                baseline_preds.append(prices[target_offset])
            else:
                baseline_preds.append(prices[i])   # naive "last value" fallback
        baseline_preds = np.array(baseline_preds[:len(y_te)])

        gbm_preds = m50.predict(X_te)
        gbm_rmse = _rmse(gbm_preds, y_te)
        base_rmse = _rmse(baseline_preds, y_te) if len(baseline_preds) == len(y_te) else float('inf')

        gbm_wins = gbm_rmse < base_rmse

        # Only for real horizons, record backtest
        if label in horizons:
            result['backtest'][label] = {
                'gbm_rmse_cwt': round(gbm_rmse, 2),
                'seasonal_naive_rmse_cwt': round(base_rmse, 2),
                'gbm_beats_baseline': bool(gbm_wins),
                'test_size': len(y_te),
            }
            if verbose:
                print(f'    {label:4s}  GBM rmse {gbm_rmse:.2f}  vs naive {base_rmse:.2f}  → {"✓ use GBM" if gbm_wins else "✗ fallback"}')

        if not gbm_wins:
            continue

        passed_any = True

        # Predict from latest features (use today's data)
        last_idx = n - 1
        if last_idx < MIN_LAG:
            continue
        feat_now = np.array([_build_features_at(prices, dates, last_idx)])
        # v2.2 — recency-weighted refit on full (filtered) dataset for the live forecast
        full_weights = np.array([_recency_weight(dates[MIN_LAG + j]) for j in range(len(X))])
        full_m50 = _fit_quantile(X, y, 0.5, sample_weight=full_weights)
        full_m10 = _fit_quantile(X, y, 0.1, sample_weight=full_weights)
        full_m90 = _fit_quantile(X, y, 0.9, sample_weight=full_weights)
        p50 = float(full_m50.predict(feat_now)[0])
        p10 = float(full_m10.predict(feat_now)[0])
        p90 = float(full_m90.predict(feat_now)[0])

        # Ensure monotone ordering (quantile crossing correction)
        p10, p50, p90 = sorted([p10, p50, p90])

        div = cfg['price_lb_divisor']
        pack = {
            'p10_cwt': round(p10, 2), 'p50_cwt': round(p50, 2), 'p90_cwt': round(p90, 2),
            'p10_lb': round(p10 / div, 4), 'p50_lb': round(p50 / div, 4), 'p90_lb': round(p90 / div, 4),
        }
        if label in horizons:
            result['horizons'][label] = pack
        else:
            # curve point
            week = int(label.replace('curve_', '').replace('w', ''))
            result['curve'].append({'week': week, **pack})

    if not passed_any:
        return None

    # Sort curve by week
    result['curve'].sort(key=lambda x: x['week'])
    return result


# ──────────────────────────────────────────────────────────────────────
# HISTORY MANAGEMENT
# ──────────────────────────────────────────────────────────────────────

def _load_history() -> dict:
    if not HISTORY_PATH.exists():
        return {}
    try:
        return json.loads(HISTORY_PATH.read_text(encoding='utf-8'))
    except json.JSONDecodeError:
        return {}


def _append_today(history: dict, latest: dict, today: date) -> dict:
    iso = today.isoformat()
    reports = latest.get('reports', {})
    changed = False
    for cut_key, cfg in CUTS.items():
        report = reports.get(cfg['source_report'], {})
        anchor = _dig(report, cfg['path'])
        if anchor is None:
            continue
        series = history.setdefault(cut_key, [])
        # Upsert
        if series and series[-1].get('date') == iso:
            series[-1]['price_cwt'] = anchor
        else:
            series.append({'date': iso, 'price_cwt': anchor})
            changed = True
    if changed:
        HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding='utf-8')
    return history


# ──────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────

def _build_curve_parametric(anchor, cfg, today) -> list:
    return [
        {
            'week': w,
            **{k: v for k, v in parametric_forecast(anchor, cfg, today, w * 7).items()
               if k in ('p10_cwt', 'p50_cwt', 'p90_cwt', 'p10_lb', 'p50_lb', 'p90_lb')}
        }
        for w in CURVE_WEEKS
    ]


def _enrich_from_history(series: list, cfg: dict, anchor: float) -> dict:
    """Compute prior-day value, 52-week range, and 6-month monthly chart history.

    All values reported in both cwt and $/lb form for convenience. When history
    is thin, missing fields stay None (dashboard will fall back to hardcoded).
    """
    if not series:
        return {}
    div = cfg['price_lb_divisor']

    # Prior day — second-to-last observation, if today is the last
    prior_cwt = None
    if len(series) >= 2:
        last = series[-1]
        if last.get('date') == date.today().isoformat():
            # Today's row present → use previous
            prior_cwt = float(series[-2].get('price_cwt'))
        else:
            # Today not yet in series → last IS yesterday
            prior_cwt = float(series[-1].get('price_cwt'))

    # 52-week range: min/max of last 365 calendar days
    today_iso = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=365)).isoformat()
    recent = [r for r in series if r.get('date') >= cutoff and r.get('price_cwt') is not None]
    range_low = min(r['price_cwt'] for r in recent) if recent else None
    range_high = max(r['price_cwt'] for r in recent) if recent else None

    # Chart history: 6 evenly-spaced monthly anchor points ending at today
    # Take the last observation of each of the prior 6 calendar months
    chart_hist_cwt = []
    today_d = date.today()
    for i in range(5, -1, -1):     # months_back = 5,4,3,2,1,0
        target_month_end = (today_d.replace(day=1) - timedelta(days=1))  # last of prev month
        # Step back i months from current month start
        y, m = today_d.year, today_d.month - i
        while m <= 0:
            m += 12
            y -= 1
        # End of target month = start of next month - 1 day
        if m == 12:
            month_end = date(y + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(y, m + 1, 1) - timedelta(days=1)
        month_end_iso = month_end.isoformat()
        # Find the latest observation on or before month_end
        candidates = [r for r in series if r.get('date') <= month_end_iso and r.get('price_cwt') is not None]
        if candidates:
            chart_hist_cwt.append(round(float(candidates[-1]['price_cwt']), 2))
        else:
            chart_hist_cwt.append(None)

    # Anchor the final point to today's value for visual continuity
    if chart_hist_cwt:
        chart_hist_cwt[-1] = round(float(anchor), 2)

    return {
        'prior_day_cwt': round(prior_cwt, 2) if prior_cwt else None,
        'prior_day_lb': round(prior_cwt / div, 4) if prior_cwt else None,
        'range_52w_low_cwt': round(range_low, 2) if range_low else None,
        'range_52w_high_cwt': round(range_high, 2) if range_high else None,
        'range_52w_low_lb': round(range_low / div, 4) if range_low else None,
        'range_52w_high_lb': round(range_high / div, 4) if range_high else None,
        'range_52w_points': len(recent),
        'chart_hist_cwt': chart_hist_cwt,
        'chart_hist_lb': [round(v / div, 4) if v is not None else None for v in chart_hist_cwt],
        'delta_day_cwt': round(anchor - prior_cwt, 2) if prior_cwt else None,
        'delta_day_pct': round((anchor / prior_cwt - 1) * 100, 2) if prior_cwt else None,
    }


def main():
    DATA_DIR.mkdir(exist_ok=True)
    if not LATEST_PATH.exists():
        print('  → forecast_cuts: data/latest.json missing — run update_usda_data.py first')
        return

    latest = json.loads(LATEST_PATH.read_text(encoding='utf-8'))
    today = date.today()

    history = _load_history()
    history = _append_today(history, latest, today)

    forecasts = {}
    skipped = []
    gbm_active = []
    parametric_only = []

    for cut_key, cfg in CUTS.items():
        report = latest.get('reports', {}).get(cfg['source_report'], {})
        anchor = _dig(report, cfg['path'])
        if anchor is None or anchor <= 0:
            skipped.append(cut_key)
            continue

        series = history.get(cut_key, [])
        hist_len = len(series)

        forecast_entry = {
            'name': cfg['name'],
            'source_report': cfg['source_report'],
            'anchor_cwt': anchor,
            'anchor_lb': round(anchor / cfg['price_lb_divisor'], 4),
            'unit': cfg['unit'],
            'history_points': hist_len,
        }
        forecast_entry.update(_enrich_from_history(series, cfg, anchor))

        gbm_out = gbm_forecast_for_cut(series, cfg, today, verbose=False) if hist_len >= MIN_GBM_OBS else None

        if gbm_out:
            forecast_entry['model'] = 'quantile_gbm_v1'
            forecast_entry['horizons'] = gbm_out['horizons']
            forecast_entry['curve'] = gbm_out['curve']
            forecast_entry['backtest'] = gbm_out['backtest']
            gbm_active.append(cut_key)
        else:
            # Parametric fallback
            forecast_entry['model'] = 'parametric_seasonal_drift_v1'
            forecast_entry['horizons'] = {lbl: parametric_forecast(anchor, cfg, today, days) for lbl, days in HORIZONS.items()}
            forecast_entry['curve'] = _build_curve_parametric(anchor, cfg, today)
            forecast_entry['priors'] = {
                'drift_annual': cfg['drift_annual'], 'vol_monthly': cfg['vol_monthly'],
                'seasonality_amp': cfg['seasonality_amp'], 'seasonality_peak_doy': cfg['seasonality_peak_doy'],
            }
            parametric_only.append(cut_key)

        forecasts[cut_key] = forecast_entry

    out = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'report_date': latest.get('report_date'),
        'sklearn_available': SKLEARN_OK,
        'min_gbm_obs': MIN_GBM_OBS,
        'model_note': (
            'Per-cut: quantile Gradient Boosting trained on real history when '
            f'>= {MIN_GBM_OBS} observations AND beats seasonal-naïve baseline on '
            'held-out walk-forward test. Otherwise parametric seasonal-drift fallback. '
            'Every cut shows its `model` tag and — if GBM — its `backtest` RMSE comparison.'
        ),
        'convention': 'Best = P10 · Base = P50 · Worst = P90 (80% central band).',
        'cuts_on_gbm': gbm_active,
        'cuts_on_parametric': parametric_only,
        'forecasts': forecasts,
        'skipped': skipped,
    }

    OUT_PATH.write_text(json.dumps(out, indent=2), encoding='utf-8')
    print(f'  ✓ forecast_cuts: {len(forecasts)} cuts → {OUT_PATH.name}')
    if gbm_active:
        print(f'    GBM active:  {", ".join(gbm_active)}')
    if parametric_only:
        print(f'    parametric:  {", ".join(parametric_only)}')
    if skipped:
        print(f'    skipped:     {", ".join(skipped)}')
    if not SKLEARN_OK:
        print('  ⚠ scikit-learn not installed — all cuts on parametric. Install with: python -m pip install scikit-learn numpy')


if __name__ == '__main__':
    main()
