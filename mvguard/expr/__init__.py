"""The guardrail expression language: tokenizer, parser, evaluator.

Typical use::

    from mvguard.expr import Expression
    from mvguard.expr.windows import WindowStore

    rule = Expression.compile("output.score > 0.99")
    store = WindowStore()
    result, observed = rule.evaluate(record, "my_rule", None, event_ts, store)
"""

import hashlib

from . import nodes
from .errors import (
    EvaluationError,
    ExpressionError,
    ParseError,
    TokenizeError,
)
from .evaluator import EvalContext, evaluate
from .functions import ALL_FUNCTION_NAMES, SCALAR_FUNCTIONS, WINDOW_FUNCTIONS
from .parser import parse
from .values import MISSING, UNKNOWN, is_nullish, truthiness
from .windows import WindowStore

__all__ = [
    "Expression",
    "EvalContext",
    "EvaluationError",
    "ExpressionError",
    "ParseError",
    "TokenizeError",
    "WindowStore",
    "MISSING",
    "UNKNOWN",
    "ALL_FUNCTION_NAMES",
    "SCALAR_FUNCTIONS",
    "WINDOW_FUNCTIONS",
    "evaluate",
    "parse",
    "is_nullish",
    "truthiness",
]


class Expression:
    """A parsed, reusable guardrail expression."""

    __slots__ = ("source", "tree", "fingerprint")

    def __init__(self, source, tree):
        self.source = source
        self.tree = tree
        # Window state is scoped by this fingerprint as well as by rule name, so
        # editing a rule's text starts its windows fresh instead of inheriting
        # samples that were collected under the previous definition. Without it,
        # changing `mean_over(output.score, 60s)` to `rate_over(...)` would keep
        # feeding the new aggregate from the old one's history.
        self.fingerprint = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def compile(cls, source):
        """Parse ``source``. Raises TokenizeError or ParseError on bad input."""
        return cls(source, parse(source))

    @property
    def is_stateful(self):
        """True if the expression uses any windowed aggregate."""
        return any(isinstance(node, nodes.WindowCall) for node in nodes.walk(self.tree))

    @property
    def field_refs(self):
        """Every dotted field path the expression can read, sorted."""
        return sorted(
            {".".join(node.path) for node in nodes.walk(self.tree)
             if isinstance(node, nodes.FieldRef)}
        )

    @property
    def max_window_seconds(self):
        """Widest window span used, or 0.0 for a stateless expression."""
        spans = [
            node.span_seconds
            for node in nodes.walk(self.tree)
            if isinstance(node, nodes.WindowCall)
        ]
        return max(spans) if spans else 0.0

    def evaluate(self, record, rule_name, group_key, event_ts, windows, trace=True):
        """Evaluate against one record.

        Returns ``(result, observed)`` where ``result`` is True, False, or None
        (UNKNOWN), and ``observed`` maps sub-expression text to the value it
        produced -- the "why" that gets attached to an alert.
        """
        observed = {} if trace else None
        scope = "{}:{}".format(rule_name, self.fingerprint)
        context = EvalContext(record, scope, group_key, event_ts, windows, observed)
        value = evaluate(self.tree, context)
        result = truthiness(value)
        if result is NotImplemented:
            raise EvaluationError(
                "a rule must evaluate to true or false, but this one produced "
                "{!r}; did you forget a comparison?".format(value)
            )
        return result, (observed or {})

    def __repr__(self):
        return "Expression({!r})".format(self.source)
