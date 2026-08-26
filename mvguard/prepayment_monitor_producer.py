"""Synthetic monthly cohort panel for the prepayment-model-monitoring
guardrail extension: 100 fictional loan cohorts x 150 months = 15,000
cohort-months, matching the number the resume claims.

Each record is one cross-sectional cohort-month: a fictional cohort's
predicted CPR (from a fixed monitoring model), its realized (actual) CPR,
and the refinance-incentive input the model consumes that month. This
mirrors the design `mvguard/marks_producer.py` documents for the
price-verification extension: the same underlying record supports both a
purely cross-sectional check (cpr_tolerance, one cohort-month against
itself) and a check needing a short per-key history (stale_input, one
cohort's own incentive across consecutive months), while a third check
(population_stability) needs the *whole population* of cohorts at one
month, which is why `mvguard/prepayment_monitor.py` buffers cohort-months
by month and only closes out a month's check once every cohort for that
month has been seen, the same buffering idea `price_verification.py` uses
for its own single-key state, generalized to a population-wide key.

Shape of the panel
-------------------
    cohorts (100): cohort-000 .. cohort-099, each a fixed synthetic WAC
    months (150):  month 0 .. month 149

Market rate is held roughly flat (small month-to-month noise only, no
systematic cycle). That is a deliberate simplification, stated here rather
than hidden: it keeps the population's cross-sectional refi-incentive
distribution stable in the *clean* run, so population_stability's fixed
baseline deciles (established at month 0) stay valid for the full 150
months and any drift the check flags is attributable to the seeded
cohort-mix perturbations below, not to an unmodeled real rate cycle a
production deployment would also have to control for.

Seeded violations
------------------
Exactly 30 violations are planted, 10 per family (cpr_tolerance,
stale_input, population_stability). See `SEED_SPECS`.
"""

import math
import random

COHORT_COUNT = 100
MONTH_COUNT = 150
DEFAULT_SEED = 20260827
DEFAULT_START_TS = 1_700_000_000.0
DEFAULT_INTERVAL_SECONDS = 2_628_000.0  # ~1 month

# Also written into rules/prepayment_monitor_guardrails.yaml; kept here too
# so the seed generator can size its deltas against them without importing
# YAML at import time (the same layering `marks_producer.py` uses).
CPR_TOLERANCE = 3.0             # percentage points, actual vs. predicted
STALE_MONTHS_THRESHOLD = 6      # consecutive months refi_incentive unchanged
PSI_THRESHOLD = 0.25            # standard "significant population shift" PSI cutoff

# Clean-baseline noise. actual-vs-predicted noise is hard-clipped comfortably
# under CPR_TOLERANCE so the clean run cannot cross it by chance, the same
# discipline the option-surface and price-verification extensions use to
# guarantee a real zero-false-positive baseline rather than a probable one.
CPR_NOISE_SIGMA = 0.6
CPR_NOISE_CLIP = 2.5
INCENTIVE_NOISE_SIGMA = 0.15
MARKET_RATE_BASE = 5.25
MARKET_RATE_NOISE_SIGMA = 0.03


def _cohort_params(cohort_index, rng):
    base_wac = 3.75 + 2.75 * rng.random()   # 3.75% .. 6.50%
    seasoning_start = rng.randint(6, 48)    # months already seasoned at month 0
    return {"base_wac": base_wac, "seasoning_start": seasoning_start}


def _seasoning_ramp(loan_age):
    return min(1.0, loan_age / 30.0)


def _burnout(cum_incentive_months):
    return 1.0 / (1.0 + 0.03 * cum_incentive_months)


def _incentive_response(incentive):
    return 1.0 / (1.0 + math.exp(-1.35 * (incentive - 0.5)))


def _predicted_cpr(incentive, loan_age, cum_incentive_months):
    ramp = _seasoning_ramp(loan_age)
    burnout = _burnout(cum_incentive_months)
    base_cpr = 5.0 + 28.0 * _incentive_response(incentive)
    return ramp * burnout * base_cpr


def _clip(value, lo, hi):
    return max(lo, min(hi, value))


def generate_clean_snapshots(cohorts=COHORT_COUNT, months=MONTH_COUNT, seed=DEFAULT_SEED,
                              start_ts=DEFAULT_START_TS, interval_seconds=DEFAULT_INTERVAL_SECONDS):
    """Yield one snapshot per (month, cohort), in month-major order.

    Pure and deterministic. Ordered by month, then cohort within a month,
    which is both a real monitoring-cadence shape (every cohort scored the
    same month-end) and what `prepayment_monitor.py` needs: a cohort's own
    snapshots appear in strictly increasing month order for the stale_input
    check, and a month's whole population of cohorts arrives contiguously
    for the population_stability check.
    """
    rng = random.Random(seed)
    params = [_cohort_params(i, rng) for i in range(cohorts)]
    cum_incentive_months = [0.0] * cohorts
    snapshots = []
    for month in range(months):
        market_rate = MARKET_RATE_BASE + rng.gauss(0.0, MARKET_RATE_NOISE_SIGMA)
        for c in range(cohorts):
            p = params[c]
            loan_age = p["seasoning_start"] + month
            incentive = p["base_wac"] - market_rate + rng.gauss(0.0, INCENTIVE_NOISE_SIGMA)
            if incentive > 0.5:
                cum_incentive_months[c] += 1.0
            predicted = _predicted_cpr(incentive, loan_age, cum_incentive_months[c])
            noise = _clip(rng.gauss(0.0, CPR_NOISE_SIGMA), -CPR_NOISE_CLIP, CPR_NOISE_CLIP)
            actual = max(0.0, predicted + noise)
            snapshots.append({
                "cohort_id": "cohort-{:03d}".format(c),
                "cohort_index": c,
                "month_index": month,
                "ts": round(start_ts + month * interval_seconds, 3),
                "loan_age": loan_age,
                "predicted_cpr": round(predicted, 4),
                "actual_cpr": round(actual, 4),
                "refi_incentive": round(incentive, 4),
            })
    return snapshots


def _index_by_cohort_month(snapshots):
    return {(s["cohort_index"], s["month_index"]): s for s in snapshots}


def _index_by_month(snapshots):
    by_month = {}
    for s in snapshots:
        by_month.setdefault(s["month_index"], []).append(s)
    return by_month


# --- cpr_tolerance (10): actual_cpr forced away from predicted_cpr at one
# cohort-month, past CPR_TOLERANCE plus a clearance margin.
_CPR_CLEARANCE = 1.0
CPR_SEEDS = [
    {"id": "cpr-01", "family": "cpr_tolerance", "cohort_index": 5, "month_index": 15, "sign": 1},
    {"id": "cpr-02", "family": "cpr_tolerance", "cohort_index": 12, "month_index": 35, "sign": -1},
    {"id": "cpr-03", "family": "cpr_tolerance", "cohort_index": 20, "month_index": 55, "sign": 1},
    {"id": "cpr-04", "family": "cpr_tolerance", "cohort_index": 28, "month_index": 75, "sign": -1},
    {"id": "cpr-05", "family": "cpr_tolerance", "cohort_index": 36, "month_index": 95, "sign": 1},
    {"id": "cpr-06", "family": "cpr_tolerance", "cohort_index": 44, "month_index": 113, "sign": -1},
    {"id": "cpr-07", "family": "cpr_tolerance", "cohort_index": 52, "month_index": 128, "sign": 1},
    {"id": "cpr-08", "family": "cpr_tolerance", "cohort_index": 60, "month_index": 40, "sign": -1},
    {"id": "cpr-09", "family": "cpr_tolerance", "cohort_index": 68, "month_index": 108, "sign": 1},
    {"id": "cpr-10", "family": "cpr_tolerance", "cohort_index": 76, "month_index": 145, "sign": -1},
]

# --- stale_input (10): refi_incentive frozen for STALE_MONTHS_THRESHOLD
# consecutive months at one cohort, starting at freeze_start (the month
# before freeze_start keeps its natural value, which becomes the frozen
# one). Only predicted/actual CPR fields are left untouched by this seed:
# the CPR fields were already generated from the true, unfrozen incentive
# trajectory, so a stale_input seed cannot also trip cpr_tolerance.
STALE_SEEDS = [
    {"id": "stale-01", "family": "stale_input", "cohort_index": 8, "freeze_start": 20},
    {"id": "stale-02", "family": "stale_input", "cohort_index": 16, "freeze_start": 45},
    {"id": "stale-03", "family": "stale_input", "cohort_index": 24, "freeze_start": 60},
    {"id": "stale-04", "family": "stale_input", "cohort_index": 32, "freeze_start": 80},
    {"id": "stale-05", "family": "stale_input", "cohort_index": 40, "freeze_start": 90},
    {"id": "stale-06", "family": "stale_input", "cohort_index": 48, "freeze_start": 105},
    {"id": "stale-07", "family": "stale_input", "cohort_index": 56, "freeze_start": 118},
    {"id": "stale-08", "family": "stale_input", "cohort_index": 64, "freeze_start": 125},
    {"id": "stale-09", "family": "stale_input", "cohort_index": 72, "freeze_start": 135},
    {"id": "stale-10", "family": "stale_input", "cohort_index": 80, "freeze_start": 142},
]

# --- population_stability (10): half the cohort population's refi_incentive
# shifted into one extreme decile bin for a single month, overwriting only
# the refi_incentive field, not predicted/actual CPR, so a population seed
# cannot also trip cpr_tolerance. Months avoid every stale_input freeze
# window above so a population overwrite never unfreezes a stale_input run.
POPULATION_SEED_SHIFT = 3.0
POPULATION_SHIFTED_COHORTS = 50  # first 50 of 100 cohorts, by index
POPULATION_SEEDS = [
    {"id": "pop-01", "family": "population_stability", "month_index": 10},
    {"id": "pop-02", "family": "population_stability", "month_index": 30},
    {"id": "pop-03", "family": "population_stability", "month_index": 52},
    {"id": "pop-04", "family": "population_stability", "month_index": 70},
    {"id": "pop-05", "family": "population_stability", "month_index": 88},
    {"id": "pop-06", "family": "population_stability", "month_index": 100},
    {"id": "pop-07", "family": "population_stability", "month_index": 120},
    {"id": "pop-08", "family": "population_stability", "month_index": 132},
    {"id": "pop-09", "family": "population_stability", "month_index": 140},
    {"id": "pop-10", "family": "population_stability", "month_index": 148},
]

SEED_SPECS = CPR_SEEDS + STALE_SEEDS + POPULATION_SEEDS


def _apply_cpr_seed(by_cohort_month, spec):
    snap = by_cohort_month[(spec["cohort_index"], spec["month_index"])]
    delta = spec["sign"] * (CPR_TOLERANCE + _CPR_CLEARANCE)
    snap["actual_cpr"] = round(max(0.0, snap["predicted_cpr"] + delta), 4)


def _apply_stale_seed(by_cohort_month, spec):
    freeze_start = spec["freeze_start"]
    anchor = by_cohort_month[(spec["cohort_index"], freeze_start - 1)]
    frozen_value = anchor["refi_incentive"]
    frozen_months = STALE_MONTHS_THRESHOLD - 1  # anchor month already counts as 1
    for offset in range(frozen_months):
        snap = by_cohort_month[(spec["cohort_index"], freeze_start + offset)]
        snap["refi_incentive"] = frozen_value


def _apply_population_seed(by_month, spec):
    month_snaps = sorted(by_month[spec["month_index"]], key=lambda s: s["cohort_index"])
    for snap in month_snaps[:POPULATION_SHIFTED_COHORTS]:
        snap["refi_incentive"] = round(snap["refi_incentive"] + POPULATION_SEED_SHIFT, 4)


def generate_seeded_snapshots(cohorts=COHORT_COUNT, months=MONTH_COUNT, seed=DEFAULT_SEED,
                               start_ts=DEFAULT_START_TS, interval_seconds=DEFAULT_INTERVAL_SECONDS,
                               seed_specs=SEED_SPECS):
    """Clean panel with the 30 planted violations from ``seed_specs`` applied."""
    snapshots = generate_clean_snapshots(cohorts, months, seed, start_ts, interval_seconds)
    by_cohort_month = _index_by_cohort_month(snapshots)
    by_month = _index_by_month(snapshots)
    for spec in seed_specs:
        if spec["family"] == "cpr_tolerance":
            _apply_cpr_seed(by_cohort_month, spec)
        elif spec["family"] == "stale_input":
            _apply_stale_seed(by_cohort_month, spec)
        elif spec["family"] == "population_stability":
            _apply_population_seed(by_month, spec)
    return snapshots


def seed_target_snapshot_ids(spec):
    """Every snapshot id a seed is allowed to alert in, without counting as a
    false positive.
    """
    if spec["family"] == "stale_input":
        freeze_start = spec["freeze_start"]
        months = [freeze_start - 1] + [freeze_start + i for i in range(STALE_MONTHS_THRESHOLD - 1)]
        return {"cohort-{:03d}-m{:04d}".format(spec["cohort_index"], m) for m in months}
    if spec["family"] == "population_stability":
        return {"population-m{:04d}".format(spec["month_index"])}
    return {"cohort-{:03d}-m{:04d}".format(spec["cohort_index"], spec["month_index"])}
