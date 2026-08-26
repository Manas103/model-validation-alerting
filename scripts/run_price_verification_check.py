"""Run the independent price-verification guardrails over the seeded
synthetic forward-mark curve and report, per seed, whether its target family
fired, plus the total raw alert count and the false-positive count (alerts
outside any seeded snapshot). Also writes the month-end Excel exception pack.
Prints the same report `docs/price_verification_output.txt` is a transcript
of.

    python scripts/run_price_verification_check.py
"""

import sys
from collections import defaultdict

sys.path.insert(0, ".")

from mvguard.marks_producer import (
    DEFAULT_SESSION_COUNT,
    SEED_SPECS,
    generate_clean_marks,
    generate_seeded_marks,
    seed_target_snapshot_ids,
)
from mvguard.price_verification import evaluate_stream, load_rules
from mvguard.exception_pack import write_exception_pack

DEFAULT_XLSX_PATH = "docs/exception_pack.xlsx"


def main():
    rules = load_rules()

    clean = generate_clean_marks()
    clean_alerts = evaluate_stream(clean, rules)
    total_marks = sum(len(s["tenors"]) for s in clean)
    print("clean baseline: {} sessions, {} snapshots, {} marks, {} alert(s)".format(
        DEFAULT_SESSION_COUNT, len(clean), total_marks, len(clean_alerts)))
    for a in clean_alerts:
        print("  UNEXPECTED CLEAN ALERT", a["rule_name"], a["snapshot_id"], a["observed"])

    seeded = generate_seeded_marks()
    alerts = evaluate_stream(seeded, rules)
    by_snapshot = defaultdict(list)
    for a in alerts:
        by_snapshot[a["snapshot_id"]].append(a)

    seeded_snapshot_ids = set()
    for spec in SEED_SPECS:
        seeded_snapshot_ids |= seed_target_snapshot_ids(spec)
    false_positive_snapshots = sorted(set(by_snapshot) - seeded_snapshot_ids)
    false_positive_alerts = sum(len(by_snapshot[s]) for s in false_positive_snapshots)

    print()
    print("seeded run: {} snapshots, {} marks, {} rule-level alert(s) from {} planted seeds".format(
        len(seeded), total_marks, len(alerts), len(SEED_SPECS)))
    print("false-positive snapshots (alerts with no planted seed): {}".format(
        false_positive_snapshots or "none"))
    print("false-positive alert count: {}".format(false_positive_alerts))

    caught = 0
    print()
    print("{:<12} {:<10} {:<20} {:<16} {}".format(
        "seed", "commodity", "target snapshot(s)", "target family", "families fired"))
    for spec in SEED_SPECS:
        target_ids = seed_target_snapshot_ids(spec)
        fired_families = sorted({
            a["family"] for sid in target_ids for a in by_snapshot.get(sid, [])
        })
        target_hit = spec["family"] in fired_families
        caught += int(target_hit)
        mark = "OK" if target_hit else "MISSED"
        print("{:<12} {:<10} {:<20} {:<16} {}  [{}]".format(
            spec["id"], spec["commodity"], ",".join(sorted(target_ids)), spec["family"],
            fired_families, mark))

    print()
    print("seeds caught (target family fired in its snapshot(s)): {}/{}".format(caught, len(SEED_SPECS)))
    print("false positives (alerts outside any seeded snapshot): {}".format(false_positive_alerts))
    print()
    print("note: a handful of seeds also trip a second family in the same seeded snapshot")
    print("(an off-market delta at one tenor mechanically shifts that tenor's adjacent")
    print("calendar spreads too). That is counted here as the seed being caught, not as")
    print("an extra false positive, since every one of those extra firings is inside a")
    print("snapshot that really was seeded. See README, 'What one seed actually breaks'.")

    xlsx_path = write_exception_pack(alerts, DEFAULT_XLSX_PATH)
    print()
    print("month-end exception pack written: {} ({} row(s))".format(xlsx_path, len(alerts)))


if __name__ == "__main__":
    main()
