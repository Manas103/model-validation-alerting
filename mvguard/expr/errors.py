"""Error types for the guardrail expression language.

Rule authors are analysts and risk owners, not engineers, so a failed parse has
to say *where* it failed and *what was expected*, not just raise. Every error
carries the source text and a character offset so ``__str__`` can render a
caret pointing at the offending token.
"""


class ExpressionError(Exception):
    """Base class for anything wrong with a rule expression."""


class _PositionalError(ExpressionError):
    def __init__(self, message, source, pos, length=1):
        self.message = message
        self.source = source
        self.pos = max(0, min(pos, len(source)))
        self.length = max(1, length)
        super().__init__(message)

    def caret_line(self):
        """Render the source with a caret underlining the offending span."""
        # Expressions are single-line in practice (one YAML scalar), but be
        # correct anyway: locate the line containing self.pos.
        line_start = self.source.rfind("\n", 0, self.pos) + 1
        line_end = self.source.find("\n", self.pos)
        if line_end == -1:
            line_end = len(self.source)
        line = self.source[line_start:line_end]
        col = self.pos - line_start
        width = min(self.length, max(1, line_end - self.pos))
        return "{}\n{}{}".format(line, " " * col, "^" * width)

    def __str__(self):
        return "{} (column {})\n{}".format(self.message, self.pos + 1, self.caret_line())


class TokenizeError(_PositionalError):
    """The raw text could not be split into tokens."""


class ParseError(_PositionalError):
    """The tokens did not form a valid expression."""


class EvaluationError(ExpressionError):
    """The expression parsed but could not be evaluated against a record.

    Raised for genuine authoring mistakes that only surface at runtime -- an
    ordering comparison between a number and a string, an unknown function, a
    bad argument count. Missing or null data is *not* an evaluation error; it
    flows through the null semantics in ``values.py`` instead.
    """

    def __init__(self, message, node=None):
        self.node = node
        super().__init__(message)
