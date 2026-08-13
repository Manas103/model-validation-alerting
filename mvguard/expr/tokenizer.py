"""Tokenizer for the guardrail expression language."""

from .errors import TokenizeError

# Token kinds
NUMBER = "NUMBER"
STRING = "STRING"
IDENT = "IDENT"
DURATION = "DURATION"
OP = "OP"
PUNCT = "PUNCT"
EOF = "EOF"

KEYWORDS = {"and", "or", "not", "in", "true", "false", "null"}

# Longest-match-first so "<=" wins over "<".
OPERATORS = ("==", "!=", "<=", ">=", "<", ">", "+", "-", "*", "/", "%")
PUNCTUATION = ("(", ")", "[", "]", ",", ".")

# Duration suffixes, in seconds. Written attached to the number ("60s", "5m")
# so the lexer can tell a duration from a bare number followed by a field.
DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_IDENT_CHARS = _IDENT_START | set("0123456789")
_DIGITS = set("0123456789")


class Token:
    __slots__ = ("kind", "value", "pos", "end")

    def __init__(self, kind, value, pos, end):
        self.kind = kind
        self.value = value
        self.pos = pos
        self.end = end

    @property
    def length(self):
        return max(1, self.end - self.pos)

    def __repr__(self):
        return "Token({}, {!r}, {})".format(self.kind, self.value, self.pos)


def tokenize(source):
    """Split expression source into a list of tokens, ending with EOF."""
    tokens = []
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]

        if ch in " \t\r\n":
            i += 1
            continue

        # Comments let rule files carry an inline note next to the expression.
        if ch == "#":
            while i < n and source[i] != "\n":
                i += 1
            continue

        if ch in _DIGITS or (ch == "." and i + 1 < n and source[i + 1] in _DIGITS):
            start = i
            seen_dot = False
            while i < n and (source[i] in _DIGITS or (source[i] == "." and not seen_dot)):
                if source[i] == ".":
                    # A dot only belongs to the number if a digit follows it;
                    # otherwise it is field access on a numeric-looking token.
                    if i + 1 >= n or source[i + 1] not in _DIGITS:
                        break
                    seen_dot = True
                i += 1
            # Scientific notation: 1e-3, 2E5
            if i < n and source[i] in "eE":
                probe = i + 1
                if probe < n and source[probe] in "+-":
                    probe += 1
                if probe < n and source[probe] in _DIGITS:
                    i = probe
                    while i < n and source[i] in _DIGITS:
                        i += 1
                    seen_dot = True
            number_text = source[start:i]

            # A letter run immediately after the number is a duration unit.
            if i < n and source[i] in _IDENT_START:
                unit_start = i
                while i < n and source[i] in _IDENT_CHARS:
                    i += 1
                unit = source[unit_start:i]
                if unit not in DURATION_UNITS:
                    raise TokenizeError(
                        "unknown duration unit {!r}; expected one of {}".format(
                            unit, ", ".join(sorted(DURATION_UNITS))
                        ),
                        source,
                        unit_start,
                        len(unit),
                    )
                seconds = float(number_text) * DURATION_UNITS[unit]
                tokens.append(Token(DURATION, seconds, start, i))
                continue

            value = float(number_text) if seen_dot else int(number_text)
            tokens.append(Token(NUMBER, value, start, i))
            continue

        if ch in ("'", '"'):
            quote = ch
            start = i
            i += 1
            chunks = []
            while True:
                if i >= n:
                    raise TokenizeError("unterminated string literal", source, start, 1)
                c = source[i]
                if c == "\\":
                    if i + 1 >= n:
                        raise TokenizeError("unterminated escape sequence", source, i, 1)
                    nxt = source[i + 1]
                    mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
                    if nxt not in mapping:
                        raise TokenizeError(
                            "unsupported escape sequence '\\{}'".format(nxt), source, i, 2
                        )
                    chunks.append(mapping[nxt])
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                chunks.append(c)
                i += 1
            tokens.append(Token(STRING, "".join(chunks), start, i))
            continue

        if ch in _IDENT_START:
            start = i
            while i < n and source[i] in _IDENT_CHARS:
                i += 1
            tokens.append(Token(IDENT, source[start:i], start, i))
            continue

        matched = None
        for op in OPERATORS:
            if source.startswith(op, i):
                matched = op
                break
        if matched:
            tokens.append(Token(OP, matched, i, i + len(matched)))
            i += len(matched)
            continue

        if ch in PUNCTUATION:
            tokens.append(Token(PUNCT, ch, i, i + 1))
            i += 1
            continue

        if ch == "=":
            raise TokenizeError(
                "unexpected '='; use '==' to compare for equality", source, i, 1
            )
        if ch in "&|":
            word = "and" if ch == "&" else "or"
            raise TokenizeError(
                "unexpected {!r}; use '{}' instead".format(ch, word), source, i, 1
            )

        raise TokenizeError("unexpected character {!r}".format(ch), source, i, 1)

    tokens.append(Token(EOF, None, len(source), len(source)))
    return tokens
