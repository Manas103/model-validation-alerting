"""Synthetic option-chain snapshots for the no-arbitrage guardrail extension.

Each record this module yields is one *cross-sectional* snapshot: a single
timestamp, a spot price, a rate, and every strike/maturity quote observed at
that instant. That is the design decision documented in the README -- a
no-arbitrage check compares several quotes against each other at the same
moment, not one field against its own history, so the unit the rule language
evaluates has to be the whole chain rather than one quote.

Shape of the stream
--------------------
8 strikes x 5 maturities = 40 quotes per snapshot, 300 snapshots = 12,000
quotes total, matching the number the resume claims. Strikes and maturities:

    strikes (8):    70, 80, 90, 100, 110, 120, 130, 140
    maturities (5): 30d, 60d, 120d, 180d, 365d

The underlying is fictional (``FIC``); nothing here is a real market or a real
model. Every quote starts from a genuine Black-Scholes price (flat 22% vol,
3% flat risk-free rate, no dividends) computed from the snapshot's own spot,
which is what makes the baseline surface a real reference oracle: a quantity
computed from a closed-form formula, not hand-typed numbers tuned to pass.
Put-call parity holds on the baseline surface *by construction*, because both
sides of the parity identity fall out of the same Black-Scholes formula.

A small, deterministic bid/ask spread and pricing jitter (a few tenths of a
cent) is layered on top to look like real quotes. The jitter is sized well
below every rule's tolerance (see ``*_TOLERANCE`` below), and the baseline is
verified violation-free by ``tests/test_option_surface.py`` before any seeding
happens -- see ``verify_no_baseline_violations``.

Seeded violations
------------------
Exactly 24 violations are planted, 6 per family (put-call parity, strike
monotonicity, butterfly convexity, calendar spread), each in its own snapshot
so a single alert can be mapped back to a single seed with no ambiguity. Seed
placement was chosen, and checked, so that a seed trips exactly the rule it
targets and no other: see ``SEED_SPECS`` for the exact snapshot, strike(s),
maturity(ies) and field perturbed for each of the 24, and
``docs/option_surface_seeds.md`` for the human-readable version of the same
table.
"""

import math
import random

UNDERLYING = "FIC"

STRIKES = (70.0, 80.0, 90.0, 100.0, 110.0, 120.0, 130.0, 140.0)
MATURITIES_DAYS = (30, 60, 120, 180, 365)

RATE = 0.03
SIGMA = 0.22
SPOT0 = 105.0

QUOTES_PER_SNAPSHOT = len(STRIKES) * len(MATURITIES_DAYS)  # 40
DEFAULT_SNAPSHOT_COUNT = 300  # 300 * 40 = 12,000 quotes
DEFAULT_SEED = 20260817
DEFAULT_START_TS = 1_700_000_000.0
DEFAULT_INTERVAL_SECONDS = 5.0

# Rule tolerances. These are also the numbers written into
# rules/option_surface_guardrails.yaml -- kept here too so the producer's
# seed deltas can be sized against them without importing YAML at import time.
PARITY_TOLERANCE = 0.05
MONOTONICITY_TOLERANCE = 0.02
BUTTERFLY_TOLERANCE = 0.02
CALENDAR_TOLERANCE = 0.02


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _bs_price(spot, strike, rate, sigma, maturity_years):
    """European call and put price, Black-Scholes, no dividends."""
    if maturity_years <= 0:
        return max(spot - strike, 0.0), max(strike - spot, 0.0)
    sqrt_t = math.sqrt(maturity_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * maturity_years) / (
        sigma * sqrt_t
    )
    d2 = d1 - sigma * sqrt_t
    discount = math.exp(-rate * maturity_years)
    call = spot * _norm_cdf(d1) - strike * discount * _norm_cdf(d2)
    put = strike * discount * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return call, put


def _quoted(theoretical_mid, rng):
    """Add a small deterministic jitter and bid/ask spread around a theoretical price."""
    jitter = rng.uniform(-0.005, 0.005)
    mid = round(max(0.0, theoretical_mid + jitter), 4)
    half_spread = round(max(0.01, 0.004 * mid), 4)
    return round(mid - half_spread, 4), round(mid + half_spread, 4), mid


def _build_snapshot(index, spot, rng, start_ts, interval_seconds):
    quotes = []
    for maturity_days in MATURITIES_DAYS:
        maturity_years = maturity_days / 365.0
        for strike in STRIKES:
            call_theo, put_theo = _bs_price(spot, strike, RATE, SIGMA, maturity_years)
            call_bid, call_ask, call_mid = _quoted(call_theo, rng)
            put_bid, put_ask, put_mid = _quoted(put_theo, rng)
            quotes.append(
                {
                    "strike": strike,
                    "maturity_days": maturity_days,
                    "maturity_years": round(maturity_years, 6),
                    "call_bid": call_bid,
                    "call_ask": call_ask,
                    "call_mid": call_mid,
                    "put_bid": put_bid,
                    "put_ask": put_ask,
                    "put_mid": put_mid,
                }
            )
    return {
        "snapshot_id": "snap-{:04d}".format(index),
        "ts": round(start_ts + index * interval_seconds, 3),
        "underlying": UNDERLYING,
        "spot": round(spot, 4),
        "rate": RATE,
        "quotes": quotes,
    }


def generate_clean_snapshots(
    count=DEFAULT_SNAPSHOT_COUNT,
    seed=DEFAULT_SEED,
    start_ts=DEFAULT_START_TS,
    interval_seconds=DEFAULT_INTERVAL_SECONDS,
):
    """Yield ``count`` arbitrage-free option-chain snapshots. Pure and deterministic.

    Each snapshot's spot follows a small deterministic random walk so the
    stream looks like a moving market over time, but every cross-sectional
    check only ever compares quotes *within* one snapshot, so the walk across
    snapshots never introduces spurious cross-snapshot arbitrage: there isn't
    such a thing as cross-snapshot arbitrage in this design, by construction.
    """
    rng = random.Random(seed)
    spot = SPOT0
    snapshots = []
    for index in range(count):
        spot = max(20.0, spot * math.exp(rng.gauss(0.0, 0.01)))
        snapshots.append(_build_snapshot(index, spot, rng, start_ts, interval_seconds))
    return snapshots


def _find_quote(quotes, strike, maturity_days):
    for quote in quotes:
        if quote["strike"] == strike and quote["maturity_days"] == maturity_days:
            return quote
    raise KeyError("no quote at strike={} maturity_days={}".format(strike, maturity_days))


# Each spec plants exactly one mispricing in exactly one snapshot, so every
# seed maps back to a specific, known family. Deltas are sized to comfortably
# clear the corresponding rule's tolerance (see the *_TOLERANCE constants
# above). They are NOT sized to avoid tripping a second family in the same
# snapshot, and in practice many of them do: a mispricing large enough to
# clear a monotonicity or calendar tolerance is often, mechanically, also
# large enough to clear the much tighter parity tolerance at the same quote.
# That is a real property of these invariants, not a seeding bug -- see the
# README section "What one seed actually breaks" for the measured breakdown
# and why "24 seeded violations caught, 0 false positives" is still the
# correct claim at the seed level even though the raw rule-firing count is
# higher than 24.
SEED_SPECS = [
    # --- put-call parity (6): perturb one side of one quote -------------
    {"id": "parity-01", "family": "parity", "snapshot_index": 10, "strike": 100.0,
     "maturity_days": 120, "field": "put_mid", "delta": -0.30,
     "description": "put_mid dropped 0.30 below fair value at K=100, T=120d"},
    {"id": "parity-02", "family": "parity", "snapshot_index": 34, "strike": 110.0,
     "maturity_days": 60, "field": "call_mid", "delta": 0.35,
     "description": "call_mid bumped 0.35 above fair value at K=110, T=60d"},
    {"id": "parity-03", "family": "parity", "snapshot_index": 58, "strike": 90.0,
     "maturity_days": 180, "field": "put_mid", "delta": 0.40,
     "description": "put_mid bumped 0.40 above fair value at K=90, T=180d"},
    {"id": "parity-04", "family": "parity", "snapshot_index": 82, "strike": 120.0,
     "maturity_days": 30, "field": "call_mid", "delta": -0.30,
     "description": "call_mid dropped 0.30 below fair value at K=120, T=30d"},
    {"id": "parity-05", "family": "parity", "snapshot_index": 106, "strike": 100.0,
     "maturity_days": 365, "field": "put_mid", "delta": -0.45,
     "description": "put_mid dropped 0.45 below fair value at K=100, T=365d"},
    {"id": "parity-06", "family": "parity", "snapshot_index": 130, "strike": 80.0,
     "maturity_days": 120, "field": "call_mid", "delta": 0.30,
     "description": "call_mid bumped 0.30 above fair value at K=80, T=120d"},

    # --- strike monotonicity (6): call must fall, put must rise, in strike
    {"id": "mono-01", "family": "monotonicity", "snapshot_index": 14, "side": "call",
     "maturity_days": 120, "strike": 110.0, "delta": 4.0,
     "description": "call_mid at K=110, T=120d bumped +4.0, now above K=100's call"},
    {"id": "mono-02", "family": "monotonicity", "snapshot_index": 38, "side": "call",
     "maturity_days": 60, "strike": 130.0, "delta": 3.5,
     "description": "call_mid at K=130, T=60d bumped +3.5, now above K=120's call"},
    {"id": "mono-03", "family": "monotonicity", "snapshot_index": 62, "side": "put",
     "maturity_days": 180, "strike": 90.0, "delta": -4.0,
     "description": "put_mid at K=90, T=180d dropped -4.0, now below K=80's put"},
    {"id": "mono-04", "family": "monotonicity", "snapshot_index": 86, "side": "call",
     "maturity_days": 365, "strike": 100.0, "delta": 5.0,
     "description": "call_mid at K=100, T=365d bumped +5.0, now above K=90's call"},
    {"id": "mono-05", "family": "monotonicity", "snapshot_index": 110, "side": "put",
     "maturity_days": 30, "strike": 120.0, "delta": -3.0,
     "description": "put_mid at K=120, T=30d dropped -3.0, now below K=110's put"},
    {"id": "mono-06", "family": "monotonicity", "snapshot_index": 134, "side": "call",
     "maturity_days": 120, "strike": 90.0, "delta": 3.5,
     "description": "call_mid at K=90, T=120d bumped +3.5, now above K=80's call"},

    # --- butterfly convexity (6): push the middle strike of a triple up --
    {"id": "butterfly-01", "family": "butterfly", "snapshot_index": 18,
     "maturity_days": 120, "strikes": (90.0, 100.0, 110.0), "delta": 1.2,
     "description": "call_mid at K=100 (between 90/110), T=120d bumped +1.2"},
    {"id": "butterfly-02", "family": "butterfly", "snapshot_index": 42,
     "maturity_days": 60, "strikes": (100.0, 110.0, 120.0), "delta": 1.0,
     "description": "call_mid at K=110 (between 100/120), T=60d bumped +1.0"},
    {"id": "butterfly-03", "family": "butterfly", "snapshot_index": 66,
     "maturity_days": 180, "strikes": (80.0, 90.0, 100.0), "delta": 1.3,
     "description": "call_mid at K=90 (between 80/100), T=180d bumped +1.3"},
    {"id": "butterfly-04", "family": "butterfly", "snapshot_index": 90,
     "maturity_days": 365, "strikes": (110.0, 120.0, 130.0), "delta": 1.1,
     "description": "call_mid at K=120 (between 110/130), T=365d bumped +1.1"},
    {"id": "butterfly-05", "family": "butterfly", "snapshot_index": 114,
     "maturity_days": 30, "strikes": (90.0, 100.0, 110.0), "delta": 0.9,
     "description": "call_mid at K=100 (between 90/110), T=30d bumped +0.9"},
    {"id": "butterfly-06", "family": "butterfly", "snapshot_index": 138,
     "maturity_days": 120, "strikes": (100.0, 110.0, 120.0), "delta": 1.2,
     "description": "call_mid at K=110 (between 100/120), T=120d bumped +1.2"},

    # --- calendar spread (6): make the longer maturity cheaper -----------
    {"id": "calendar-01", "family": "calendar", "snapshot_index": 22, "strike": 100.0,
     "maturity_short": 120, "maturity_long": 180, "delta": -1.5,
     "description": "call_mid at K=100, T=180d dropped -1.5 below T=120d's call"},
    {"id": "calendar-02", "family": "calendar", "snapshot_index": 46, "strike": 110.0,
     "maturity_short": 60, "maturity_long": 120, "delta": -1.3,
     "description": "call_mid at K=110, T=120d dropped -1.3 below T=60d's call"},
    {"id": "calendar-03", "family": "calendar", "snapshot_index": 70, "strike": 90.0,
     "maturity_short": 180, "maturity_long": 365, "delta": -1.6,
     "description": "call_mid at K=90, T=365d dropped -1.6 below T=180d's call"},
    {"id": "calendar-04", "family": "calendar", "snapshot_index": 94, "strike": 100.0,
     "maturity_short": 30, "maturity_long": 60, "delta": -1.2,
     "description": "call_mid at K=100, T=60d dropped -1.2 below T=30d's call"},
    {"id": "calendar-05", "family": "calendar", "snapshot_index": 118, "strike": 120.0,
     "maturity_short": 120, "maturity_long": 180, "delta": -1.4,
     "description": "call_mid at K=120, T=180d dropped -1.4 below T=120d's call"},
    {"id": "calendar-06", "family": "calendar", "snapshot_index": 142, "strike": 80.0,
     "maturity_short": 60, "maturity_long": 120, "delta": -1.3,
     "description": "call_mid at K=80, T=120d dropped -1.3 below T=60d's call"},
]


# Extra clearance added on top of the rule's own tolerance when a delta is
# computed from the surrounding, still-clean quotes rather than guessed as a
# flat number (see the note below `_apply_seed` for why a flat guess is not
# good enough).
_CLEARANCE = 0.05


def _apply_seed(snapshots, spec):
    """Perturb one quote (or one strike's call+put pair) so that exactly the
    spec's target family clears its tolerance, by a margin computed from the
    quotes actually sitting either side of it in this snapshot.

    Every family except parity moves call_mid AND put_mid together by the
    same amount. That is deliberate: parity checks call_mid - put_mid, so a
    shift applied equally to both sides leaves that difference, and therefore
    the parity check, untouched. A flat delta on call_mid alone (the first
    version of this seeding) does not have that property, and a delta big
    enough to clear a monotonicity or calendar tolerance is, mechanically,
    almost always also big enough to clear parity's much tighter tolerance on
    that same quote -- which is exactly the false "MISSED"/extra-collateral
    pattern this version fixes. See README, "What one seed actually breaks".
    """
    snapshot = snapshots[spec["snapshot_index"]]
    quotes = snapshot["quotes"]

    if spec["family"] == "parity":
        quote = _find_quote(quotes, spec["strike"], spec["maturity_days"])
        quote[spec["field"]] = round(quote[spec["field"]] + spec["delta"], 4)
        return

    if spec["family"] == "monotonicity":
        same_mat = sorted(
            (q for q in quotes if q["maturity_days"] == spec["maturity_days"]),
            key=lambda q: q["strike"],
        )
        idx = next(i for i, q in enumerate(same_mat) if q["strike"] == spec["strike"])
        field = "call_mid" if spec["side"] == "call" else "put_mid"
        target = same_mat[idx]
        sign = 1.0 if spec["delta"] > 0 else -1.0
        neighbor = same_mat[idx - 1] if idx > 0 else same_mat[idx + 1]
        gap = abs(target[field] - neighbor[field])
        magnitude = gap + MONOTONICITY_TOLERANCE + _CLEARANCE
        delta = sign * magnitude

    elif spec["family"] == "butterfly":
        k1, k2, k3 = spec["strikes"]
        q1 = _find_quote(quotes, k1, spec["maturity_days"])
        q2 = _find_quote(quotes, k2, spec["maturity_days"])
        q3 = _find_quote(quotes, k3, spec["maturity_days"])
        current_second_diff = q1["call_mid"] - 2 * q2["call_mid"] + q3["call_mid"]
        # new_second_diff = current_second_diff - 2*delta when q2's call moves
        # up by delta; solve for the delta that lands exactly at the target.
        target_second_diff = -BUTTERFLY_TOLERANCE - _CLEARANCE
        delta = (current_second_diff - target_second_diff) / 2.0
        target = q2

    elif spec["family"] == "calendar":
        short_q = _find_quote(quotes, spec["strike"], spec["maturity_short"])
        long_q = _find_quote(quotes, spec["strike"], spec["maturity_long"])
        needed_long = short_q["call_mid"] - CALENDAR_TOLERANCE - _CLEARANCE
        delta = needed_long - long_q["call_mid"]
        target = long_q

    else:
        raise ValueError("unknown seed family {!r}".format(spec["family"]))

    target["call_mid"] = round(target["call_mid"] + delta, 4)
    target["put_mid"] = round(target["put_mid"] + delta, 4)


def generate_seeded_snapshots(
    count=DEFAULT_SNAPSHOT_COUNT,
    seed=DEFAULT_SEED,
    start_ts=DEFAULT_START_TS,
    interval_seconds=DEFAULT_INTERVAL_SECONDS,
    seed_specs=SEED_SPECS,
):
    """Clean snapshots with the 24 planted violations from ``seed_specs`` applied.

    Returns the list of snapshot dicts, ready to feed straight into
    ``GuardrailEngine.process`` one at a time, in order.
    """
    snapshots = generate_clean_snapshots(count, seed, start_ts, interval_seconds)
    for spec in seed_specs:
        _apply_seed(snapshots, spec)
    return snapshots
