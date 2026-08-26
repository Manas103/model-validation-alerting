"""Run the prepayment-model-monitoring guardrails over the seeded synthetic
cohort panel and report, per seed, whether its target family fired, plus the
total raw alert count and the false-positive count (alerts outside any
seeded snapshot). Also writes the month-end Excel exception pack. Prints the
same report `docs/prepayment_monitor_output.txt` is a transcript of.

    python scripts/run_prepayment_monitor_check.py
"""

import sys
from collections import defaultdict

sys.path.insert(0, ".")

from mvguard.prepayment_monitor_producer import (
    COHORT_COUNT,
    MONTH_COUNT,
    SEED_SPECS,
    generate_clean_snapshots,
    generate_seeded_snapshots,
    seed_target_snapshot_ids,
)
from mvguard.prepayment_monitor import evaluate_stream, load_rules
from mvguard.prepayment_exception_pack import write_exception_pack

DEFAULT_XLSX_PATH = "docs/prepayment_exception_pack.xlsx"


def main():
    rules = load_rules()

    clean = generate_clean_snapshots()
    clean_alerts = evaluate_stream(clean, rules, population_size=COHORT_COUNT)
    print("clean baseline: {} cohorts, {} months, {} cohort-months, {} alert(s)".format(
        COHORT_COUNT, MONTH_COUNT, len(clean), len(clean_alerts)))
    for a in clean_alerts:
        print("  UNEXPECTED CLEAN ALERT", a["rule_name"], a["snapshot_id"], a["observed"])

    seeded = generate_seeded_snapshots()
    alerts = evaluate_stream(seeded, rules, population_size=COHORT_COUNT)
    by_snapshot = defaultdict(list)
    for a in alerts:
        by_snapshot[a["snapshot_id"]].append(a)

    seeded_snapshot_ids = set()
    for spec in SEED_SPECS:
        seeded_snapshot_ids |= seed_target_snapshot_ids(spec)
    false_positive_snapshots = sorted(set(by_snapshot) - seeded_snapshot_ids)
    false_positive_alerts = sum(len(by_snapshot[s]) for s in false_positive_snapshots)

    print()
    print("seeded run: {} cohort-months, {} rule-level alert(s) from {} planted seeds".format(
        len(seeded), len(alerts), len(SEED_SPECS)))
    print("false-positive snapshots (alerts with no planted seed): {}".format(
        false_positive_snapshots or "none"))
    print("false-positive alert count: {}".format(false_positive_alerts))

    caught = 0
    print()
    print("{:<10} {:<8} {:<28} {:<20} {}".format(
        "seed", "family", "target snapshot(s)", "families fired", "result"))
    for spec in SEED_SPECS:
        target_ids = seed_target_snapshot_ids(spec)
        fired_families = sorted({
            a["family"] for sid in target_ids for a in by_snapshot.get(sid, [])
        })
        target_hit = spec["family"] in fired_families
        caught += int(target_hit)
        mark = "OK" if target_hit else "MISSED"
        print("{:<10} {:<8} {:<28} {:<20} {}".format(
            spec["id"], spec["family"], ",".join(sorted(target_ids)), str(fired_families), mark))

    print()
    print("seeds caught (target family fired in its snapshot(s)): {}/{}".format(caught, len(SEED_SPECS)))
    print("false positives (alerts outside any seeded snapshot): {}".format(false_positive_alerts))

    xlsx_path = write_exception_pack(alerts, DEFAULT_XLSX_PATH)
    print()
    print("month-end exception pack written: {} ({} row(s))".format(xlsx_path, len(alerts)))


if __name__ == "__main__":
    main()
