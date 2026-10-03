"""A documented Python mirror of `vba/MonthEndReview.bas`.

This machine has no Excel installed (confirmed: no registered Excel.Application
COM class), so the VBA workbook's cell-population algorithm cannot be executed
live. `vba/MonthEndReview.bas` is the real production design: a worksheet
macro a non-engineer would click to refresh the month-end exception pack. This
module is an independently written Python transliteration of exactly that
macro's logic and row/column shape (flat worksheet rows grouped by composite
keys, not the nested snapshot/quotes structure `mvguard/surface_guardrails.py`
uses), so the claim "the workbook recomputes every check in-sheet" can be
measured without Excel, the same way `equilibrium-catalyst-report-addin`
measured a real-but-unexecuted VBA add-in with a Python mirror.

The reconciliation this buys is real: `mvguard/surface_guardrails.py` (the
Python engine, already covered by `tests/test_option_surface.py`) and this
mirror are two independently coded implementations of the same four
no-arbitrage identities. `scripts/build_month_end_workbook.py` runs both over
the same 12,000-quote seeded surface and diffs them cell for cell; agreement
is evidence the VBA design is correct, not just that this file imports the
engine it is supposed to check.

Deliberate difference from the engine: this module groups a flat list of
worksheet-shaped rows by composite key strings (`"snap-0001|120"`), the way
a VBA macro groups `Range` rows with a `Scripting.Dictionary`, and sorts each
small group with an explicit insertion sort rather than Python's `sorted`,
because VBA has no built-in stable sort over a `Collection` either. Using
`sorted()` here would make this file an easier port of the engine, not an
honest mirror of what the worksheet macro actually does.
"""

import math

PARITY_TOLERANCE = 0.05
MONOTONICITY_TOLERANCE = 0.02
BUTTERFLY_TOLERANCE = 0.02
CALENDAR_TOLERANCE = 0.02


def flatten_snapshots(snapshots):
    """One row per quote, the shape the "Quotes" worksheet holds."""
    rows = []
    for snapshot in snapshots:
        for q in snapshot["quotes"]:
            rows.append(
                {
                    "snapshot_id": snapshot["snapshot_id"],
                    "spot": snapshot["spot"],
                    "rate": snapshot["rate"],
                    "strike": q["strike"],
                    "maturity_days": q["maturity_days"],
                    "maturity_years": q["maturity_years"],
                    "call_mid": q["call_mid"],
                    "put_mid": q["put_mid"],
                }
            )
    return rows


def _insertion_sort_by(rows, key):
    """Stand-in for the VBA macro's bubble/insertion sort over a Collection.

    Small groups only (8 strikes or 5 maturities per snapshot), so an O(n^2)
    sort is what the macro actually uses and is not a performance concern.
    """
    out = list(rows)
    for i in range(1, len(out)):
        current = out[i]
        j = i - 1
        while j >= 0 and key(out[j]) > key(current):
            out[j + 1] = out[j]
            j -= 1
        out[j + 1] = current
    return out


def _group_by(rows, key_fn):
    groups = {}
    for row in rows:
        groups.setdefault(key_fn(row), []).append(row)
    return groups


def evaluate_rows(rows):
    """Worksheet-shaped equivalent of `mvguard.surface_guardrails.evaluate_stream`.

    Returns a flat list of violation dicts: {snapshot_id, family, detail}.
    """
    violations = []

    for row in rows:
        expected = row["spot"] - row["strike"] * math.exp(-row["rate"] * row["maturity_years"])
        actual = row["call_mid"] - row["put_mid"]
        diff = actual - expected
        if abs(diff) > PARITY_TOLERANCE:
            violations.append(
                {
                    "snapshot_id": row["snapshot_id"],
                    "family": "parity",
                    "detail": {
                        "strike": row["strike"],
                        "maturity_days": row["maturity_days"],
                        "diff": diff,
                        "tolerance": PARITY_TOLERANCE,
                    },
                }
            )

    by_maturity_group = _group_by(rows, lambda r: (r["snapshot_id"], r["maturity_days"]))
    for (snapshot_id, maturity_days), group in by_maturity_group.items():
        group = _insertion_sort_by(group, key=lambda r: r["strike"])

        for i in range(1, len(group)):
            lo, hi = group[i - 1], group[i]
            if hi["call_mid"] > lo["call_mid"] + MONOTONICITY_TOLERANCE:
                violations.append(
                    {
                        "snapshot_id": snapshot_id,
                        "family": "monotonicity",
                        "detail": {
                            "side": "call",
                            "maturity_days": maturity_days,
                            "strike_lo": lo["strike"],
                            "strike_hi": hi["strike"],
                        },
                    }
                )
            if hi["put_mid"] < lo["put_mid"] - MONOTONICITY_TOLERANCE:
                violations.append(
                    {
                        "snapshot_id": snapshot_id,
                        "family": "monotonicity",
                        "detail": {
                            "side": "put",
                            "maturity_days": maturity_days,
                            "strike_lo": lo["strike"],
                            "strike_hi": hi["strike"],
                        },
                    }
                )

        for i in range(1, len(group) - 1):
            k1, k2, k3 = group[i - 1], group[i], group[i + 1]
            if (k2["strike"] - k1["strike"]) != (k3["strike"] - k2["strike"]):
                continue
            second_diff = k1["call_mid"] - 2 * k2["call_mid"] + k3["call_mid"]
            if second_diff < -BUTTERFLY_TOLERANCE:
                violations.append(
                    {
                        "snapshot_id": snapshot_id,
                        "family": "butterfly",
                        "detail": {
                            "maturity_days": maturity_days,
                            "strikes": (k1["strike"], k2["strike"], k3["strike"]),
                            "second_difference": second_diff,
                        },
                    }
                )

    by_strike_group = _group_by(rows, lambda r: (r["snapshot_id"], r["strike"]))
    for (snapshot_id, strike), group in by_strike_group.items():
        group = _insertion_sort_by(group, key=lambda r: r["maturity_days"])
        for i in range(1, len(group)):
            short, long_ = group[i - 1], group[i]
            if long_["call_mid"] < short["call_mid"] - CALENDAR_TOLERANCE:
                violations.append(
                    {
                        "snapshot_id": snapshot_id,
                        "family": "calendar",
                        "detail": {
                            "strike": strike,
                            "maturity_short": short["maturity_days"],
                            "maturity_long": long_["maturity_days"],
                        },
                    }
                )

    return violations


def summarize_by_snapshot_family(violations, snapshot_ids, families):
    """Per-(snapshot, family) violation count, the shape the "VBASummary" sheet holds."""
    counts = {(sid, fam): 0 for sid in snapshot_ids for fam in families}
    for v in violations:
        counts[(v["snapshot_id"], v["family"])] += 1
    return counts
