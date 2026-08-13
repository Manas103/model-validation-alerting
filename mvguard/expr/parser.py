"""Recursive-descent parser for the guardrail expression language.

Grammar (also documented in ``docs/grammar.md``)::

    expression     = or_expr ;
    or_expr        = and_expr { "or" and_expr } ;
    and_expr       = not_expr { "and" not_expr } ;
    not_expr       = "not" not_expr | comparison ;
    comparison     = additive [ comp_op additive
                              | [ "not" ] "in" primary ] ;
    comp_op        = "==" | "!=" | "<" | "<=" | ">" | ">=" ;
    additive       = multiplicative { ( "+" | "-" ) multiplicative } ;
    multiplicative = unary { ( "*" | "/" | "%" ) unary } ;
    unary          = "-" unary | primary ;
    primary        = NUMBER | STRING | DURATION | "true" | "false" | "null"
                   | list_literal | func_call | field_ref
                   | "(" expression ")" ;
    list_literal   = "[" [ expression { "," expression } ] "]" ;
    func_call      = IDENT "(" [ expression { "," expression } ] ")" ;
    field_ref      = IDENT { "." IDENT } ;

Comparisons do not chain: ``a < b < c`` is rejected with a message telling the
author to write ``a < b and b < c``. Python's chaining semantics would surprise
anyone coming from SQL or a spreadsheet, and silently accepting C-style
left-to-right semantics would be worse.
"""

from . import nodes
from .errors import ParseError
from .functions import ALL_FUNCTION_NAMES, SCALAR_FUNCTIONS, WINDOW_FUNCTIONS
from .tokenizer import DURATION, EOF, IDENT, NUMBER, OP, PUNCT, STRING, tokenize

COMPARISON_OPS = {"==", "!=", "<", "<=", ">", ">="}
ADDITIVE_OPS = {"+", "-"}
MULTIPLICATIVE_OPS = {"*", "/", "%"}
LITERAL_KEYWORDS = {"true": True, "false": False, "null": None}
RESERVED = {"and", "or", "not", "in", "true", "false", "null"}


def parse(source):
    """Parse expression source into an AST. Raises TokenizeError/ParseError."""
    return _Parser(source).parse()


class _Parser:
    def __init__(self, source):
        self.source = source
        self.tokens = tokenize(source)
        self.index = 0
        self._next_site_id = 0

    # -- token helpers -------------------------------------------------

    @property
    def current(self):
        return self.tokens[self.index]

    def peek(self, offset=0):
        position = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[position]

    def advance(self):
        token = self.tokens[self.index]
        if token.kind != EOF:
            self.index += 1
        return token

    def at_keyword(self, word, offset=0):
        token = self.peek(offset)
        return token.kind == IDENT and token.value == word

    def at_op(self, *ops):
        token = self.current
        return token.kind == OP and token.value in ops

    def at_punct(self, char):
        token = self.current
        return token.kind == PUNCT and token.value == char

    def expect_punct(self, char, context):
        if not self.at_punct(char):
            raise ParseError(
                "expected {!r} {}, got {}".format(char, context, self._describe(self.current)),
                self.source,
                self.current.pos,
                self.current.length,
            )
        return self.advance()

    @staticmethod
    def _describe(token):
        if token.kind == EOF:
            return "end of expression"
        if token.kind == STRING:
            return "string {!r}".format(token.value)
        if token.kind == DURATION:
            return "duration {}s".format(token.value)
        return "{!r}".format(token.value)

    # -- entry point ---------------------------------------------------

    def parse(self):
        if self.current.kind == EOF:
            raise ParseError("expression is empty", self.source, 0, 1)
        tree = self.parse_or()
        if self.current.kind != EOF:
            raise ParseError(
                "unexpected {} after a complete expression".format(
                    self._describe(self.current)
                ),
                self.source,
                self.current.pos,
                self.current.length,
            )
        return tree

    # -- precedence levels ---------------------------------------------

    def parse_or(self):
        left = self.parse_and()
        while self.at_keyword("or"):
            pos = self.advance().pos
            right = self.parse_and()
            left = nodes.LogicalOp("or", left, right, pos)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.at_keyword("and"):
            pos = self.advance().pos
            right = self.parse_not()
            left = nodes.LogicalOp("and", left, right, pos)
        return left

    def parse_not(self):
        if self.at_keyword("not"):
            pos = self.advance().pos
            return nodes.NotOp(self.parse_not(), pos)
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_additive()

        if self.at_keyword("in") or (self.at_keyword("not") and self.at_keyword("in", 1)):
            negated = self.at_keyword("not")
            if negated:
                self.advance()
            pos = self.advance().pos
            candidates = self.parse_primary()
            return nodes.InOp(left, candidates, negated, pos)

        if self.at_op(*COMPARISON_OPS):
            token = self.advance()
            right = self.parse_additive()
            if self.at_op(*COMPARISON_OPS):
                chained = self.current
                raise ParseError(
                    "comparisons cannot be chained; write 'a {} b and b {} c' instead".format(
                        token.value, chained.value
                    ),
                    self.source,
                    chained.pos,
                    chained.length,
                )
            return nodes.Compare(token.value, left, right, token.pos)

        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.at_op(*ADDITIVE_OPS):
            token = self.advance()
            right = self.parse_multiplicative()
            left = nodes.Arithmetic(token.value, left, right, token.pos)
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        while self.at_op(*MULTIPLICATIVE_OPS):
            token = self.advance()
            right = self.parse_unary()
            left = nodes.Arithmetic(token.value, left, right, token.pos)
        return left

    def parse_unary(self):
        if self.at_op("-"):
            token = self.advance()
            return nodes.UnaryMinus(self.parse_unary(), token.pos)
        if self.at_op("+"):
            self.advance()
            return self.parse_unary()
        return self.parse_primary()

    def parse_primary(self):
        token = self.current

        if token.kind == NUMBER:
            self.advance()
            return nodes.Literal(token.value, token.pos)

        if token.kind == STRING:
            self.advance()
            return nodes.Literal(token.value, token.pos)

        if token.kind == DURATION:
            self.advance()
            return nodes.Duration(token.value, token.pos)

        if token.kind == PUNCT and token.value == "(":
            self.advance()
            inner = self.parse_or()
            self.expect_punct(")", "to close '('")
            return inner

        if token.kind == PUNCT and token.value == "[":
            return self.parse_list_literal()

        if token.kind == IDENT:
            if token.value in LITERAL_KEYWORDS:
                self.advance()
                return nodes.Literal(LITERAL_KEYWORDS[token.value], token.pos)
            if token.value in RESERVED:
                raise ParseError(
                    "{!r} is a reserved word and cannot start a value".format(token.value),
                    self.source,
                    token.pos,
                    token.length,
                )
            if self.peek(1).kind == PUNCT and self.peek(1).value == "(":
                return self.parse_call()
            return self.parse_field_ref()

        raise ParseError(
            "expected a value, got {}".format(self._describe(token)),
            self.source,
            token.pos,
            token.length,
        )

    def parse_list_literal(self):
        open_token = self.expect_punct("[", "to open a list")
        items = []
        if not self.at_punct("]"):
            while True:
                items.append(self.parse_or())
                if self.at_punct(","):
                    self.advance()
                    continue
                break
        self.expect_punct("]", "to close a list")
        return nodes.ListLiteral(items, open_token.pos)

    def parse_field_ref(self):
        first = self.advance()
        path = [first.value]
        while self.at_punct("."):
            self.advance()
            part = self.current
            if part.kind != IDENT:
                raise ParseError(
                    "expected a field name after '.', got {}".format(self._describe(part)),
                    self.source,
                    part.pos,
                    part.length,
                )
            if part.value in RESERVED:
                raise ParseError(
                    "{!r} is a reserved word and cannot be used as a field name".format(
                        part.value
                    ),
                    self.source,
                    part.pos,
                    part.length,
                )
            self.advance()
            path.append(part.value)
        return nodes.FieldRef(path, first.pos)

    def parse_call(self):
        name_token = self.advance()
        name = name_token.value
        self.expect_punct("(", "after function name")

        args = []
        if not self.at_punct(")"):
            while True:
                args.append(self.parse_or())
                if self.at_punct(","):
                    self.advance()
                    continue
                break
        self.expect_punct(")", "to close the argument list of {}()".format(name))

        if name in WINDOW_FUNCTIONS:
            return self.build_window_call(name, args, name_token)
        if name in SCALAR_FUNCTIONS:
            return self.build_scalar_call(name, args, name_token)

        raise ParseError(
            "unknown function {!r}; available functions are: {}".format(
                name, ", ".join(ALL_FUNCTION_NAMES)
            ),
            self.source,
            name_token.pos,
            name_token.length,
        )

    def build_scalar_call(self, name, args, name_token):
        _, min_args, max_args = SCALAR_FUNCTIONS[name]
        if not min_args <= len(args) <= max_args:
            expected = (
                str(min_args)
                if min_args == max_args
                else "between {} and {}".format(min_args, max_args)
            )
            raise ParseError(
                "{}() takes {} argument(s), got {}".format(name, expected, len(args)),
                self.source,
                name_token.pos,
                name_token.length,
            )
        for arg in args:
            if isinstance(arg, nodes.Duration):
                raise ParseError(
                    "{}() does not take a duration argument".format(name),
                    self.source,
                    arg.pos,
                    1,
                )
        return nodes.FuncCall(name, args, name_token.pos)

    def build_window_call(self, name, args, name_token):
        if not 2 <= len(args) <= 3:
            raise ParseError(
                "{}() takes 2 or 3 arguments -- an expression, a window such as "
                "60s, and optionally a minimum sample count -- got {}".format(name, len(args)),
                self.source,
                name_token.pos,
                name_token.length,
            )

        expr, span = args[0], args[1]

        if not isinstance(span, nodes.Duration):
            raise ParseError(
                "{}() expects a window duration as its second argument, such as "
                "60s or 5m".format(name),
                self.source,
                span.pos,
                1,
            )
        if span.seconds <= 0:
            raise ParseError(
                "{}() window must be greater than zero".format(name),
                self.source,
                span.pos,
                1,
            )

        min_samples = 1
        if len(args) == 3:
            third = args[2]
            if not isinstance(third, nodes.Literal) or not isinstance(third.value, int):
                raise ParseError(
                    "{}() expects a whole number of samples as its third argument".format(name),
                    self.source,
                    third.pos,
                    1,
                )
            if third.value < 1:
                raise ParseError(
                    "{}() minimum sample count must be at least 1".format(name),
                    self.source,
                    third.pos,
                    1,
                )
            min_samples = third.value

        # Nested windows would need a window of windows to be well defined, and
        # there is no sensible reading of mean_over(mean_over(x, 1m), 5m) here.
        for descendant in nodes.walk(expr):
            if isinstance(descendant, nodes.WindowCall):
                raise ParseError(
                    "window functions cannot be nested inside one another",
                    self.source,
                    descendant.pos,
                    1,
                )
        if isinstance(expr, nodes.Duration):
            raise ParseError(
                "{}() expects a value expression as its first argument".format(name),
                self.source,
                expr.pos,
                1,
            )

        site_id = self._next_site_id
        self._next_site_id += 1
        return nodes.WindowCall(
            name, expr, span.seconds, min_samples, site_id, name_token.pos
        )
