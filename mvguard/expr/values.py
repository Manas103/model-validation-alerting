"""Value semantics for the guardrail language: null, missing, and three-valued logic.

Real scoring traffic has holes in it. A feature can be *absent* from the payload
(the upstream service never sent it) or *present and explicitly null* (the
service sent it, but had no value). Those are different failures with different
owners, so the language keeps them distinct: ``MISSING`` for absent, ``None``
for explicit null. Both are "nullish" everywhere else.

Comparisons and boolean operators follow SQL's three-valued logic (TRUE /
FALSE / UNKNOWN) rather than Python's truthiness. The reason is that Python
would coerce a missing field to something definite -- ``None > 0.9`` raises,
and a naive ``bool(None)`` is ``False`` -- and either behaviour silently turns
"we don't know" into "no breach". A guardrail that quietly stops firing when
its input goes missing is worse than one that errors, so UNKNOWN propagates and
the engine counts it separately (see ``engine.py``).
"""


class _Missing:
    """Singleton marker for a field the record did not contain at all."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "MISSING"

    def __bool__(self):
        raise TypeError("MISSING has no truth value; use is_null()/is_missing()")


MISSING = _Missing()

# UNKNOWN is spelled as Python None inside the evaluator. Explicit null in a
# record payload is also None -- they are the same value, which is correct:
# "the field was null" and "the comparison was undecidable" are both UNKNOWN.
UNKNOWN = None


def is_nullish(value):
    """True for both explicit null and absent-field."""
    return value is None or value is MISSING


def is_number(value):
    """True for ints and floats, excluding bool.

    ``bool`` is a subclass of ``int`` in Python, but treating ``true`` as the
    number 1 in an ordering comparison produces nonsense like ``true > 0.5``
    quietly succeeding, so bools are kept out of the numeric tower here.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def type_name(value):
    """Human-readable type name for error messages."""
    if value is MISSING:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if is_number(value):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "list"
    return type(value).__name__


def truthiness(value):
    """Coerce an evaluated value to TRUE / FALSE / UNKNOWN.

    Only booleans are definite. Nullish is UNKNOWN. Anything else is an
    authoring error -- ``when: output.score`` (a number used as a condition) is
    almost certainly a mistake, so it is rejected rather than coerced.
    """
    if is_nullish(value):
        return UNKNOWN
    if isinstance(value, bool):
        return value
    return NotImplemented


def logical_and(left, right):
    """SQL three-valued AND. FALSE dominates UNKNOWN."""
    if left is False or right is False:
        return False
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return True


def logical_or(left, right):
    """SQL three-valued OR. TRUE dominates UNKNOWN."""
    if left is True or right is True:
        return True
    if left is UNKNOWN or right is UNKNOWN:
        return UNKNOWN
    return False


def logical_not(value):
    """SQL three-valued NOT. NOT UNKNOWN is UNKNOWN."""
    if value is UNKNOWN:
        return UNKNOWN
    return not value
