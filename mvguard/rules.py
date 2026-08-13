"""Declarative guardrail rules, loaded from YAML.

A rule file is the interface non-engineers actually touch, so loading is strict
and the errors are specific: an unknown key is an error rather than a silently
ignored typo, because a guardrail that was quietly never armed is the worst
possible failure mode for this system.
"""

import os

import yaml

from .expr import Expression, ParseError, TokenizeError
from .expr.tokenizer import DURATION_UNITS

SEVERITIES = ("info", "warning", "critical")
DEFAULT_CHANNELS = ("postgres", "console")

_ALLOWED_KEYS = {
    "name",
    "when",
    "severity",
    "description",
    "group_by",
    "cooldown",
    "channels",
    "enabled",
}


class RuleError(Exception):
    """A rule file could not be loaded."""


def parse_duration(text, field="cooldown"):
    """Accept '30s', '5m', '1h', or a bare number meaning seconds."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        if text < 0:
            raise RuleError("{} cannot be negative".format(field))
        return float(text)
    if not isinstance(text, str):
        raise RuleError("{} must be a duration such as '30s', got {!r}".format(field, text))

    stripped = text.strip()
    for unit in sorted(DURATION_UNITS, key=len, reverse=True):
        if stripped.endswith(unit):
            number = stripped[: -len(unit)].strip()
            try:
                value = float(number)
            except ValueError:
                raise RuleError(
                    "{} must be a duration such as '30s', got {!r}".format(field, text)
                )
            if value < 0:
                raise RuleError("{} cannot be negative".format(field))
            return value * DURATION_UNITS[unit]
    try:
        return float(stripped)
    except ValueError:
        raise RuleError(
            "{} must be a duration such as '30s' or '5m', got {!r}".format(field, text)
        )


class Rule:
    """One guardrail: a named condition plus how to react when it holds."""

    __slots__ = (
        "name",
        "when",
        "severity",
        "description",
        "group_by",
        "cooldown_seconds",
        "channels",
        "enabled",
        "expression",
    )

    def __init__(
        self,
        name,
        when,
        severity="warning",
        description="",
        group_by=None,
        cooldown_seconds=0.0,
        channels=DEFAULT_CHANNELS,
        enabled=True,
    ):
        self.name = name
        self.when = when
        self.severity = severity
        self.description = description
        self.group_by = group_by
        self.cooldown_seconds = cooldown_seconds
        self.channels = tuple(channels)
        self.enabled = enabled
        self.expression = Expression.compile(when)

    @property
    def fingerprint(self):
        return self.expression.fingerprint

    @property
    def is_stateful(self):
        return self.expression.is_stateful

    def group_key_for(self, record):
        """Resolve this rule's group_by path against a record.

        Returns None for an ungrouped rule. A missing or null grouping field
        collapses to the literal string '<none>' so those records still get
        evaluated together rather than being dropped.
        """
        if not self.group_by:
            return None
        current = record
        for part in self.group_by.split("."):
            if not isinstance(current, dict) or part not in current:
                return "<none>"
            current = current[part]
        if current is None:
            return "<none>"
        return str(current)

    def __repr__(self):
        return "Rule({!r}, severity={!r})".format(self.name, self.severity)


class RuleSet:
    """An ordered collection of rules loaded from one file."""

    def __init__(self, rules, source_path=None):
        self.rules = list(rules)
        self.source_path = source_path

    def __iter__(self):
        return iter(self.rules)

    def __len__(self):
        return len(self.rules)

    @property
    def enabled(self):
        return [rule for rule in self.rules if rule.enabled]

    def get(self, name):
        for rule in self.rules:
            if rule.name == name:
                return rule
        return None


def _require_str(value, field, rule_label):
    if not isinstance(value, str) or not value.strip():
        raise RuleError("{}: '{}' must be a non-empty string".format(rule_label, field))
    return value.strip()


def load_ruleset(path):
    """Load and validate a rule file. Raises RuleError with a specific message."""
    if not os.path.exists(path):
        raise RuleError("rule file not found: {}".format(path))

    with open(path, "r", encoding="utf-8") as handle:
        try:
            document = yaml.safe_load(handle)
        except yaml.YAMLError as exc:
            raise RuleError("{} is not valid YAML: {}".format(path, exc))

    if document is None:
        raise RuleError("{} is empty".format(path))
    if not isinstance(document, dict) or "rules" not in document:
        raise RuleError("{} must contain a top-level 'rules:' list".format(path))
    raw_rules = document["rules"]
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleError("{}: 'rules' must be a non-empty list".format(path))

    rules = []
    seen_names = set()

    for index, raw in enumerate(raw_rules):
        label = "rule #{}".format(index + 1)
        if not isinstance(raw, dict):
            raise RuleError("{}: each rule must be a mapping".format(label))

        unknown = set(raw) - _ALLOWED_KEYS
        if unknown:
            raise RuleError(
                "{}: unknown key(s) {}; allowed keys are {}".format(
                    label,
                    ", ".join(repr(k) for k in sorted(unknown)),
                    ", ".join(sorted(_ALLOWED_KEYS)),
                )
            )

        if "name" not in raw:
            raise RuleError("{}: missing required key 'name'".format(label))
        name = _require_str(raw["name"], "name", label)
        label = "rule {!r}".format(name)

        if name in seen_names:
            raise RuleError("duplicate rule name {!r}".format(name))
        seen_names.add(name)

        if "when" not in raw:
            raise RuleError("{}: missing required key 'when'".format(label))
        when = _require_str(raw["when"], "when", label)

        severity = str(raw.get("severity", "warning")).lower().strip()
        if severity not in SEVERITIES:
            raise RuleError(
                "{}: severity must be one of {}, got {!r}".format(
                    label, ", ".join(SEVERITIES), raw.get("severity")
                )
            )

        description = raw.get("description", "") or ""
        if not isinstance(description, str):
            raise RuleError("{}: 'description' must be a string".format(label))

        group_by = raw.get("group_by")
        if group_by is not None:
            group_by = _require_str(group_by, "group_by", label)
            for part in group_by.split("."):
                if not part.isidentifier():
                    raise RuleError(
                        "{}: group_by must be a dotted field path such as "
                        "'model_version', got {!r}".format(label, raw["group_by"])
                    )

        try:
            cooldown_seconds = parse_duration(raw.get("cooldown", 0), "cooldown")
        except RuleError as exc:
            raise RuleError("{}: {}".format(label, exc))

        channels = raw.get("channels", list(DEFAULT_CHANNELS))
        if isinstance(channels, str):
            channels = [channels]
        if not isinstance(channels, list) or not channels:
            raise RuleError("{}: 'channels' must be a non-empty list".format(label))
        channels = [_require_str(c, "channels", label) for c in channels]

        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RuleError("{}: 'enabled' must be true or false".format(label))

        try:
            rule = Rule(
                name=name,
                when=when,
                severity=severity,
                description=description,
                group_by=group_by,
                cooldown_seconds=cooldown_seconds,
                channels=channels,
                enabled=enabled,
            )
        except (ParseError, TokenizeError) as exc:
            raise RuleError("{}: could not parse 'when':\n{}".format(label, exc))

        rules.append(rule)

    return RuleSet(rules, source_path=path)
