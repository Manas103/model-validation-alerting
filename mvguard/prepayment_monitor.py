"""Fourth engine: monitoring a deployed prepayment model.

`mvguard/price_verification.py` is the closest template: a small, parallel
engine over a different domain, mixing a purely cross-sectional check with
one that needs per-key history. This module adds a third shape on top of
those two: `population_stability` needs the *whole population* of cohorts
observed in one month, not one record's own history, so it buffers
cohort-months by month and only closes a month out (compares it to the
fixed baseline established at month 0) once every cohort for that month has
arrived. `cpr_tolerance` stays purely cross-sectional and stateless, and
`stale_input` carries per-cohort state across the stream the same way
`price_verification.py`'s staleness check does.

Every alert this module yields carries the triggering input(s) under
`snapshot` and the exact numeric comparison under `observed`, matching the
convention the other three engines already use.
"""

import math
import os

import yaml

DEFAULT_RULES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rules",
    "prepayment_monitor_guardrails.yaml",
)

_REQUIRED_FIELDS = {
    "cpr_tolerance": ("tolerance",),
    "stale_input": ("stale_months",),
    "population_stability": ("threshold",),
}


class PrepaymentMonitorRuleError(Exception):
    """The prepayment-monitor rule file could not be loaded or is invalid."""


def load_rules(path=DEFAULT_RULES_PATH):
    """Load the family -> rule-config map for every *enabled* rule.

    Same strictness `mvguard/price_verification.py::load_rules` argues for:
    this is the file a non-engineer edits, so a missing or malformed field
    fails loudly at load time rather than silently producing a check that
    can never fire.
    """
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, list):
        raise PrepaymentMonitorRuleError("expected a YAML list of rule objects")

    by_family = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise PrepaymentMonitorRuleError("each rule must be a mapping, got {!r}".format(entry))
        name = entry.get("name")
        family = entry.get("family")
        if family is None:
            raise PrepaymentMonitorRuleError("rule {!r} is missing 'family'".format(name))
        if family not in _REQUIRED_FIELDS:
            raise PrepaymentMonitorRuleError(
                "rule {!r} has unknown family {!r}; expected one of {}".format(
                    name, family, sorted(_REQUIRED_FIELDS)
                )
            )
        for field in _REQUIRED_FIELDS[family]:
            if field not in entry:
                raise PrepaymentMonitorRuleError(
                    "rule {!r} (family {!r}) is missing required field {!r}".format(name, family, field)
                )
            value = entry[field]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise PrepaymentMonitorRuleError(
                    "rule {!r} field {!r} must be a positive number, got {!r}".format(name, field, value)
                )
        if entry.get("enabled", True):
            by_family[family] = entry
    return by_family


def _cohort_snapshot_id(snapshot):
    return "{}-m{:04d}".format(snapshot["cohort_id"], snapshot["month_index"])


def _alert(rule, snapshot, family, snapshot_fields, observed):
    return {
        "rule_name": rule["name"],
        "family": family,
        "severity": rule.get("severity", "warning"),
        "snapshot_id": _cohort_snapshot_id(snapshot),
        "cohort_id": snapshot["cohort_id"],
        "month_index": snapshot["month_index"],
        "ts": snapshot["ts"],
        "observed": observed,
        "snapshot": dict(
            snapshot_fields,
            cohort_id=snapshot["cohort_id"],
            month_index=snapshot["month_index"],
        ),
    }


def _check_cpr_tolerance(snapshot, rule, state):
    tol = rule["tolerance"]
    diff = snapshot["actual_cpr"] - snapshot["predicted_cpr"]
    if abs(diff) > tol:
        yield _alert(
            rule, snapshot, "cpr_tolerance",
            {"predicted_cpr": snapshot["predicted_cpr"], "actual_cpr": snapshot["actual_cpr"]},
            {"cpr_diff": round(diff, 4), "tolerance": tol},
        )


def _check_stale_input(snapshot, rule, state):
    threshold = int(rule["stale_months"])
    keyed = state.setdefault("stale_input", {})
    key = snapshot["cohort_id"]
    value = snapshot["refi_incentive"]
    previous = keyed.get(key)
    if previous is not None and previous["value"] == value:
        run_length = previous["run_length"] + 1
    else:
        run_length = 1
    keyed[key] = {"value": value, "run_length": run_length}
    # Fire exactly once, on the month a run first reaches the threshold, the
    # same one-shot-per-streak rule `price_verification.py` uses.
    if run_length == threshold:
        yield _alert(
            rule, snapshot, "stale_input",
            {"refi_incentive": value, "unchanged_months": run_length},
            {"consecutive_unchanged_months": run_length, "threshold": threshold},
        )


# --- population_stability: 5 equal-frequency bins (quintiles) computed from
# the first complete month observed in the stream (the model's baseline
# population), compared against a Population Stability Index over every
# later month. Quintiles rather than the textbook 10 deciles: with only 100
# cohorts in the population, 10 bins puts ~10 samples in each one, and the
# resulting sampling noise on the baseline run alone crossed the 0.25
# threshold on 3 of 149 unseeded months (see the README's "what broke"
# section). 5 bins doubles the samples per bin and is still within the
# 5-to-10-bin range PSI is conventionally computed over.

_POPULATION_BINS = 5


def _decile_edges(values):
    ordered = sorted(values)
    n = len(ordered)
    return [
        ordered[min(n - 1, max(0, round(i * n / _POPULATION_BINS) - 1))]
        for i in range(1, _POPULATION_BINS)
    ]


def _bin_index(value, edges):
    for i, edge in enumerate(edges):
        if value <= edge:
            return i
    return len(edges)


def _bin_proportions(values, edges):
    counts = [0] * (len(edges) + 1)
    for v in values:
        counts[_bin_index(v, edges)] += 1
    n = len(values)
    return [c / n for c in counts]


def psi(expected_proportions, actual_proportions, epsilon=1e-4):
    """Population Stability Index: sum((actual - expected) * ln(actual/expected))."""
    total = 0.0
    for expected, actual in zip(expected_proportions, actual_proportions):
        expected = max(expected, epsilon)
        actual = max(actual, epsilon)
        total += (actual - expected) * math.log(actual / expected)
    return total


def _population_alert(rule, month_index, psi_value, values, edges):
    return {
        "rule_name": rule["name"],
        "family": "population_stability",
        "severity": rule.get("severity", "warning"),
        "snapshot_id": "population-m{:04d}".format(month_index),
        "cohort_id": None,
        "month_index": month_index,
        "ts": None,
        "observed": {"psi": round(psi_value, 4), "threshold": rule["threshold"]},
        "snapshot": {"month_index": month_index, "cohort_count": len(values), "bin_edges": edges},
    }


def _check_population_stability(snapshot, rule, state, population_size):
    pop_state = state.setdefault("population", {"baseline_edges": None, "month_buffers": {}})
    month = snapshot["month_index"]
    buffers = pop_state["month_buffers"]
    buf = buffers.setdefault(month, [])
    buf.append(snapshot["refi_incentive"])
    if len(buf) < population_size:
        return
    values = buffers.pop(month)
    if pop_state["baseline_edges"] is None:
        # The first complete month establishes the baseline deciles; there
        # is nothing to compare it against yet.
        pop_state["baseline_edges"] = _decile_edges(values)
        pop_state["baseline_month"] = month
        return
    edges = pop_state["baseline_edges"]
    expected = [1.0 / _POPULATION_BINS] * _POPULATION_BINS  # equal-frequency by construction
    actual = _bin_proportions(values, edges)
    psi_value = psi(expected, actual)
    if psi_value > rule["threshold"]:
        yield _population_alert(rule, month, psi_value, values, edges)


_CHECKS = {
    "cpr_tolerance": _check_cpr_tolerance,
    "stale_input": _check_stale_input,
}


def check_snapshot(snapshot, rules, state, population_size):
    """Yield one alert dict per violation this snapshot's rules find."""
    for family, rule in rules.items():
        if family == "population_stability":
            for alert in _check_population_stability(snapshot, rule, state, population_size):
                yield alert
            continue
        check = _CHECKS.get(family)
        if check is None:
            raise PrepaymentMonitorRuleError("no checker implemented for family {!r}".format(family))
        for alert in check(snapshot, rule, state):
            yield alert


def evaluate_stream(snapshots, rules=None, population_size=100):
    """Evaluate every snapshot in order; return the flat list of all alerts.

    ``snapshots`` must be in month-major, cohort-minor order (the order
    `prepayment_monitor_producer.py` yields them in) or both the stale_input
    per-cohort history and the population_stability month buffering will be
    evaluated against a scrambled panel.
    """
    if rules is None:
        rules = load_rules()
    state = {}
    alerts = []
    for snapshot in snapshots:
        alerts.extend(check_snapshot(snapshot, rules, state, population_size))
    return alerts
