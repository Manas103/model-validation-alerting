"""Cross-sectional no-arbitrage guardrails over an option-chain surface.

`mvguard/engine.py` evaluates one rule against one record plus that record's
own time-windowed history: the unit of comparison is a field against itself
over event time, grouped by `group_by`. A no-arbitrage check is a different
shape of comparison entirely: put-call parity compares two fields of the
*same* quote to each other, and monotonicity, butterfly convexity and
calendar-spread each compare several *different* quotes from the *same*
instant against each other. None of that is "a field's history"; teaching
the tokenizer and parser to traverse an array field and compare its elements
pairwise would mean rebuilding the evaluation model the streaming rules
depend on, for a check that never needs a window or a cooldown.

So this module is a second, parallel engine rather than a bolt-on to the
first: same shape of contract (YAML declares what is armed, at what
tolerance, at what severity; this file is the only place the comparison
arithmetic lives, so it stays auditable in one spot), applied to whichever
document shape the domain actually needs. `rules/option_surface_guardrails.yaml`
is what a non-engineer edits; nothing below reads config from anywhere else.

Every alert this module yields carries the triggering quote(s) under
`snapshot` and the exact numbers compared under `observed`, matching the
convention `mvguard/engine.py` already uses for its alerts.
"""

import math
import os

import yaml

DEFAULT_RULES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rules",
    "option_surface_guardrails.yaml",
)


class SurfaceRuleError(Exception):
    """The option-surface rule file could not be loaded."""


def load_rules(path=DEFAULT_RULES_PATH):
    """Load the family -> rule-config map for every *enabled* rule."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, list):
        raise SurfaceRuleError("expected a YAML list of rule objects")
    by_family = {}
    for entry in raw:
        family = entry.get("family")
        if family is None:
            raise SurfaceRuleError("rule {!r} is missing 'family'".format(entry.get("name")))
        if entry.get("enabled", True):
            by_family[family] = entry
    return by_family


def _grouped(quotes, key):
    groups = {}
    for quote in quotes:
        groups.setdefault(quote[key], []).append(quote)
    return groups


def _alert(rule, snapshot, quote_fields, observed):
    return {
        "rule_name": rule["name"],
        "family": rule["family"],
        "severity": rule.get("severity", "critical"),
        "snapshot_id": snapshot["snapshot_id"],
        "ts": snapshot["ts"],
        "observed": observed,
        "snapshot": dict(quote_fields, spot=snapshot["spot"], rate=snapshot["rate"]),
    }


def _check_parity(snapshot, rule):
    tol = rule["tolerance"]
    spot, rate = snapshot["spot"], snapshot["rate"]
    for q in snapshot["quotes"]:
        expected = spot - q["strike"] * math.exp(-rate * q["maturity_years"])
        actual = q["call_mid"] - q["put_mid"]
        diff = actual - expected
        if abs(diff) > tol:
            yield _alert(
                rule, snapshot,
                {"strike": q["strike"], "maturity_days": q["maturity_days"],
                 "call_mid": q["call_mid"], "put_mid": q["put_mid"]},
                {"call_mid - put_mid": actual, "spot - strike*exp(-r*T)": expected,
                 "diff": diff, "tolerance": tol},
            )


def _check_monotonicity(snapshot, rule):
    tol = rule["tolerance"]
    for maturity_days, group in _grouped(snapshot["quotes"], "maturity_days").items():
        group = sorted(group, key=lambda q: q["strike"])
        for i in range(1, len(group)):
            lo, hi = group[i - 1], group[i]
            if hi["call_mid"] > lo["call_mid"] + tol:
                yield _alert(
                    rule, snapshot,
                    {"side": "call", "maturity_days": maturity_days,
                     "strike_lo": lo["strike"], "call_mid_lo": lo["call_mid"],
                     "strike_hi": hi["strike"], "call_mid_hi": hi["call_mid"]},
                    {"call_mid_hi - call_mid_lo": hi["call_mid"] - lo["call_mid"], "tolerance": tol},
                )
            if hi["put_mid"] < lo["put_mid"] - tol:
                yield _alert(
                    rule, snapshot,
                    {"side": "put", "maturity_days": maturity_days,
                     "strike_lo": lo["strike"], "put_mid_lo": lo["put_mid"],
                     "strike_hi": hi["strike"], "put_mid_hi": hi["put_mid"]},
                    {"put_mid_lo - put_mid_hi": lo["put_mid"] - hi["put_mid"], "tolerance": tol},
                )


def _check_butterfly(snapshot, rule):
    tol = rule["tolerance"]
    for maturity_days, group in _grouped(snapshot["quotes"], "maturity_days").items():
        group = sorted(group, key=lambda q: q["strike"])
        for i in range(1, len(group) - 1):
            k1, k2, k3 = group[i - 1], group[i], group[i + 1]
            if (k2["strike"] - k1["strike"]) != (k3["strike"] - k2["strike"]):
                continue  # only defined for equally spaced strike triples
            second_diff = k1["call_mid"] - 2 * k2["call_mid"] + k3["call_mid"]
            if second_diff < -tol:
                yield _alert(
                    rule, snapshot,
                    {"maturity_days": maturity_days,
                     "strikes": (k1["strike"], k2["strike"], k3["strike"]),
                     "call_mids": (k1["call_mid"], k2["call_mid"], k3["call_mid"])},
                    {"second_difference": second_diff, "tolerance": -tol},
                )


def _check_calendar(snapshot, rule):
    tol = rule["tolerance"]
    for strike, group in _grouped(snapshot["quotes"], "strike").items():
        group = sorted(group, key=lambda q: q["maturity_days"])
        for i in range(1, len(group)):
            short, long_ = group[i - 1], group[i]
            if long_["call_mid"] < short["call_mid"] - tol:
                yield _alert(
                    rule, snapshot,
                    {"strike": strike,
                     "maturity_short": short["maturity_days"], "call_mid_short": short["call_mid"],
                     "maturity_long": long_["maturity_days"], "call_mid_long": long_["call_mid"]},
                    {"call_mid_long - call_mid_short": long_["call_mid"] - short["call_mid"], "tolerance": tol},
                )


_CHECKS = {
    "parity": _check_parity,
    "monotonicity": _check_monotonicity,
    "butterfly": _check_butterfly,
    "calendar": _check_calendar,
}


def check_snapshot(snapshot, rules):
    """Yield one alert dict per violation this snapshot's rules find."""
    for family, rule in rules.items():
        check = _CHECKS.get(family)
        if check is None:
            raise SurfaceRuleError("no checker implemented for family {!r}".format(family))
        for alert in check(snapshot, rule):
            yield alert


def evaluate_stream(snapshots, rules=None):
    """Evaluate every snapshot in order; return the flat list of all alerts."""
    if rules is None:
        rules = load_rules()
    alerts = []
    for snapshot in snapshots:
        alerts.extend(check_snapshot(snapshot, rules))
    return alerts
