"""Evaluation of a parsed guardrail expression against one record.

Two passes run per record:

1. **Window pre-pass** -- every ``*_over`` call site in the tree feeds its
   sub-expression's value into that site's window, unconditionally.
2. **Evaluation** -- the tree is walked with short-circuiting boolean operators
   and SQL three-valued logic.

The pre-pass exists because of a real bug found while building this. Window
functions have side effects: each record has to be added to the trailing window.
But ``and``/``or`` short-circuit, so in a rule like
``output.latency_ms > 250 and mean_over(output.score, 60s) > 0.8`` the
``mean_over`` sub-expression is only reached on records where the first
condition holds. Updating windows during evaluation therefore built the average
out of a biased subset -- only the slow requests -- and the rule silently
measured the wrong thing. Separating the unconditional update from the
short-circuiting read fixes it, and keeps both properties worth having.
"""

from . import nodes
from .errors import EvaluationError
from .functions import SCALAR_FUNCTIONS, WINDOW_FUNCTIONS, call_scalar
from .values import (
    MISSING,
    UNKNOWN,
    is_nullish,
    is_number,
    logical_and,
    logical_not,
    logical_or,
    type_name,
)


class EvalContext:
    """Everything one evaluation needs beyond the AST itself."""

    __slots__ = ("record", "rule_name", "group_key", "event_ts", "windows", "trace")

    def __init__(self, record, rule_name, group_key, event_ts, windows, trace=None):
        self.record = record
        self.rule_name = rule_name
        self.group_key = group_key
        self.event_ts = event_ts
        self.windows = windows
        self.trace = trace


def evaluate(tree, context):
    """Run the window pre-pass, then evaluate ``tree``. Returns a value."""
    if context.windows is not None:
        update_windows(tree, context)
    return _eval(tree, context)


def update_windows(tree, context):
    """Feed every window call site in the tree with this record's sample."""
    for node in nodes.walk(tree):
        if not isinstance(node, nodes.WindowCall):
            continue
        value = _eval(node.expr, context)
        if is_nullish(value):
            # A null sample carries no information about the aggregate; skipping
            # it is what makes mean_over robust to intermittently missing data.
            continue
        _, expects_boolean = WINDOW_FUNCTIONS[node.name]
        if expects_boolean is True and not isinstance(value, bool):
            raise EvaluationError(
                "{}() expects a condition such as 'output.score < 0.05', but its "
                "argument produced {}".format(node.name, type_name(value)),
                node,
            )
        if expects_boolean is False and not is_number(value):
            raise EvaluationError(
                "{}() expects a number, but its argument produced {}".format(
                    node.name, type_name(value)
                ),
                node,
            )
        key = context.windows.key(context.rule_name, node.site_id, context.group_key)
        context.windows.observe(key, context.event_ts, value, node.span_seconds)


def _record(context, node, value):
    if context.trace is not None:
        context.trace[node.text()] = value
    return value


def _eval(node, context):
    if isinstance(node, nodes.Literal):
        return node.value

    if isinstance(node, nodes.Duration):
        raise EvaluationError(
            "a duration such as {}s is only meaningful as a window argument".format(
                node.seconds
            ),
            node,
        )

    if isinstance(node, nodes.ListLiteral):
        return [_eval(item, context) for item in node.items]

    if isinstance(node, nodes.FieldRef):
        return _record(context, node, _lookup(context.record, node.path))

    if isinstance(node, nodes.UnaryMinus):
        value = _eval(node.operand, context)
        if is_nullish(value):
            return UNKNOWN
        if not is_number(value):
            raise EvaluationError(
                "cannot negate {}".format(type_name(value)), node
            )
        return -value

    if isinstance(node, nodes.Arithmetic):
        return _eval_arithmetic(node, context)

    if isinstance(node, nodes.Compare):
        return _eval_compare(node, context)

    if isinstance(node, nodes.InOp):
        return _eval_in(node, context)

    if isinstance(node, nodes.NotOp):
        return logical_not(_as_condition(node.operand, context))

    if isinstance(node, nodes.LogicalOp):
        return _eval_logical(node, context)

    if isinstance(node, nodes.FuncCall):
        args = [_eval(arg, context) for arg in node.args]
        if node.name not in SCALAR_FUNCTIONS:
            raise EvaluationError("unknown function {!r}".format(node.name), node)
        return _record(context, node, call_scalar(node.name, args, node))

    if isinstance(node, nodes.WindowCall):
        return _record(context, node, _eval_window(node, context))

    raise EvaluationError("cannot evaluate node type {}".format(type(node).__name__), node)


def _lookup(record, path):
    """Walk a dotted path. Absent at any level yields MISSING, not an error."""
    current = record
    for part in path:
        if not isinstance(current, dict) or part not in current:
            return MISSING
        current = current[part]
    return current


def _eval_logical(node, context):
    """Short-circuit ``and``/``or`` under three-valued logic.

    Short-circuiting is safe only because the window pre-pass already ran; see
    the module docstring.
    """
    left = _as_condition(node.left, context)
    if node.op == "and":
        if left is False:
            return False
        return logical_and(left, _as_condition(node.right, context))
    if left is True:
        return True
    return logical_or(left, _as_condition(node.right, context))


def _as_condition(node, context):
    """Evaluate a node that must yield TRUE / FALSE / UNKNOWN."""
    value = _eval(node, context)
    if is_nullish(value):
        return UNKNOWN
    if isinstance(value, bool):
        return value
    raise EvaluationError(
        "expected a condition here, but '{}' produced {}; did you mean a "
        "comparison such as '{} > 0'?".format(node.text(), type_name(value), node.text()),
        node,
    )


def _eval_arithmetic(node, context):
    left = _eval(node.left, context)
    right = _eval(node.right, context)

    if is_nullish(left) or is_nullish(right):
        return UNKNOWN

    if node.op == "+" and isinstance(left, str) and isinstance(right, str):
        return left + right

    if not is_number(left) or not is_number(right):
        raise EvaluationError(
            "cannot apply '{}' to {} and {}".format(
                node.op, type_name(left), type_name(right)
            ),
            node,
        )

    if node.op == "+":
        return left + right
    if node.op == "-":
        return left - right
    if node.op == "*":
        return left * right

    if right == 0:
        # Data-dependent, not an authoring mistake: a denominator that happens
        # to be zero for one record should make that rule undecidable for that
        # record, not take the consumer down or flood the error counter.
        return UNKNOWN
    if node.op == "/":
        return left / right
    if node.op == "%":
        return left % right

    raise EvaluationError("unknown operator {!r}".format(node.op), node)


def _eval_compare(node, context):
    left = _eval(node.left, context)
    right = _eval(node.right, context)

    if is_nullish(left) or is_nullish(right):
        return UNKNOWN

    if node.op in ("==", "!="):
        # Equality is type-tolerant: comparing a string field to a number is a
        # mismatch, not a crash. Ordering below is strict, because "is this
        # string less than this number" has no defensible answer.
        if isinstance(left, bool) != isinstance(right, bool):
            equal = False
        elif is_number(left) and is_number(right):
            equal = left == right
        elif type(left) is type(right):
            equal = left == right
        else:
            equal = False
        return equal if node.op == "==" else not equal

    both_numbers = is_number(left) and is_number(right)
    both_strings = isinstance(left, str) and isinstance(right, str)
    if not (both_numbers or both_strings):
        raise EvaluationError(
            "cannot compare {} with {} using '{}'".format(
                type_name(left), type_name(right), node.op
            ),
            node,
        )

    if node.op == "<":
        return left < right
    if node.op == "<=":
        return left <= right
    if node.op == ">":
        return left > right
    return left >= right


def _eval_in(node, context):
    value = _eval(node.value, context)
    candidates = _eval(node.candidates, context)

    if not isinstance(candidates, list):
        raise EvaluationError(
            "'in' expects a list on the right-hand side, got {}".format(
                type_name(candidates)
            ),
            node,
        )

    if is_nullish(value):
        return UNKNOWN

    saw_null = False
    for candidate in candidates:
        if is_nullish(candidate):
            saw_null = True
            continue
        if isinstance(value, bool) != isinstance(candidate, bool):
            continue
        if is_number(value) and is_number(candidate):
            if value == candidate:
                return not node.negated
            continue
        if type(value) is type(candidate) and value == candidate:
            return not node.negated

    # SQL semantics: a non-match against a list containing null is UNKNOWN,
    # because the null might have been the missing match.
    if saw_null:
        return UNKNOWN
    return node.negated


def _eval_window(node, context):
    if context.windows is None:
        raise EvaluationError(
            "{}() needs streaming state; this expression cannot be evaluated "
            "standalone".format(node.name),
            node,
        )

    key = context.windows.key(context.rule_name, node.site_id, context.group_key)
    values = context.windows.values(key)

    if len(values) < node.min_samples:
        # Not enough history yet: undecidable rather than falsely calm. This is
        # what stops drift rules from firing on the first record after a deploy.
        return UNKNOWN

    aggregate, _ = WINDOW_FUNCTIONS[node.name]
    return aggregate(values)
