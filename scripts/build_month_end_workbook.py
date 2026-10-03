"""Build the VBA-driven month-end review workbook and reconcile it to the
Python engine, cell for cell.

Runs the same seeded 300-snapshot / 12,000-quote option surface
`scripts/run_option_surface_check.py` measures, through two independently
coded implementations of the four no-arbitrage checks:

    1. `mvguard/surface_guardrails.py`, the Python engine already covered by
       tests/test_option_surface.py (24/24 seeded violations, 0 false
       positives, previously measured).
    2. `mvguard/vba_mirror.py`, a Python transliteration of the real VBA
       macro `vba/MonthEndReview.bas` (this machine has no Excel; see that
       module's docstring for why a mirror stands in for a live run).

Writes `docs/month_end_exception_pack_vba.xlsx` with the Quotes sheet the
real workbook's "Quotes" tab would hold, the two engines' per-snapshot,
per-family violation counts, and a Reconciliation sheet diffing them cell
for cell.

    python scripts/build_month_end_workbook.py
"""

import sys

sys.path.insert(0, ".")

import openpyxl

from mvguard.surface_producer import DEFAULT_SNAPSHOT_COUNT, SEED_SPECS, generate_seeded_snapshots
from mvguard.surface_guardrails import evaluate_stream, load_rules
from mvguard import vba_mirror

OUTPUT_PATH = "docs/month_end_exception_pack_vba.xlsx"
FAMILIES = ("parity", "monotonicity", "butterfly", "calendar")


def engine_summary(snapshots):
    alerts = evaluate_stream(snapshots, load_rules())
    counts = {(s["snapshot_id"], fam): 0 for s in snapshots for fam in FAMILIES}
    for a in alerts:
        counts[(a["snapshot_id"], a["family"])] += 1
    return counts, alerts


def mirror_summary(snapshots):
    rows = vba_mirror.flatten_snapshots(snapshots)
    violations = vba_mirror.evaluate_rows(rows)
    snapshot_ids = [s["snapshot_id"] for s in snapshots]
    counts = vba_mirror.summarize_by_snapshot_family(violations, snapshot_ids, FAMILIES)
    return rows, counts, violations


def build_workbook(snapshots, rows, engine_counts, mirror_counts, path, quotes_sheet_snapshot_ids):
    wb = openpyxl.Workbook()

    # The reconciliation below (EngineSummary/VBASummary/Reconciliation) runs
    # over the full 300-snapshot, 12,000-quote surface. The committed "Quotes"
    # sheet only holds the 24 seeded snapshots (960 rows): enough for a
    # reviewer to see every planted violation in context without checking in
    # a 500KB+ raw dump of a fully synthetic surface. The full 12,000-quote
    # run is in docs/vba_reconciliation_output.txt and reproduces with
    # `python scripts/build_month_end_workbook.py`.
    ws_quotes = wb.active
    ws_quotes.title = "Quotes"
    ws_quotes.append(
        ["SnapshotID", "Spot", "Rate", "Strike", "MaturityDays", "MaturityYears", "CallMid", "PutMid"]
    )
    for r in rows:
        if r["snapshot_id"] not in quotes_sheet_snapshot_ids:
            continue
        ws_quotes.append(
            [r["snapshot_id"], r["spot"], r["rate"], r["strike"], r["maturity_days"],
             r["maturity_years"], r["call_mid"], r["put_mid"]]
        )

    ws_engine = wb.create_sheet("EngineSummary")
    ws_engine.append(["SnapshotID", "Family", "Count"])
    for (sid, fam), count in sorted(engine_counts.items()):
        ws_engine.append([sid, fam, count])

    ws_vba = wb.create_sheet("VBASummary")
    ws_vba.append(["SnapshotID", "Family", "Count"])
    for (sid, fam), count in sorted(mirror_counts.items()):
        ws_vba.append([sid, fam, count])

    ws_exceptions = wb.create_sheet("Exceptions")
    ws_exceptions.append(["SnapshotID", "Family", "Detail1", "Detail2", "Detail3", "Observed"])

    ws_recon = wb.create_sheet("Reconciliation")
    ws_recon.append(["SnapshotID", "Family", "EngineCount", "VBACount", "Match"])
    mismatches = 0
    for key in sorted(engine_counts.keys()):
        sid, fam = key
        e_count = engine_counts[key]
        v_count = mirror_counts.get(key, -1)
        match = e_count == v_count
        if not match:
            mismatches += 1
        ws_recon.append([sid, fam, e_count, v_count, match])

    ws_summary = wb.create_sheet("Summary", 0)
    ws_summary.append(["metric", "value"])
    ws_summary.append(["snapshots", len(snapshots)])
    ws_summary.append(["quotes", sum(len(s["quotes"]) for s in snapshots)])
    ws_summary.append(["planted seeds", len(SEED_SPECS)])
    ws_summary.append(["reconciliation cells (snapshot x family)", len(engine_counts)])
    ws_summary.append(["mismatches", mismatches])
    ws_summary.append(["ALL MATCH", mismatches == 0])

    wb.save(path)
    return mismatches


def main():
    snapshots = generate_seeded_snapshots()
    total_quotes = sum(len(s["quotes"]) for s in snapshots)

    engine_counts, engine_alerts = engine_summary(snapshots)
    rows, mirror_counts, mirror_violations = mirror_summary(snapshots)

    seeded_snapshot_ids = {"snap-{:04d}".format(s["snapshot_index"]) for s in SEED_SPECS}

    def seeds_caught(counts):
        caught = 0
        for spec in SEED_SPECS:
            sid = "snap-{:04d}".format(spec["snapshot_index"])
            if counts[(sid, spec["family"])] > 0:
                caught += 1
        return caught

    def false_positive_count(counts):
        total = 0
        for (sid, fam), c in counts.items():
            if sid not in seeded_snapshot_ids:
                total += c
        return total

    print("month-end workbook reconciliation: {} snapshots, {} quotes, {} planted seeds".format(
        len(snapshots), total_quotes, len(SEED_SPECS)))
    print()
    print("Python engine (mvguard/surface_guardrails.py):")
    print("  seeds caught: {}/{}".format(seeds_caught(engine_counts), len(SEED_SPECS)))
    print("  false positives: {}".format(false_positive_count(engine_counts)))
    print()
    print("VBA mirror (mvguard/vba_mirror.py, standing in for vba/MonthEndReview.bas):")
    print("  seeds caught: {}/{}".format(seeds_caught(mirror_counts), len(SEED_SPECS)))
    print("  false positives: {}".format(false_positive_count(mirror_counts)))
    print()

    mismatches = build_workbook(
        snapshots, rows, engine_counts, mirror_counts, OUTPUT_PATH, seeded_snapshot_ids
    )
    cells_checked = len(engine_counts)

    print("cell-for-cell reconciliation: {} (snapshot, family) cells checked, {} mismatch(es)".format(
        cells_checked, mismatches))
    print("ALL MATCH: {}".format(mismatches == 0))
    print()
    print("workbook written to {}".format(OUTPUT_PATH))

    return 0 if mismatches == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
