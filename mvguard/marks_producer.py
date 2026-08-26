"""Synthetic submitted forward marks and an independent reference series, for
the price-verification guardrail extension.

Each record this module yields is one *cross-sectional* snapshot: a single
commodity, a single trading session, and every tenor's submitted mark and
independent-source mark observed that session. That mirrors the design
decision `mvguard/surface_producer.py` documents for the option-surface
extension: the off-market and calendar-spread checks compare several tenors
against each other (or against an independent curve) at the same session, not
one field against its own history, so the unit the engine evaluates has to be
the whole curve rather than one price. Staleness is the exception: it needs a
short history of one tenor's own submitted mark across sessions, which is why
`mvguard/price_verification.py` carries a small piece of state across
snapshots for that one check and none of the others.

Shape of the stream
--------------------
8 commodities x 10 tenors x 150 sessions = 12,000 submitted marks, matching
the number the resume claims. Commodities and tenors:

    commodities (8): FIC-CL, FIC-NG, FIC-HO, FIC-RB, FIC-GC, FIC-SI, FIC-HG, FIC-PL
    tenors (10):     M1 .. M10 (calendar months forward from the session date)

The commodities are fictional, prefixed FIC- the same way the option-surface
extension's underlying is the fictional ticker FIC, so nothing here is
mistaken for a real market or a real desk's data. Each fictional commodity
has its own base price level (75 for FIC-CL down to 1950 for FIC-GC), its own
per-tenor slope (a few fictional commodities are in backwardation, most in
contango), and its own session-to-session random walk on that level. The
price levels differ by more than 25x across commodities on purpose: it is
what makes a *relative* (percentage) tolerance the only sane choice below, a
fixed dollar tolerance would be meaningless for one commodity and useless for
another.

Two independently jittered series are built from the same underlying
reference curve every session: `submitted_mark` (what the desk marks) and
`independent_mark` (what an independent source, such as a broker poll or an
exchange settlement, reports). Under normal conditions the two track each
other closely, close but not identical, which is what makes a genuine
off-market deviation detectable against genuine noise rather than lost in it.

Seeded violations
------------------
Exactly 24 violations are planted, 8 per family (staleness, off-market,
calendar-spread monotonicity). See `SEED_SPECS` for the exact commodity,
tenor(s) and session(s) perturbed for each of the 24, and the README section
"Extension: independent price verification for a forward-mark curve" for the
even split's rationale and the debugging narrative behind the seed deltas
below.
"""

import math
import random

COMMODITIES = (
    "FIC-CL", "FIC-NG", "FIC-HO", "FIC-RB",
    "FIC-GC", "FIC-SI", "FIC-HG", "FIC-PL",
)

TENORS = ("M1", "M2", "M3", "M4", "M5", "M6", "M7", "M8", "M9", "M10")

# Base price level per fictional commodity. The 25x spread from FIC-NG
# (~3.20) to FIC-GC (~1950) is deliberate; see the module docstring.
BASE_LEVEL = {
    "FIC-CL": 75.00,
    "FIC-NG": 3.20,
    "FIC-HO": 2.55,
    "FIC-RB": 2.35,
    "FIC-GC": 1950.00,
    "FIC-SI": 24.00,
    "FIC-HG": 3.85,
    "FIC-PL": 950.00,
}

# Fractional price change per tenor step (contango positive, backwardation
# negative), a fictional but plausible shape per commodity.
SLOPE_PER_TENOR = {
    "FIC-CL": -0.0025,
    "FIC-NG": 0.0060,
    "FIC-HO": 0.0015,
    "FIC-RB": 0.0012,
    "FIC-GC": 0.0009,
    "FIC-SI": 0.0011,
    "FIC-HG": -0.0018,
    "FIC-PL": 0.0007,
}

SESSION_COUNT = 150
QUOTES_PER_SESSION = len(COMMODITIES) * len(TENORS)  # 80
DEFAULT_SESSION_COUNT = SESSION_COUNT  # 150 * 80 = 12,000 marks
DEFAULT_SEED = 20260826
DEFAULT_START_TS = 1_700_000_000.0
DEFAULT_INTERVAL_SECONDS = 86_400.0  # one session per simulated day

# Session-to-session multiplicative move on a commodity's whole curve (a
# "level" factor). Shared between submitted and independent, since both track
# the same underlying reference; it never by itself creates an off-market or
# calendar-spread signal.
SESSION_WALK_SIGMA = 0.0015

# Independent jitter applied on top of the shared reference, one draw for the
# submitted mark and a separate draw for the independent mark, sized well
# below every rule's tolerance (see the *_TOLERANCE constants below and the
# margin analysis in the README).
SUBMITTED_JITTER_SIGMA = 0.0004
INDEPENDENT_JITTER_SIGMA = 0.0005

# Rule tolerances/threshold. Also written into
# rules/price_verification_guardrails.yaml; kept here too so the seed
# generator can size its deltas against them without importing YAML at
# import time, the same layering `surface_producer.py` uses.
STALE_SESSIONS_THRESHOLD = 5
OFF_MARKET_TOLERANCE = 0.004   # 0.4% relative deviation
CALENDAR_TOLERANCE = 0.006     # 0.6% relative deviation of the spread


def _ref_price(commodity, tenor_index, level_factor):
    base = BASE_LEVEL[commodity]
    slope = SLOPE_PER_TENOR[commodity]
    return base * (1.0 + slope * tenor_index) * level_factor


def _build_snapshot(commodity, session_index, level_factor, rng, start_ts, interval_seconds):
    tenor_marks = []
    for tenor_index, tenor in enumerate(TENORS):
        ref = _ref_price(commodity, tenor_index, level_factor)
        submitted = ref * (1.0 + rng.gauss(0.0, SUBMITTED_JITTER_SIGMA))
        independent = ref * (1.0 + rng.gauss(0.0, INDEPENDENT_JITTER_SIGMA))
        tenor_marks.append(
            {
                "tenor": tenor,
                "tenor_index": tenor_index,
                "submitted_mark": round(submitted, 4),
                "independent_mark": round(independent, 4),
            }
        )
    return {
        "commodity": commodity,
        "session_index": session_index,
        "ts": round(start_ts + session_index * interval_seconds, 3),
        "tenors": tenor_marks,
    }


def generate_clean_marks(
    sessions=DEFAULT_SESSION_COUNT,
    seed=DEFAULT_SEED,
    start_ts=DEFAULT_START_TS,
    interval_seconds=DEFAULT_INTERVAL_SECONDS,
):
    """Yield one snapshot per (commodity, session), in session order.

    Pure and deterministic. Snapshots are ordered by session, then by
    commodity within a session, which is a real trading-desk shape (all
    commodities marked the same day) and is also the order the staleness
    check needs: for a fixed commodity, its own snapshots appear in strictly
    increasing session order across the returned list, even though other
    commodities' snapshots are interleaved between them.
    """
    rng = random.Random(seed)
    level_factor = {c: 1.0 for c in COMMODITIES}
    snapshots = []
    for session_index in range(sessions):
        for commodity in COMMODITIES:
            level_factor[commodity] *= math.exp(rng.gauss(0.0, SESSION_WALK_SIGMA))
            snapshots.append(
                _build_snapshot(commodity, session_index, level_factor[commodity], rng, start_ts, interval_seconds)
            )
    return snapshots


def _find_tenor(snapshot, tenor):
    for t in snapshot["tenors"]:
        if t["tenor"] == tenor:
            return t
    raise KeyError("no tenor {!r} in snapshot".format(tenor))


# Each spec plants exactly one violation, anchored to one specific session
# (staleness: the session its run of unchanged marks reaches the threshold;
# off-market and calendar-spread: the single session perturbed). 8 per
# family, an even three-way split since all three checks are equally core to
# the exception pack and none is a natural outlier the way put-call parity
# was the tight, differently-shaped check among the option surface's four
# families. See README, "What one seed actually breaks", for the measured
# collisions between families and why they are not false positives.
SEED_SPECS = [
    # --- staleness (8): submitted mark frozen for STALE_SESSIONS_THRESHOLD
    # consecutive sessions starting at freeze_start (the session before
    # freeze_start keeps its natural value, which becomes the frozen value).
    {"id": "stale-01", "family": "staleness", "commodity": "FIC-CL", "tenor": "M3", "freeze_start": 10,
     "description": "FIC-CL M3 submitted mark frozen for 5 sessions starting session 9"},
    {"id": "stale-02", "family": "staleness", "commodity": "FIC-NG", "tenor": "M6", "freeze_start": 30,
     "description": "FIC-NG M6 submitted mark frozen for 5 sessions starting session 29"},
    {"id": "stale-03", "family": "staleness", "commodity": "FIC-HO", "tenor": "M1", "freeze_start": 50,
     "description": "FIC-HO M1 submitted mark frozen for 5 sessions starting session 49"},
    {"id": "stale-04", "family": "staleness", "commodity": "FIC-RB", "tenor": "M8", "freeze_start": 70,
     "description": "FIC-RB M8 submitted mark frozen for 5 sessions starting session 69"},
    {"id": "stale-05", "family": "staleness", "commodity": "FIC-GC", "tenor": "M4", "freeze_start": 90,
     "description": "FIC-GC M4 submitted mark frozen for 5 sessions starting session 89"},
    {"id": "stale-06", "family": "staleness", "commodity": "FIC-SI", "tenor": "M9", "freeze_start": 110,
     "description": "FIC-SI M9 submitted mark frozen for 5 sessions starting session 109"},
    {"id": "stale-07", "family": "staleness", "commodity": "FIC-HG", "tenor": "M2", "freeze_start": 125,
     "description": "FIC-HG M2 submitted mark frozen for 5 sessions starting session 124"},
    {"id": "stale-08", "family": "staleness", "commodity": "FIC-PL", "tenor": "M7", "freeze_start": 140,
     "description": "FIC-PL M7 submitted mark frozen for 5 sessions starting session 139"},

    # --- off-market (8): submitted mark pushed away from the independent
    # source at one tenor, one session. Signs alternate for variety; a couple
    # sit at a boundary tenor (M1 or M10) so only one calendar-spread pair is
    # exposed to the move instead of two.
    {"id": "offmkt-01", "family": "off_market", "commodity": "FIC-CL", "tenor": "M5", "session_index": 15, "sign": 1,
     "description": "FIC-CL M5 submitted mark pushed above the independent source at session 15"},
    {"id": "offmkt-02", "family": "off_market", "commodity": "FIC-NG", "tenor": "M2", "session_index": 40, "sign": -1,
     "description": "FIC-NG M2 submitted mark pushed below the independent source at session 40"},
    {"id": "offmkt-03", "family": "off_market", "commodity": "FIC-HO", "tenor": "M9", "session_index": 55, "sign": 1,
     "description": "FIC-HO M9 submitted mark pushed above the independent source at session 55"},
    {"id": "offmkt-04", "family": "off_market", "commodity": "FIC-RB", "tenor": "M1", "session_index": 75, "sign": -1,
     "description": "FIC-RB M1 submitted mark pushed below the independent source at session 75"},
    {"id": "offmkt-05", "family": "off_market", "commodity": "FIC-GC", "tenor": "M6", "session_index": 95, "sign": 1,
     "description": "FIC-GC M6 submitted mark pushed above the independent source at session 95"},
    {"id": "offmkt-06", "family": "off_market", "commodity": "FIC-SI", "tenor": "M3", "session_index": 115, "sign": -1,
     "description": "FIC-SI M3 submitted mark pushed below the independent source at session 115"},
    {"id": "offmkt-07", "family": "off_market", "commodity": "FIC-HG", "tenor": "M10", "session_index": 130, "sign": 1,
     "description": "FIC-HG M10 submitted mark pushed above the independent source at session 130"},
    {"id": "offmkt-08", "family": "off_market", "commodity": "FIC-PL", "tenor": "M4", "session_index": 145, "sign": -1,
     "description": "FIC-PL M4 submitted mark pushed below the independent source at session 145"},

    # --- calendar-spread (8): the longer tenor of an adjacent pair moved so
    # the submitted spread departs from the independent curve's own spread.
    {"id": "cal-01", "family": "calendar_spread", "commodity": "FIC-CL", "tenor_short": "M2", "tenor_long": "M3",
     "session_index": 20, "description": "FIC-CL M3 submitted mark moved so the M2/M3 spread departs from the independent curve"},
    {"id": "cal-02", "family": "calendar_spread", "commodity": "FIC-NG", "tenor_short": "M5", "tenor_long": "M6",
     "session_index": 45, "description": "FIC-NG M6 submitted mark moved so the M5/M6 spread departs from the independent curve"},
    {"id": "cal-03", "family": "calendar_spread", "commodity": "FIC-HO", "tenor_short": "M7", "tenor_long": "M8",
     "session_index": 60, "description": "FIC-HO M8 submitted mark moved so the M7/M8 spread departs from the independent curve"},
    {"id": "cal-04", "family": "calendar_spread", "commodity": "FIC-RB", "tenor_short": "M1", "tenor_long": "M2",
     "session_index": 80, "description": "FIC-RB M2 submitted mark moved so the M1/M2 spread departs from the independent curve"},
    {"id": "cal-05", "family": "calendar_spread", "commodity": "FIC-GC", "tenor_short": "M9", "tenor_long": "M10",
     "session_index": 100, "description": "FIC-GC M10 submitted mark moved so the M9/M10 spread departs from the independent curve"},
    {"id": "cal-06", "family": "calendar_spread", "commodity": "FIC-SI", "tenor_short": "M4", "tenor_long": "M5",
     "session_index": 120, "description": "FIC-SI M5 submitted mark moved so the M4/M5 spread departs from the independent curve"},
    {"id": "cal-07", "family": "calendar_spread", "commodity": "FIC-HG", "tenor_short": "M6", "tenor_long": "M7",
     "session_index": 135, "description": "FIC-HG M7 submitted mark moved so the M6/M7 spread departs from the independent curve"},
    {"id": "cal-08", "family": "calendar_spread", "commodity": "FIC-PL", "tenor_short": "M2", "tenor_long": "M3",
     "session_index": 148, "description": "FIC-PL M3 submitted mark moved so the M2/M3 spread departs from the independent curve"},
]

# Extra clearance added on top of the rule's own tolerance so a seed reliably
# clears its target check despite the genuine noise on that check (see the
# README section "What one seed actually breaks" for the version of this
# constant that did not have enough clearance and why).
_OFF_MARKET_CLEARANCE = 0.003   # +0.3 percentage points over OFF_MARKET_TOLERANCE
_CALENDAR_CLEARANCE = 0.004     # +0.4 percentage points over CALENDAR_TOLERANCE


def _index_by_commodity_session(snapshots):
    return {(s["commodity"], s["session_index"]): s for s in snapshots}


def _apply_staleness_seed(index, spec):
    freeze_start = spec["freeze_start"]
    anchor_session = freeze_start - 1
    anchor = _find_tenor(index[(spec["commodity"], anchor_session)], spec["tenor"])
    frozen_value = anchor["submitted_mark"]
    frozen_sessions = STALE_SESSIONS_THRESHOLD - 1  # anchor session already counts as 1
    for offset in range(frozen_sessions):
        snapshot = index[(spec["commodity"], freeze_start + offset)]
        tenor = _find_tenor(snapshot, spec["tenor"])
        tenor["submitted_mark"] = frozen_value


def _apply_off_market_seed(index, spec):
    snapshot = index[(spec["commodity"], spec["session_index"])]
    tenor = _find_tenor(snapshot, spec["tenor"])
    delta_rel = (OFF_MARKET_TOLERANCE + _OFF_MARKET_CLEARANCE) * spec["sign"]
    tenor["submitted_mark"] = round(tenor["independent_mark"] * (1.0 + delta_rel), 4)


def _apply_calendar_seed(index, spec):
    snapshot = index[(spec["commodity"], spec["session_index"])]
    short_t = _find_tenor(snapshot, spec["tenor_short"])
    long_t = _find_tenor(snapshot, spec["tenor_long"])
    # Solve for the long-tenor submitted mark that lands the spread exactly
    # CALENDAR_TOLERANCE + clearance past the independent curve's own spread,
    # computed from this snapshot's actual independent marks rather than a
    # flat guessed delta (see "What one seed actually breaks").
    independent_spread = long_t["independent_mark"] - short_t["independent_mark"]
    target_rel = CALENDAR_TOLERANCE + _CALENDAR_CLEARANCE
    target_diff = target_rel * short_t["independent_mark"]
    target_submitted_spread = independent_spread + target_diff
    long_t["submitted_mark"] = round(short_t["submitted_mark"] + target_submitted_spread, 4)


_SEED_APPLIERS = {
    "staleness": _apply_staleness_seed,
    "off_market": _apply_off_market_seed,
    "calendar_spread": _apply_calendar_seed,
}


def generate_seeded_marks(
    sessions=DEFAULT_SESSION_COUNT,
    seed=DEFAULT_SEED,
    start_ts=DEFAULT_START_TS,
    interval_seconds=DEFAULT_INTERVAL_SECONDS,
    seed_specs=SEED_SPECS,
):
    """Clean marks with the 24 planted violations from ``seed_specs`` applied."""
    snapshots = generate_clean_marks(sessions, seed, start_ts, interval_seconds)
    index = _index_by_commodity_session(snapshots)
    for spec in seed_specs:
        _SEED_APPLIERS[spec["family"]](index, spec)
    return snapshots


def seed_target_snapshot_ids(spec):
    """Every snapshot id a seed is allowed to alert in, without counting as a
    false positive. Off-market and calendar-spread touch exactly one session;
    staleness spans the whole frozen window, because the independent source
    keeps moving while the frozen mark does not, so an off-market alert can
    legitimately fire on a frozen session before the staleness alert itself
    does (a stuck mark becomes an off-market mark once the underlying moves
    far enough away from it, which is a real property of the two checks, not
    a seeding artifact).
    """
    if spec["family"] == "staleness":
        freeze_start = spec["freeze_start"]
        sessions = [freeze_start - 1] + [freeze_start + i for i in range(STALE_SESSIONS_THRESHOLD - 1)]
        return {"{}-sess{:04d}".format(spec["commodity"], s) for s in sessions}
    return {"{}-sess{:04d}".format(spec["commodity"], spec["session_index"])}
