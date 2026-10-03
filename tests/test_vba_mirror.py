"""Tests for the VBA month-end workbook extension.

`mvguard/vba_mirror.py` is a Python mirror of the real, uncommitted-to-run
`vba/MonthEndReview.bas` (this machine has no Excel). Two things are checked
here: that the mirror's own measurement meets the same "24/24 seeded, 0 false
positives" bar the Python engine already meets, and that the mirror and the
engine agree cell for cell, which is the actual reconciliation claim.
"""

from mvguard.surface_guardrails import evaluate_stream, load_rules
from mvguard.surface_producer import SEED_SPECS, generate_clean_snapshots, generate_seeded_snapshots
from mvguard import vba_mirror

FAMILIES = ("parity", "monotonicity", "butterfly", "calendar")


def _engine_counts(snapshots):
    alerts = evaluate_stream(snapshots, load_rules())
    counts = {(s["snapshot_id"], fam): 0 for s in snapshots for fam in FAMILIES}
    for a in alerts:
        counts[(a["snapshot_id"], a["family"])] += 1
    return counts


def _mirror_counts(snapshots):
    rows = vba_mirror.flatten_snapshots(snapshots)
    violations = vba_mirror.evaluate_rows(rows)
    snapshot_ids = [s["snapshot_id"] for s in snapshots]
    return vba_mirror.summarize_by_snapshot_family(violations, snapshot_ids, FAMILIES)


def test_mirror_clean_baseline_has_no_violations():
    clean = generate_clean_snapshots()
    counts = _mirror_counts(clean)
    assert sum(counts.values()) == 0


def test_mirror_catches_all_24_seeds_with_no_false_positives():
    seeded = generate_seeded_snapshots()
    counts = _mirror_counts(seeded)
    seeded_snapshot_ids = {"snap-{:04d}".format(s["snapshot_index"]) for s in SEED_SPECS}

    caught = 0
    for spec in SEED_SPECS:
        sid = "snap-{:04d}".format(spec["snapshot_index"])
        if counts[(sid, spec["family"])] > 0:
            caught += 1
    assert caught == len(SEED_SPECS)

    false_positives = sum(
        c for (sid, fam), c in counts.items() if sid not in seeded_snapshot_ids
    )
    assert false_positives == 0


def test_mirror_reconciles_cell_for_cell_against_the_engine():
    """The actual resume claim: the workbook (mirror, standing in for the
    uncommitted-to-run VBA macro) and the Python engine agree on every
    (snapshot, family) violation count over the full 12,000-quote surface."""
    seeded = generate_seeded_snapshots()
    engine_counts = _engine_counts(seeded)
    mirror_counts = _mirror_counts(seeded)

    assert set(engine_counts) == set(mirror_counts)
    mismatches = [
        key for key in engine_counts
        if engine_counts[key] != mirror_counts[key]
    ]
    assert mismatches == [], "mismatched (snapshot, family) cells: {}".format(mismatches)


def test_mirror_reconciles_on_the_clean_surface_too():
    clean = generate_clean_snapshots()
    engine_counts = _engine_counts(clean)
    mirror_counts = _mirror_counts(clean)
    assert engine_counts == mirror_counts
