"""Built-in functions for the guardrail expression language.

Two families:

* **Scalar functions** operate on the current record only.
* **Window functions** (``*_over``) aggregate one sub-expression across a
  trailing time window. They are declared here but evaluated specially -- see
  ``windows.py`` and the pre-pass in ``evaluator.py``.

The set is deliberately small. Every function here exists because a guardrail
needed it; there is no general-purpose standard library, because the language
is meant to be readable by someone who does not program, and because a small
surface is a small attack surface.
"""

import math

from .errors import EvaluationError
from .values import MISSING, is_nullish, is_number, type_name


def _require_number(value, func_name, arg_index):
    if not is_number(value):
        raise EvaluationError(
            "{}() expects a number for argument {}, got {}".format(
                func_name, arg_index + 1, type_name(value)
            )
        )
    return value


def _fn_abs(args):
    (value,) = args
    if is_nullish(value):
        return None
    return abs(_require_number(value, "abs", 0))


def _fn_round(args):
    value = args[0]
    digits = args[1] if len(args) > 1 else 0
    if is_nullish(value):
        return None
    _require_number(value, "round", 0)
    if not is_number(digits):
        raise EvaluationError("round() expects a number for argument 2")
    return round(value, int(digits))


def _fn_floor(args):
    (value,) = args
    if is_nullish(value):
        return None
    return math.floor(_require_number(value, "floor", 0))


def _fn_ceil(args):
    (value,) = args
    if is_nullish(value):
        return None
    return math.ceil(_require_number(value, "ceil", 0))


def _fn_is_null(args):
    """True for an absent field *or* an explicit null. Never returns UNKNOWN."""
    (value,) = args
    return is_nullish(value)


def _fn_is_missing(args):
    """True only when the field was absent from the payload entirely."""
    (value,) = args
    return value is MISSING


def _fn_coalesce(args):
    for value in args:
        if not is_nullish(value):
            return value
    return None


def _fn_len(args):
    (value,) = args
    if is_nullish(value):
        return None
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (list, tuple)):
        return len(value)
    raise EvaluationError("len() expects a string or list, got {}".format(type_name(value)))


def _fn_lower(args):
    (value,) = args
    if is_nullish(value):
        return None
    if not isinstance(value, str):
        raise EvaluationError("lower() expects a string, got {}".format(type_name(value)))
    return value.lower()


def _fn_upper(args):
    (value,) = args
    if is_nullish(value):
        return None
    if not isinstance(value, str):
        raise EvaluationError("upper() expects a string, got {}".format(type_name(value)))
    return value.upper()


def _fn_min(args):
    values = [v for v in args if not is_nullish(v)]
    if not values:
        return None
    for index, value in enumerate(values):
        _require_number(value, "min", index)
    return min(values)


def _fn_max(args):
    values = [v for v in args if not is_nullish(v)]
    if not values:
        return None
    for index, value in enumerate(values):
        _require_number(value, "max", index)
    return max(values)


# name -> (implementation, min_args, max_args)
SCALAR_FUNCTIONS = {
    "abs": (_fn_abs, 1, 1),
    "round": (_fn_round, 1, 2),
    "floor": (_fn_floor, 1, 1),
    "ceil": (_fn_ceil, 1, 1),
    "is_null": (_fn_is_null, 1, 1),
    "is_missing": (_fn_is_missing, 1, 1),
    "coalesce": (_fn_coalesce, 1, 8),
    "len": (_fn_len, 1, 1),
    "lower": (_fn_lower, 1, 1),
    "upper": (_fn_upper, 1, 1),
    "min": (_fn_min, 1, 8),
    "max": (_fn_max, 1, 8),
}


def _agg_mean(values):
    return sum(values) / len(values)


def _agg_sum(values):
    return sum(values)


def _agg_count(values):
    return len(values)


def _agg_min(values):
    return min(values)


def _agg_max(values):
    return max(values)


def _agg_stddev(values):
    """Population standard deviation. Single sample has zero spread."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))


def _agg_rate(values):
    """Fraction of window samples that were true.

    ``rate_over`` is the one aggregate whose sub-expression is a condition
    rather than a number: ``rate_over(output.score < 0.05, 60s)`` reads as
    "what share of recent scores were under 0.05".
    """
    truthy = sum(1 for v in values if v is True)
    return truthy / len(values)


# name -> (aggregate, expects_boolean_samples)
WINDOW_FUNCTIONS = {
    "mean_over": (_agg_mean, False),
    "sum_over": (_agg_sum, False),
    "count_over": (_agg_count, None),
    "min_over": (_agg_min, False),
    "max_over": (_agg_max, False),
    "stddev_over": (_agg_stddev, False),
    "rate_over": (_agg_rate, True),
}

ALL_FUNCTION_NAMES = sorted(set(SCALAR_FUNCTIONS) | set(WINDOW_FUNCTIONS))


def call_scalar(name, args, node=None):
    impl, min_args, max_args = SCALAR_FUNCTIONS[name]
    if not min_args <= len(args) <= max_args:
        expected = (
            str(min_args)
            if min_args == max_args
            else "{} to {}".format(min_args, max_args)
        )
        raise EvaluationError(
            "{}() takes {} argument(s), got {}".format(name, expected, len(args)), node
        )
    return impl(args)
