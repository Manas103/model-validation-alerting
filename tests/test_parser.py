"""Tokenizer and parser tests: syntax, precedence, and error quality."""

import pytest

from mvguard.expr import Expression, ParseError, TokenizeError
from mvguard.expr.parser import parse
from mvguard.expr.tokenizer import DURATION, NUMBER, STRING, tokenize


def text_of(source):
    return parse(source).text()


class TestTokenizer:
    def test_numbers(self):
        tokens = tokenize("1 2.5 .5 1e3 2E-2")
        assert [t.value for t in tokens if t.kind == NUMBER] == [1, 2.5, 0.5, 1000.0, 0.02]

    def test_strings_both_quote_styles(self):
        tokens = tokenize("'a' \"b\"")
        assert [t.value for t in tokens if t.kind == STRING] == ["a", "b"]

    def test_string_escapes(self):
        (token,) = [t for t in tokenize(r"'a\'b\nc'") if t.kind == STRING]
        assert token.value == "a'b\nc"

    def test_durations_convert_to_seconds(self):
        tokens = tokenize("500ms 30s 5m 2h 1d")
        assert [t.value for t in tokens if t.kind == DURATION] == [
            0.5, 30.0, 300.0, 7200.0, 86400.0
        ]

    def test_unterminated_string(self):
        with pytest.raises(TokenizeError) as exc:
            tokenize("'abc")
        assert "unterminated string" in str(exc.value)

    def test_unknown_duration_unit_names_the_valid_ones(self):
        with pytest.raises(TokenizeError) as exc:
            tokenize("5min")
        message = str(exc.value)
        assert "unknown duration unit 'min'" in message
        assert "ms" in message and "s" in message

    def test_single_equals_suggests_double(self):
        with pytest.raises(TokenizeError) as exc:
            tokenize("output.score = 1")
        assert "use '=='" in str(exc.value)

    def test_ampersand_suggests_and(self):
        with pytest.raises(TokenizeError) as exc:
            tokenize("a & b")
        assert "use 'and'" in str(exc.value)

    def test_comment_is_ignored(self):
        assert text_of("output.score > 1 # trailing note") == "(output.score > 1)"


class TestPrecedence:
    def test_arithmetic_before_comparison(self):
        assert text_of("1 + 2 > 2") == "((1 + 2) > 2)"

    def test_multiplication_before_addition(self):
        assert text_of("1 + 2 * 3") == "(1 + (2 * 3))"

    def test_comparison_before_and(self):
        assert text_of("a > 1 and b < 2") == "((a > 1) and (b < 2))"

    def test_and_before_or(self):
        assert text_of("a or b and c") == "(a or (b and c))"

    def test_not_binds_tighter_than_and(self):
        assert text_of("not a and b") == "((not a) and b)"

    def test_parentheses_override(self):
        assert text_of("(1 + 2) * 3") == "((1 + 2) * 3)"
        assert text_of("not (a and b)") == "(not (a and b))"

    def test_arithmetic_is_left_associative(self):
        assert text_of("10 - 3 - 2") == "((10 - 3) - 2)"

    def test_unary_minus(self):
        assert text_of("-x + 1") == "(-x + 1)"


class TestParseErrors:
    def test_empty_expression(self):
        with pytest.raises(ParseError) as exc:
            parse("")
        assert "empty" in str(exc.value)

    def test_unclosed_paren(self):
        with pytest.raises(ParseError) as exc:
            parse("(a > 1")
        assert "expected ')'" in str(exc.value)

    def test_error_reports_column_and_caret(self):
        with pytest.raises(ParseError) as exc:
            parse("output.score > ")
        message = str(exc.value)
        assert "column 16" in message
        assert "^" in message
        # The caret line must reproduce the source so the author sees context.
        assert "output.score > " in message

    def test_chained_comparison_is_rejected_with_guidance(self):
        with pytest.raises(ParseError) as exc:
            parse("0 < output.score < 1")
        assert "cannot be chained" in str(exc.value)
        assert "and" in str(exc.value)

    def test_trailing_garbage(self):
        with pytest.raises(ParseError) as exc:
            parse("a > 1 b")
        assert "after a complete expression" in str(exc.value)

    def test_unknown_function_lists_available(self):
        with pytest.raises(ParseError) as exc:
            parse("median(output.score) > 1")
        message = str(exc.value)
        assert "unknown function 'median'" in message
        assert "mean_over" in message

    def test_reserved_word_as_field(self):
        with pytest.raises(ParseError) as exc:
            parse("input.and > 1")
        assert "reserved word" in str(exc.value)

    def test_scalar_arity_checked_at_parse_time(self):
        with pytest.raises(ParseError) as exc:
            parse("abs(1, 2) > 0")
        assert "abs() takes 1 argument" in str(exc.value)

    def test_dot_without_field_name(self):
        with pytest.raises(ParseError) as exc:
            parse("input. > 1")
        assert "expected a field name after '.'" in str(exc.value)


class TestWindowParsing:
    def test_window_requires_duration_second_arg(self):
        with pytest.raises(ParseError) as exc:
            parse("mean_over(output.score, 60) > 1")
        assert "expects a window duration" in str(exc.value)

    def test_window_arity(self):
        with pytest.raises(ParseError) as exc:
            parse("mean_over(output.score) > 1")
        assert "takes 2 or 3 arguments" in str(exc.value)

    def test_window_min_samples_must_be_whole_number(self):
        with pytest.raises(ParseError) as exc:
            parse("mean_over(output.score, 60s, 1.5) > 1")
        assert "whole number of samples" in str(exc.value)

    def test_nested_windows_rejected(self):
        with pytest.raises(ParseError) as exc:
            parse("mean_over(mean_over(x, 1m), 5m) > 1")
        assert "cannot be nested" in str(exc.value)

    def test_zero_window_rejected(self):
        with pytest.raises(ParseError) as exc:
            parse("mean_over(x, 0s) > 1")
        assert "greater than zero" in str(exc.value)

    def test_distinct_call_sites_get_distinct_ids(self):
        tree = parse("mean_over(a, 1m) > max_over(b, 1m)")
        from mvguard.expr import nodes

        sites = [n.site_id for n in nodes.walk(tree) if isinstance(n, nodes.WindowCall)]
        assert sorted(sites) == [0, 1]


class TestExpressionMetadata:
    def test_field_refs_collected(self):
        expr = Expression.compile("output.score > input.age and output.score < 1")
        assert expr.field_refs == ["input.age", "output.score"]

    def test_is_stateful(self):
        assert Expression.compile("mean_over(a, 1m) > 1").is_stateful
        assert not Expression.compile("a > 1").is_stateful

    def test_max_window_seconds(self):
        expr = Expression.compile("mean_over(a, 30s) > 1 or max_over(b, 5m) > 2")
        assert expr.max_window_seconds == 300.0

    def test_fingerprint_changes_with_source(self):
        assert (
            Expression.compile("a > 1").fingerprint
            != Expression.compile("a > 2").fingerprint
        )
