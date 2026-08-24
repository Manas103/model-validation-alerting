"""Run the no-arbitrage guardrails over the seeded synthetic option surface
and report, per seed, whether its target family fired, plus the total raw
alert count and the false-positive count (alerts in a snapshot that was not
seeded at all). Prints the same report `docs/option_surface_output.txt` is a
transcript of.

    python scripts/run_option_surface_check.py
"""

import sys
from collections import defaultdict

sys.path.insert(0, ".")

from mvguard.surface_producer import (
    DEFAULT_SNAPSHOT_COUNT,
    SEED_SPECS,
    generate_clean_snapshots,
    generate_seeded_snapshots,
)
from mvguard.surface_guardrails import evaluate_stream, load_rules


def main():
    rules = load_rules()

    clean = generate_clean_snapshots()
    clean_alerts = evaluate_stream(clean, rules)
    total_quotes = sum(len(s["quotes"]) for s in clean)
    print("clean baseline: {} snapshots, {} quotes, {} alert(s)".format(
        len(clean), total_quotes, len(clean_alerts)))
    for a in clean_alerts:
        print("  UNEXPECTED CLEAN ALERT", a["rule_name"], a["snapshot_id"], a["observed"])

    seeded = generate_seeded_snapshots()
    alerts = evaluate_stream(seeded, rules)
    by_snapshot = defaultdict(list)
    for a in alerts:
        by_snapshot[a["snapshot_id"]].append(a)

    seeded_snapshot_ids = {"snap-{:04d}".format(s["snapshot_index"]) for s in SEED_SPECS}
    false_positive_snapshots = sorted(set(by_snapshot) - seeded_snapshot_ids)
    false_positive_alerts = sum(len(by_snapshot[s]) for s in false_positive_snapshots)

    print()
    print("seeded run: {} snapshots, {} quotes, {} rule-level alert(s) from {} planted seeds".format(
        len(seeded), total_quotes, len(alerts), len(SEED_SPECS)))
    print("false-positive snapshots (alerts with no planted seed): {}".format(
        false_positive_snapshots or "none"))
    print("false-positive alert count: {}".format(false_positive_alerts))

    caught = 0
    print()
    print("{:<14} {:<10} {:<14} {}".format("seed", "snapshot", "target family", "families fired"))
    for spec in SEED_SPECS:
        sid = "snap-{:04d}".format(spec["snapshot_index"])
        fired_families = sorted({a["family"] for a in by_snapshot.get(sid, [])})
        target_hit = spec["family"] in fired_families
        caught += int(target_hit)
        mark = "OK" if target_hit else "MISSED"
        print("{:<14} {:<10} {:<14} {}  [{}]".format(
            spec["id"], sid, spec["family"], fired_families, mark))

    print()
    print("seeds caught (target family fired in its snapshot): {}/{}".format(caught, len(SEED_SPECS)))
    print("false positives (alerts outside any seeded snapshot): {}".format(false_positive_alerts))
    print()
    print("note: several seeds trip more than one family in the same snapshot, because a")
    print("mispricing large enough to clear a wide monotonicity/calendar tolerance is often")
    print("also large enough to clear the much tighter parity tolerance at the same quote.")
    print("that is counted here as the seed being caught, not as extra false positives,")
    print("since every one of those extra firings is inside a snapshot that really was seeded.")


if __name__ == "__main__":
    main()
