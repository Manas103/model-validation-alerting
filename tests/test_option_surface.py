"""Tests for the no-arbitrage option-surface extension.

`mvguard/surface_producer.py` builds the synthetic surface and plants 24
known violations; `mvguard/surface_guardrails.py` is the engine under test.
The two are checked against each other here, which is a real cross-check
only because the producer's docstring states, and `test_clean_baseline_has_no_violations`
confirms, that the *unseeded* surface is arbitrage-free by construction
(built from one shared Black-Scholes formula per snapshot), independent of
whatever the engine's own arithmetic happens to do.
"""

from mvguard.surface_guardrails import check_snapshot, evaluate_stream, load_rules
from mvguard.surface_producer import (
    SEED_SPECS,
    generate_clean_snapshots,
    generate_seeded_snapshots,
)


def test_rule_file_loads_all_four_families():
    rules = load_rules()
    assert set(rules) == {"parity", "monotonicity", "butterfly", "calendar"}
    for rule in rules.values():
        assert rule["tolerance"] > 0
        assert rule["severity"] in ("info", "warning", "critical")


def test_clean_baseline_has_no_violations():
    rules = load_rules()
    clean = generate_clean_snapshots()
    assert sum(len(s["quotes"]) for s in clean) == 12000
    alerts = evaluate_stream(clean, rules)
    assert alerts == []


def test_seeded_stream_catches_all_24_with_zero_false_positives():
    rules = load_rules()
    seeded = generate_seeded_snapshots()
    alerts = evaluate_stream(seeded, rules)

    by_snapshot = {}
    for alert in alerts:
        by_snapshot.setdefault(alert["snapshot_id"], []).append(alert)

    seeded_ids = {"snap-{:04d}".format(spec["snapshot_index"]) for spec in SEED_SPECS}
    false_positive_ids = set(by_snapshot) - seeded_ids
    assert false_positive_ids == set(), (
        "alert(s) fired in a snapshot with no planted seed: {}".format(false_positive_ids)
    )

    missed = []
    for spec in SEED_SPECS:
        sid = "snap-{:04d}".format(spec["snapshot_index"])
        families_fired = {a["family"] for a in by_snapshot.get(sid, [])}
        if spec["family"] not in families_fired:
            missed.append(spec["id"])
    assert missed == [], "seed(s) whose target family never fired: {}".format(missed)


def test_every_alert_carries_triggering_inputs():
    rules = load_rules()
    seeded = generate_seeded_snapshots()
    alerts = evaluate_stream(seeded, rules)
    assert len(alerts) > 0
    for alert in alerts:
        assert "snapshot" in alert and alert["snapshot"], "alert missing triggering snapshot data"
        assert "observed" in alert and alert["observed"], "alert missing observed values"
        assert "spot" in alert["snapshot"] and "rate" in alert["snapshot"]


def test_parity_seed_does_not_collide_with_other_families():
    # Parity deltas are small (just past the 0.05 tolerance) and touch only
    # one field, so a parity seed should not also perturb monotonicity or
    # butterfly at the same quote the way an oversized delta would.
    rules = load_rules()
    seeded = generate_seeded_snapshots()
    by_id = {s["snapshot_id"]: s for s in seeded}
    isolated_parity_seeds = [
        spec for spec in SEED_SPECS
        if spec["family"] == "parity" and spec["id"] in ("parity-01", "parity-02", "parity-03", "parity-05")
    ]
    assert isolated_parity_seeds, "expected at least one cleanly isolated parity seed"
    for spec in isolated_parity_seeds:
        snapshot = by_id["snap-{:04d}".format(spec["snapshot_index"])]
        families = {a["family"] for a in check_snapshot(snapshot, rules)}
        assert families == {"parity"}, "expected only parity to fire, got {}".format(families)


def test_butterfly_requires_equally_spaced_strikes():
    rules = load_rules()
    clean = generate_clean_snapshots(count=1)
    snapshot = clean[0]
    # Corrupt spacing on purpose: drop the 90-strike quote so 80/100/110 is
    # the only remaining local triple, which is not equally spaced and must
    # be skipped rather than misapplied.
    snapshot["quotes"] = [q for q in snapshot["quotes"] if not (q["strike"] == 90.0 and q["maturity_days"] == 120)]
    alerts = list(check_snapshot(snapshot, {"butterfly": rules["butterfly"]}))
    assert alerts == []
