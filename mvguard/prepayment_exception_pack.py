"""The month-end Excel exception pack for the prepayment-monitoring
extension: one row per triggering flag, naming the exact inputs behind it.

Deliberately thin, the same reasoning `mvguard/exception_pack.py` gives for
the price-verification extension: `prepayment_monitor.py` already decided
what fired and already carries the triggering inputs on each alert's
`snapshot` and `observed` dicts; this module's only job is to lay those out
as workbook rows and columns, one sheet per check family plus a combined
"All exceptions" sheet.
"""

import datetime

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

_COMMON_COLUMNS = ("check_family", "rule_name", "severity", "cohort_id", "month_index")

_FAMILY_COLUMNS = {
    "cpr_tolerance": ("predicted_cpr", "actual_cpr", "cpr_diff", "tolerance"),
    "stale_input": ("refi_incentive", "unchanged_months", "threshold"),
    "population_stability": ("cohort_count", "psi", "threshold"),
}


def _field(alert, column):
    if column in _COMMON_COLUMNS:
        if column == "check_family":
            return alert["family"]
        return alert.get(column, alert["snapshot"].get(column))
    if column in alert["snapshot"]:
        return alert["snapshot"][column]
    if column in alert["observed"]:
        return alert["observed"][column]
    return None


def _write_sheet(wb, title, columns, alerts):
    ws = wb.create_sheet(title)
    header = list(_COMMON_COLUMNS) + list(columns)
    ws.append(header)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for alert in alerts:
        ws.append([_field(alert, col) for col in header])
    for col_index, col_name in enumerate(header, start=1):
        width = max(len(col_name) + 2, 12)
        ws.column_dimensions[get_column_letter(col_index)].width = width
    return ws


def write_exception_pack(alerts, path):
    """Write the month-end prepayment-monitoring exception pack to ``path``.

    One sheet per check family (only the columns that family's alerts
    actually carry), plus an "All exceptions" summary sheet with every
    alert's full triggering snapshot and observed comparison. Returns
    ``path``.
    """
    wb = Workbook()
    wb.remove(wb.active)

    by_family = {}
    for alert in alerts:
        by_family.setdefault(alert["family"], []).append(alert)

    for family in ("cpr_tolerance", "stale_input", "population_stability"):
        family_alerts = by_family.get(family, [])
        _write_sheet(wb, family, _FAMILY_COLUMNS[family], family_alerts)

    summary = wb.create_sheet("All exceptions", 0)
    summary.append(["generated_at_utc", "total_exceptions"])
    summary.append([datetime.datetime.now(datetime.timezone.utc).isoformat(), len(alerts)])
    summary.append([])
    header = ["check_family", "rule_name", "severity", "cohort_id", "month_index",
              "triggering_inputs", "observed"]
    summary.append(header)
    for cell in summary[4]:
        cell.font = Font(bold=True)
    for alert in alerts:
        summary.append([
            alert["family"],
            alert["rule_name"],
            alert["severity"],
            alert["cohort_id"],
            alert["month_index"],
            str(alert["snapshot"]),
            str(alert["observed"]),
        ])
    for col_index in range(1, len(header) + 1):
        summary.column_dimensions[get_column_letter(col_index)].width = 22

    wb.save(path)
    return path
