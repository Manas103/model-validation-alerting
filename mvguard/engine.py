"""The evaluation engine: run every rule against every record, route breaches.

Per record the engine produces, for each rule, one of four outcomes:

``ok``            the guardrail held
``breach``        the guardrail was violated -- an alert is routed
``suppressed``    a breach inside another breach's cooldown window
``undetermined``  the expression evaluated to UNKNOWN (missing/null input)
``error``         the expression could not be evaluated at all

``undetermined`` is tracked separately and deliberately. Folding it into "ok"
would mean a rule that stops receiving its input looks identical to a rule that
is passing, which is how guardrails rot silently.
"""

import time
from collections import defaultdict

from .expr import EvaluationError, WindowStore


class Alert:
    """A single routed breach, carrying the record that triggered it."""

    __slots__ = (
        "rule_name",
        "severity",
        "group_key",
        "event_ts",
        "detected_ts",
        "fingerprint",
        "observed",
        "snapshot",
        "suppressed_count",
        "record_key",
        "kafka_partition",
        "kafka_offset",
        "channels",
        "description",
        "expression",
    )

    def __init__(
        self,
        rule_name,
        severity,
        group_key,
        event_ts,
        detected_ts,
        fingerprint,
        observed,
        snapshot,
        suppressed_count=0,
        record_key=None,
        kafka_partition=None,
        kafka_offset=None,
        channels=(),
        description="",
        expression="",
    ):
        self.rule_name = rule_name
        self.severity = severity
        self.group_key = group_key
        self.event_ts = event_ts
        self.detected_ts = detected_ts
        self.fingerprint = fingerprint
        self.observed = observed
        self.snapshot = snapshot
        self.suppressed_count = suppressed_count
        self.record_key = record_key
        self.kafka_partition = kafka_partition
        self.kafka_offset = kafka_offset
        self.channels = tuple(channels)
        self.description = description
        self.expression = expression

    def __repr__(self):
        return "Alert({!r}, {}, group={!r})".format(
            self.rule_name, self.severity, self.group_key
        )


class RuleStats:
    __slots__ = ("ok", "breach", "suppressed", "undetermined", "error", "last_error")

    def __init__(self):
        self.ok = 0
        self.breach = 0
        self.suppressed = 0
        self.undetermined = 0
        self.error = 0
        self.last_error = None

    def as_dict(self):
        return {
            "ok": self.ok,
            "breach": self.breach,
            "suppressed": self.suppressed,
            "undetermined": self.undetermined,
            "error": self.error,
        }


class RecordMeta:
    """Where a record came from, for attaching provenance to alerts."""

    __slots__ = ("key", "partition", "offset")

    def __init__(self, key=None, partition=None, offset=None):
        self.key = key
        self.partition = partition
        self.offset = offset


EMPTY_META = RecordMeta()


class GuardrailEngine:
    def __init__(self, ruleset, window_store=None, clock=time.time):
        self.ruleset = ruleset
        self.windows = window_store if window_store is not None else WindowStore()
        self.clock = clock
        self.stats = defaultdict(RuleStats)
        self.records_seen = 0
        # (rule name, group key) -> [cooldown expiry event-time, folded count]
        self._cooldowns = {}

    def process(self, record, meta=EMPTY_META, event_ts=None):
        """Evaluate all enabled rules against one record. Returns a list of Alerts."""
        self.records_seen += 1
        if event_ts is None:
            event_ts = _record_event_ts(record, self.clock)

        alerts = []
        for rule in self.ruleset.enabled:
            stats = self.stats[rule.name]
            group_key = rule.group_key_for(record)

            try:
                result, observed = rule.expression.evaluate(
                    record, rule.name, group_key, event_ts, self.windows
                )
            except EvaluationError as exc:
                stats.error += 1
                stats.last_error = str(exc)
                continue

            if result is None:
                stats.undetermined += 1
                continue
            if result is False:
                stats.ok += 1
                continue

            alert = self._raise_breach(rule, group_key, event_ts, observed, record, meta)
            if alert is None:
                stats.suppressed += 1
            else:
                stats.breach += 1
                alerts.append(alert)

        return alerts

    def _raise_breach(self, rule, group_key, event_ts, observed, record, meta):
        """Apply the cooldown, returning an Alert or None if suppressed.

        Cooldown is measured in *event* time, matching the windows, so replaying
        a topic reproduces the same suppression decisions. A wall-clock cooldown
        would fold thousands of replayed breaches into one alert simply because
        the replay ran faster than real time.
        """
        cooldown_key = (rule.name, group_key)

        if rule.cooldown_seconds > 0:
            state = self._cooldowns.get(cooldown_key)
            if state is not None and event_ts < state[0]:
                state[1] += 1
                return None

        folded = 0
        if rule.cooldown_seconds > 0:
            previous = self._cooldowns.get(cooldown_key)
            if previous is not None:
                folded = previous[1]
            self._cooldowns[cooldown_key] = [event_ts + rule.cooldown_seconds, 0]

        return Alert(
            rule_name=rule.name,
            severity=rule.severity,
            group_key=group_key,
            event_ts=event_ts,
            detected_ts=self.clock(),
            fingerprint=rule.fingerprint,
            observed=observed,
            snapshot=record,
            suppressed_count=folded,
            record_key=meta.key,
            kafka_partition=meta.partition,
            kafka_offset=meta.offset,
            channels=rule.channels,
            description=rule.description,
            expression=rule.when,
        )

    def flush_pending_suppressions(self):
        """Report breaches still folded into an unexpired cooldown at shutdown.

        Without this, the last few suppressed breaches of a run would never be
        accounted for anywhere, and the totals would not add up.
        """
        pending = {}
        for (rule_name, group_key), state in self._cooldowns.items():
            if state[1]:
                pending[(rule_name, group_key)] = state[1]
        return pending

    def summary(self):
        return {
            "records": self.records_seen,
            "rules": {name: stat.as_dict() for name, stat in sorted(self.stats.items())},
            "windows": {
                "count": self.windows.window_count(),
                "samples": self.windows.total_samples(),
                "dropped": self.windows.dropped_samples,
                "late": self.windows.late_samples,
                "out_of_order": self.windows.out_of_order_samples,
            },
        }


def _record_event_ts(record, clock):
    """Read the record's own timestamp, falling back to now.

    Event time is what windows and cooldowns are measured in, so a record
    without a usable timestamp gets the current clock rather than zero -- which
    would otherwise place it infinitely far in the past and empty every window.
    """
    value = record.get("ts")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return clock()
