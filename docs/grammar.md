# Guardrail expression language

The language a rule author writes in the `when:` field of `rules/guardrails.yaml`.
It is deliberately small: enough to express a guardrail, not enough to express a
program. There are no variables, no assignment, no loops, no attribute access on
arbitrary objects, and no way to reach the host process.

## Grammar

```ebnf
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
primary        = NUMBER | STRING | DURATION
               | "true" | "false" | "null"
               | list_literal | func_call | field_ref
               | "(" expression ")" ;
list_literal   = "[" [ expression { "," expression } ] "]" ;
func_call      = IDENT "(" [ expression { "," expression } ] ")" ;
field_ref      = IDENT { "." IDENT } ;
```

Precedence, loosest to tightest: `or`, `and`, `not`, comparison / `in`,
`+` `-`, `*` `/` `%`, unary `-`, primary.

Comparisons **do not chain**. `0 < output.score < 1` is a parse error with a
message telling the author to write `output.score > 0 and output.score < 1`.
Python chains this in a way that would surprise a spreadsheet user, and C-style
left-to-right evaluation would be silently wrong, so it is refused outright.

## Literals

| Kind | Examples |
|---|---|
| Number | `1`, `2.5`, `.5`, `1e3`, `2E-2` |
| String | `'retail'`, `"retail"`, with `\n \t \r \\ \' \"` escapes |
| Boolean | `true`, `false` |
| Null | `null` |
| Duration | `500ms`, `30s`, `5m`, `2h`, `1d` |
| List | `["retail", "smb"]` |

A duration is written with **no space** between the number and the unit, which
is what lets the tokenizer tell `60s` (a duration) from `60 s` (a number and a
field). Durations are only valid as a window function's second argument.

`#` starts a comment that runs to end of line.

## Field references

A dotted path into the record: `output.score`, `input.age`, `model_version`.
Reading a path the record does not contain is not an error -- it produces
`MISSING`, which behaves as null everywhere except `is_missing()`.

## Null, missing, and three-valued logic

Real scoring traffic has holes in it, and the two kinds of hole have different
owners:

- **`null`** -- the field arrived, explicitly set to null. The producer had no
  value for it.
- **`MISSING`** -- the field never arrived at all. The producer does not send it,
  usually because of a schema mismatch or a version skew.

`is_null()` is true for both. `is_missing()` is true only for the second.

Everything else follows **SQL three-valued logic**: comparisons involving a
nullish operand produce UNKNOWN rather than true or false, and UNKNOWN
propagates.

| Expression | Result |
|---|---|
| `input.income > 100` where income is null | UNKNOWN |
| `false and <unknown>` | `false` (false dominates) |
| `true and <unknown>` | UNKNOWN |
| `true or <unknown>` | `true` (true dominates) |
| `false or <unknown>` | UNKNOWN |
| `not <unknown>` | UNKNOWN |
| `null == null` | UNKNOWN (neither value is known) |
| `x in [1, null]`, no match | UNKNOWN (the null might have been the match) |
| `x / 0` | UNKNOWN |

A rule that evaluates to UNKNOWN is recorded as **undetermined** -- neither a
breach nor a pass -- and counted separately. This matters more than it sounds:
if UNKNOWN collapsed to "no breach", a guardrail whose input silently stopped
arriving would look exactly like a guardrail that was passing, which is how
monitoring rots without anyone noticing.

## Types

Ordering comparisons (`<`, `<=`, `>`, `>=`) require both operands to be numbers
or both to be strings; anything else is an evaluation error naming both types.
Equality (`==`, `!=`) is type-tolerant and returns false across mismatched types
rather than erroring, because comparing a field to a literal of the wrong type is
a mismatch, not a crash.

Booleans are deliberately **not** numbers: `true > 0` is an error, not `true`.

`+` concatenates two strings; all other arithmetic is numbers only.

A rule's top-level expression must produce a boolean. `when: output.score` is
rejected with a message suggesting a comparison.

## Scalar functions

| Function | Meaning |
|---|---|
| `abs(x)` | absolute value |
| `round(x[, digits])` | round to `digits` decimal places (default 0) |
| `floor(x)` / `ceil(x)` | round down / up |
| `is_null(x)` | true for null **or** missing |
| `is_missing(x)` | true only when the field was absent |
| `coalesce(a, b, ...)` | first non-nullish argument |
| `len(x)` | length of a string or list |
| `lower(x)` / `upper(x)` | case conversion |
| `min(a, ...)` / `max(a, ...)` | smallest / largest, ignoring nullish |

Nullish input propagates: `abs(null)` is null, not an error.

## Window functions

```
mean_over(<expression>, <duration>[, <min_samples>])
```

| Function | Aggregates |
|---|---|
| `mean_over` | arithmetic mean |
| `sum_over` | sum |
| `count_over` | number of non-null samples |
| `min_over` / `max_over` | extremes |
| `stddev_over` | population standard deviation |
| `rate_over` | fraction of samples that were **true** |

`rate_over` is the one whose argument is a condition rather than a number:
`rate_over(is_null(input.income), 60s) > 0.15` reads as "more than 15% of the
last minute's records had a null income".

The optional third argument is a minimum sample count. Below it the aggregate is
UNKNOWN rather than a number computed from too little history -- this is what
stops a drift rule from firing on the first record after a deploy.

Windows cannot be nested. Null samples are skipped rather than counted as zero.

### Window state and event time

Windows are pruned by the **event timestamp on the record**, never by wall-clock
time. Replaying a topic therefore reproduces the same alerts no matter how fast
the consumer runs.

Window state is keyed by `(rule name + expression fingerprint, call site, group
key)`:

- the **call site** keeps two `mean_over` calls in one rule independent;
- the **fingerprint** of the expression text means editing a rule starts its
  windows fresh instead of inheriting samples gathered under the old definition;
- the **group key** (`group_by:` in the rule file) keeps one model version's
  history from contaminating another's.

Event-time windowing assumes an ordered stream. Records arriving behind the
window's watermark are inserted in timestamp order if they still fall inside the
window, and counted as **late** and dropped if their window has already closed.
Both counters are reported at the end of a run -- see the ordering section of the
main README for why that matters.
