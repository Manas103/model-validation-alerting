"""Independent price verification for a forward-mark curve.

`mvguard/surface_guardrails.py` is the closest template: a small, parallel
engine, cross-sectional over one snapshot, rather than a bolt-on to the
streaming expression language in `mvguard/engine.py`. This module is a third
engine of the same shape, over a different domain: a commodity desk's daily
submitted forward marks, checked against an independent source, rather than
an option-chain surface checked against itself.

Two of the three checks here (off-market, calendar-spread) are exactly as
cross-sectional as the option-surface checks: they compare several tenors of
the *same* session against each other or against the independent curve.
The third, staleness, is genuinely different: "unchanged for N consecutive
sessions" is a statement about one tenor's own history, not about one
instant. Rather than force staleness into a snapshot-only shape it does not
fit, this module carries one small piece of mutable state across the stream
(the previous submitted value and the current run length, per commodity and
tenor) the same way `mvguard/engine.py` carries cooldown state across a
stream of scoring records. Every other check in this file stays pure and
stateless.

Every alert this module yields carries the triggering input(s) under
`snapshot` and the exact numeric comparison under `observed`, matching the
convention `mvguard/engine.py` and `mvguard/surface_guardrails.py` already
use.
"""

import os

import yaml

DEFAULT_RULES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rules",
    "price_verification_guardrails.yaml",
)

_REQUIRED_FIELDS = {
    "staleness": ("stale_sessions",),
    "off_market": ("tolerance",),
    "calendar_spread": ("tolerance",),
}


class PriceVerificationRuleError(Exception):
    """The price-verification rule file could not be loaded or is invalid."""


def load_rules(path=DEFAULT_RULES_PATH):
    """Load the family -> rule-config map for every *enabled* rule.

    Strict on purpose, the same reasoning `mvguard/rules.py` gives for the
    streaming rule file: this is the file a non-engineer edits, so a missing
    or malformed field should fail loudly at load time rather than silently
    produce a check that can never fire (or fires on everything).
    """
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, list):
        raise PriceVerificationRuleError("expected a YAML list of rule objects")

    by_family = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise PriceVerificationRuleError("each rule must be a mapping, got {!r}".format(entry))
        name = entry.get("name")
        family = entry.get("family")
        if family is None:
            raise PriceVerificationRuleError("rule {!r} is missing 'family'".format(name))
        if family not in _REQUIRED_FIELDS:
            raise PriceVerificationRuleError(
                "rule {!r} has unknown family {!r}; expected one of {}".format(
                    name, family, sorted(_REQUIRED_FIELDS)
                )
            )
        for field in _REQUIRED_FIELDS[family]:
            if field not in entry:
                raise PriceVerificationRuleError(
                    "rule {!r} (family {!r}) is missing required field {!r}".format(name, family, field)
                )
            value = entry[field]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise PriceVerificationRuleError(
                    "rule {!r} field {!r} must be a positive number, got {!r}".format(name, field, value)
                )
        if entry.get("enabled", True):
            by_family[family] = entry
    return by_family


def _snapshot_id(snapshot):
    return "{}-sess{:04d}".format(snapshot["commodity"], snapshot["session_index"])


def _alert(rule, snapshot, family, snapshot_fields, observed):
    return {
        "rule_name": rule["name"],
        "family": family,
        "severity": rule.get("severity", "warning"),
        "snapshot_id": _snapshot_id(snapshot),
        "commodity": snapshot["commodity"],
        "session_index": snapshot["session_index"],
        "ts": snapshot["ts"],
        "observed": observed,
        "snapshot": dict(
            snapshot_fields,
            commodity=snapshot["commodity"],
            session_index=snapshot["session_index"],
        ),
    }


def _check_off_market(snapshot, rule, state):
    tol = rule["tolerance"]
    for t in snapshot["tenors"]:
        submitted = t["submitted_mark"]
        independent = t["independent_mark"]
        rel_diff = (submitted - independent) / independent
        if abs(rel_diff) > tol:
            yield _alert(
                rule, snapshot, "off_market",
                {"tenor": t["tenor"], "submitted_mark": submitted, "independent_mark": independent},
                {"relative_deviation": rel_diff, "tolerance": tol},
            )


def _check_calendar_spread(snapshot, rule, state):
    tol = rule["tolerance"]
    tenors = snapshot["tenors"]
    for i in range(1, len(tenors)):
        lo, hi = tenors[i - 1], tenors[i]
        submitted_spread = hi["submitted_mark"] - lo["submitted_mark"]
        independent_spread = hi["independent_mark"] - lo["independent_mark"]
        diff = submitted_spread - independent_spread
        rel_diff = diff / lo["independent_mark"]
        if abs(rel_diff) > tol:
            yield _alert(
                rule, snapshot, "calendar_spread",
                {
                    "tenor_short": lo["tenor"], "tenor_long": hi["tenor"],
                    "submitted_mark_short": lo["submitted_mark"], "submitted_mark_long": hi["submitted_mark"],
                    "independent_mark_short": lo["independent_mark"], "independent_mark_long": hi["independent_mark"],
                },
                {
                    "submitted_spread": submitted_spread, "independent_spread": independent_spread,
                    "relative_spread_deviation": rel_diff, "tolerance": tol,
                },
            )


def _check_staleness(snapshot, rule, state):
    threshold = int(rule["stale_sessions"])
    for t in snapshot["tenors"]:
        key = (snapshot["commodity"], t["tenor"])
        value = t["submitted_mark"]
        previous = state.get(key)
        if previous is not None and previous["value"] == value:
            run_length = previous["run_length"] + 1
        else:
            run_length = 1
        state[key] = {"value": value, "run_length": run_length}
        # Fire exactly once, on the session the run first reaches the
        # threshold. A run that keeps going stays flagged (this session's
        # alert already named it), and a run that resets starts counting
        # again from 1, so it can only fire once per stale streak.
        if run_length == threshold:
            yield _alert(
                rule, snapshot, "staleness",
                {"tenor": t["tenor"], "submitted_mark": value, "unchanged_sessions": run_length},
                {"consecutive_unchanged_sessions": run_length, "threshold": threshold},
            )


_CHECKS = {
    "off_market": _check_off_market,
    "calendar_spread": _check_calendar_spread,
    "staleness": _check_staleness,
}


def check_snapshot(snapshot, rules, state):
    """Yield one alert dict per violation this snapshot's rules find.

    ``state`` is a plain dict the caller owns and threads through every call,
    in stream order; only the staleness check reads or writes it.
    """
    for family, rule in rules.items():
        check = _CHECKS.get(family)
        if check is None:
            raise PriceVerificationRuleError("no checker implemented for family {!r}".format(family))
        for alert in check(snapshot, rule, state):
            yield alert


def evaluate_stream(snapshots, rules=None):
    """Evaluate every snapshot in order; return the flat list of all alerts.

    ``snapshots`` must be in session order per commodity (interleaving other
    commodities' snapshots between them is fine) or the staleness state will
    be evaluated against a scrambled history.
    """
    if rules is None:
        rules = load_rules()
    state = {}
    alerts = []
    for snapshot in snapshots:
        alerts.extend(check_snapshot(snapshot, rules, state))
    return alerts
