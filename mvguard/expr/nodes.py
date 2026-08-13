"""AST node types for the guardrail expression language.

Nodes are plain classes with a ``pos`` offset into the original source so that
runtime errors can point back at the sub-expression that caused them, and a
``text()`` method so alert payloads can name the sub-expression that produced
an observed value.
"""


class Node:
    __slots__ = ("pos",)

    def text(self):
        raise NotImplementedError

    def __repr__(self):
        return "{}({})".format(type(self).__name__, self.text())


class Literal(Node):
    __slots__ = ("value",)

    def __init__(self, value, pos):
        self.value = value
        self.pos = pos

    def text(self):
        if self.value is None:
            return "null"
        if isinstance(self.value, bool):
            return "true" if self.value else "false"
        if isinstance(self.value, str):
            return repr(self.value)
        return str(self.value)


class Duration(Node):
    """A time span literal such as ``60s``. Only valid as a window argument."""

    __slots__ = ("seconds",)

    def __init__(self, seconds, pos):
        self.seconds = seconds
        self.pos = pos

    def text(self):
        return "{}s".format(self.seconds)


class ListLiteral(Node):
    __slots__ = ("items",)

    def __init__(self, items, pos):
        self.items = items
        self.pos = pos

    def text(self):
        return "[{}]".format(", ".join(item.text() for item in self.items))


class FieldRef(Node):
    """A dotted path into the record, e.g. ``output.score``."""

    __slots__ = ("path",)

    def __init__(self, path, pos):
        self.path = tuple(path)
        self.pos = pos

    def text(self):
        return ".".join(self.path)


class UnaryMinus(Node):
    __slots__ = ("operand",)

    def __init__(self, operand, pos):
        self.operand = operand
        self.pos = pos

    def text(self):
        return "-{}".format(self.operand.text())


class Arithmetic(Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, op, left, right, pos):
        self.op = op
        self.left = left
        self.right = right
        self.pos = pos

    def text(self):
        return "({} {} {})".format(self.left.text(), self.op, self.right.text())


class Compare(Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, op, left, right, pos):
        self.op = op
        self.left = left
        self.right = right
        self.pos = pos

    def text(self):
        return "({} {} {})".format(self.left.text(), self.op, self.right.text())


class InOp(Node):
    __slots__ = ("value", "candidates", "negated")

    def __init__(self, value, candidates, negated, pos):
        self.value = value
        self.candidates = candidates
        self.negated = negated
        self.pos = pos

    def text(self):
        return "({} {} {})".format(
            self.value.text(), "not in" if self.negated else "in", self.candidates.text()
        )


class LogicalOp(Node):
    __slots__ = ("op", "left", "right")

    def __init__(self, op, left, right, pos):
        self.op = op
        self.left = left
        self.right = right
        self.pos = pos

    def text(self):
        return "({} {} {})".format(self.left.text(), self.op, self.right.text())


class NotOp(Node):
    __slots__ = ("operand",)

    def __init__(self, operand, pos):
        self.operand = operand
        self.pos = pos

    def text(self):
        return "(not {})".format(self.operand.text())


class FuncCall(Node):
    """A scalar function call such as ``abs(x)`` or ``is_null(x)``."""

    __slots__ = ("name", "args")

    def __init__(self, name, args, pos):
        self.name = name
        self.args = args
        self.pos = pos

    def text(self):
        return "{}({})".format(self.name, ", ".join(a.text() for a in self.args))


class WindowCall(Node):
    """A time-windowed aggregate such as ``mean_over(output.score, 60s)``.

    ``site_id`` is assigned at parse time and uniquely identifies this call
    within its rule. Window state is keyed by (rule, site_id, group key), so two
    different ``mean_over`` calls in the same rule keep independent history.
    """

    __slots__ = ("name", "expr", "span_seconds", "min_samples", "site_id")

    def __init__(self, name, expr, span_seconds, min_samples, site_id, pos):
        self.name = name
        self.expr = expr
        self.span_seconds = span_seconds
        self.min_samples = min_samples
        self.site_id = site_id
        self.pos = pos

    def text(self):
        parts = [self.expr.text(), "{}s".format(self.span_seconds)]
        if self.min_samples > 1:
            parts.append(str(self.min_samples))
        return "{}({})".format(self.name, ", ".join(parts))


def walk(node):
    """Yield every node in the tree, parents before children."""
    yield node
    for child in children(node):
        for descendant in walk(child):
            yield descendant


def children(node):
    """Direct child nodes of ``node``."""
    if isinstance(node, (Literal, Duration, FieldRef)):
        return ()
    if isinstance(node, ListLiteral):
        return tuple(node.items)
    if isinstance(node, UnaryMinus):
        return (node.operand,)
    if isinstance(node, (Arithmetic, Compare, LogicalOp)):
        return (node.left, node.right)
    if isinstance(node, InOp):
        return (node.value, node.candidates)
    if isinstance(node, NotOp):
        return (node.operand,)
    if isinstance(node, FuncCall):
        return tuple(node.args)
    if isinstance(node, WindowCall):
        return (node.expr,)
    raise TypeError("unknown node type {}".format(type(node).__name__))
