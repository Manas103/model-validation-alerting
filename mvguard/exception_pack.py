"""The month-end Excel exception pack: one row per triggering flag, naming
the exact inputs behind it.

This is deliberately a thin module. The engine in `price_verification.py`
already decided what fired and already carries the triggering inputs on each
alert's `snapshot` and `observed` dicts; this module's only job is to lay
those same fields out as workbook rows and columns, one sheet per check
family plus a combined "All exceptions" sheet, so nothing here re-derives or
re-checks anything the engine already computed.
"""

import datetime

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

# Columns common to every family, in the order they appear in the workbook.
_COMMON_COLUMNS = ("check_family", "rule_name", "severity", "commodity", "session_index")

# Family-specific columns, appended after the common ones. Each maps to a
# lookup that pulls the value out of the alert's snapshot/observed dicts.
_FAMILY_COLUMNS = {
    "staleness": (
        "tenor", "submitted_mark", "unchanged_sessions", "threshold",
    ),
    "off_market": (
        "tenor", "submitted_mark", "independent_mark", "relative_deviation", "tolerance",
    ),
    "calendar_spread": (
        "tenor_short", "tenor_long", "submitted_mark_short", "submitted_mark_long",
        "independent_mark_short", "independent_mark_long", "relative_spread_deviation", "tolerance",
    ),
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
    """Write the month-end exception pack to ``path``. Returns ``path``.

    One sheet per check family (only the columns that family's alerts
    actually carry), plus an "All exceptions" summary sheet with every alert
    and its full triggering snapshot serialized into one text column so
    nothing is lost even when mixing families in one view.
    """
    wb = Workbook()
    wb.remove(wb.active)

    by_family = {}
    for alert in alerts:
        by_family.setdefault(alert["family"], []).append(alert)

    for family in ("staleness", "off_market", "calendar_spread"):
        family_alerts = by_family.get(family, [])
        _write_sheet(wb, family, _FAMILY_COLUMNS[family], family_alerts)

    summary = wb.create_sheet("All exceptions", 0)
    summary.append(["generated_at_utc", "total_exceptions"])
    summary.append([datetime.datetime.now(datetime.timezone.utc).isoformat(), len(alerts)])
    summary.append([])
    header = ["check_family", "rule_name", "severity", "commodity", "session_index",
              "triggering_inputs", "observed"]
    summary.append(header)
    for cell in summary[4]:
        cell.font = Font(bold=True)
    for alert in alerts:
        summary.append([
            alert["family"],
            alert["rule_name"],
            alert["severity"],
            alert["commodity"],
            alert["session_index"],
            str(alert["snapshot"]),
            str(alert["observed"]),
        ])
    for col_index in range(1, len(header) + 1):
        summary.column_dimensions[get_column_letter(col_index)].width = 22

    wb.save(path)
    return path
