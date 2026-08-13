"""Evaluator tests: three-valued logic, null/missing handling, type errors."""

import pytest

from mvguard.expr import EvaluationError, Expression, WindowStore

RECORD = {
    "model_version": "v3",
    "input": {"age": 45, "segment": "retail", "income": None},
    "output": {"score": 0.93, "label": "approve", "latency_ms": 310},
}


def ev(source, record=None, store=None, event_ts=1000.0):
    expr = Expression.compile(source)
    result, observed = expr.evaluate(
        record if record is not None else RECORD,
        "test",
        None,
        event_ts,
        store if store is not None else WindowStore(),
    )
    return result, observed


def value(source, record=None):
    return ev(source, record)[0]


class TestBasics:
    def test_comparison(self):
        assert value("output.score > 0.9") is True
        assert value("output.score > 0.99") is False

    def test_string_equality(self):
        assert value("output.label == 'approve'") is True
        assert value("output.label != 'approve'") is False

    def test_arithmetic(self):
        assert value("output.score * 100 > 90") is True
        assert value("(output.latency_ms - 10) / 2 > 100") is True

    def test_boolean_literals(self):
        assert value("true and not false") is True

    def test_in_list(self):
        assert value("input.segment in ['retail', 'smb']") is True
        assert value("input.segment in ['enterprise']") is False
        assert value("input.segment not in ['enterprise']") is True

    def test_functions(self):
        assert value("abs(output.score - 1.0) < 0.1") is True
        assert value("len(input.segment) == 6") is True
        assert value("upper(input.segment) == 'RETAIL'") is True
        assert value("coalesce(input.income, 0) == 0") is True
        assert value("min(1, 2, 3) == 1") is True
        assert value("round(output.score, 1) == 0.9") is True


class TestNullAndMissing:
    def test_explicit_null_is_null_not_missing(self):
        assert value("is_null(input.income)") is True
        assert value("is_missing(input.income)") is False

    def test_absent_field_is_both(self):
        assert value("is_null(input.nope)") is True
        assert value("is_missing(input.nope)") is True

    def test_absent_nested_path(self):
        assert value("is_missing(nowhere.at.all)") is True

    def test_comparison_with_null_is_unknown(self):
        assert value("input.income > 100") is None

    def test_arithmetic_with_null_is_unknown(self):
        assert value("input.income + 1 > 0") is None

    def test_null_equality_is_unknown_not_true(self):
        # Two nulls are not "equal" -- neither value is known.
        assert value("input.income == input.nope") is None

    def test_three_valued_and(self):
        # FALSE dominates UNKNOWN: the result is false regardless of the unknown.
        assert value("false and input.income > 1") is False
        assert value("true and input.income > 1") is None

    def test_three_valued_or(self):
        # TRUE dominates UNKNOWN.
        assert value("true or input.income > 1") is True
        assert value("false or input.income > 1") is None

    def test_not_unknown_is_unknown(self):
        assert value("not (input.income > 1)") is None

    def test_in_with_null_value_is_unknown(self):
        assert value("input.income in [1, 2]") is None

    def test_in_with_null_candidate_and_no_match_is_unknown(self):
        assert value("input.age in [1, null]") is None

    def test_in_with_null_candidate_but_match_is_true(self):
        assert value("input.age in [45, null]") is True

    def test_division_by_zero_is_unknown_not_error(self):
        assert value("output.score / 0 > 1") is None

    def test_null_propagates_through_functions(self):
        assert value("abs(input.income) > 1") is None
        assert value("upper(input.income) == 'X'") is None


class TestTypeErrors:
    def test_ordering_across_types_errors(self):
        with pytest.raises(EvaluationError) as exc:
            value("output.label > 1")
        assert "cannot compare string with number" in str(exc.value)

    def test_equality_across_types_is_false_not_error(self):
        assert value("output.label == 1") is False
        assert value("output.label != 1") is True

    def test_boolean_is_not_a_number(self):
        with pytest.raises(EvaluationError):
            value("true > 0")

    def test_arithmetic_on_string_errors(self):
        with pytest.raises(EvaluationError) as exc:
            value("output.label * 2 > 1")
        assert "cannot apply '*'" in str(exc.value)

    def test_string_concatenation_allowed(self):
        assert value("output.label + '!' == 'approve!'") is True

    def test_non_boolean_rule_result_rejected(self):
        with pytest.raises(EvaluationError) as exc:
            value("output.score")
        assert "must evaluate to true or false" in str(exc.value)

    def test_condition_position_type_error_is_specific(self):
        with pytest.raises(EvaluationError) as exc:
            value("output.score and true")
        assert "expected a condition" in str(exc.value)


class TestObservedTrace:
    def test_trace_records_field_values(self):
        _, observed = ev("output.score > 0.9 and output.label == 'approve'")
        assert observed["output.score"] == 0.93
        assert observed["output.label"] == "approve"

    def test_trace_records_function_results(self):
        _, observed = ev("abs(output.score - 0.5) > 0.4")
        assert observed["abs((output.score - 0.5))"] == pytest.approx(0.43)

    def test_short_circuit_omits_unevaluated_branch(self):
        _, observed = ev("false and output.score > 0")
        assert "output.score" not in observed


class TestWindows:
    def _feed(self, source, samples, rule="w"):
        store = WindowStore()
        expr = Expression.compile(source)
        result = observed = None
        for index, record in enumerate(samples):
            result, observed = expr.evaluate(record, rule, None, float(index), store)
        return result, observed, store

    def test_mean_over_averages_window(self):
        samples = [{"output": {"score": s}} for s in (0.2, 0.4, 0.6)]
        _, observed, _ = self._feed("mean_over(output.score, 60s) > 0", samples)
        assert observed["mean_over(output.score, 60.0s)"] == pytest.approx(0.4)

    def test_window_prunes_by_event_time(self):
        store = WindowStore()
        expr = Expression.compile("mean_over(output.score, 10s) > 0")
        # Two samples 100 simulated seconds apart: only the second survives.
        expr.evaluate({"output": {"score": 1.0}}, "w", None, 0.0, store)
        _, observed = expr.evaluate({"output": {"score": 0.0}}, "w", None, 100.0, store)
        assert observed["mean_over(output.score, 10.0s)"] == 0.0

    def test_min_samples_yields_unknown_until_met(self):
        store = WindowStore()
        expr = Expression.compile("mean_over(output.score, 60s, 3) > 0")
        results = [
            expr.evaluate({"output": {"score": 1.0}}, "w", None, float(i), store)[0]
            for i in range(4)
        ]
        assert results == [None, None, True, True]

    def test_rate_over_counts_true_fraction(self):
        samples = [{"output": {"v": v}} for v in (1, 1, 0, 0)]
        _, observed, _ = self._feed("rate_over(output.v > 0, 60s) > 0", samples)
        assert observed["rate_over((output.v > 0), 60.0s)"] == 0.5

    def test_null_samples_skipped_not_counted(self):
        samples = [
            {"output": {"score": 1.0}},
            {"output": {"score": None}},
            {"output": {"score": 3.0}},
        ]
        _, observed, _ = self._feed("mean_over(output.score, 60s) > 0", samples)
        assert observed["mean_over(output.score, 60.0s)"] == pytest.approx(2.0)

    def test_count_over(self):
        samples = [{"output": {"score": 1.0}}] * 5
        _, observed, _ = self._feed("count_over(output.score, 60s) > 0", samples)
        assert observed["count_over(output.score, 60.0s)"] == 5

    def test_stddev_over_single_sample_is_zero(self):
        samples = [{"output": {"score": 1.0}}]
        _, observed, _ = self._feed("stddev_over(output.score, 60s) >= 0", samples)
        assert observed["stddev_over(output.score, 60.0s)"] == 0.0

    def test_rate_over_rejects_non_boolean_argument(self):
        with pytest.raises(EvaluationError) as exc:
            self._feed("rate_over(output.score, 60s) > 0", [{"output": {"score": 1.0}}])
        assert "expects a condition" in str(exc.value)

    def test_mean_over_rejects_boolean_argument(self):
        with pytest.raises(EvaluationError) as exc:
            self._feed("mean_over(output.v > 0, 60s) > 0", [{"output": {"v": 1}}])
        assert "expects a number" in str(exc.value)

    def test_group_keys_keep_independent_windows(self):
        store = WindowStore()
        expr = Expression.compile("mean_over(output.score, 60s) > 0")
        expr.evaluate({"output": {"score": 10.0}}, "w", "a", 0.0, store)
        _, observed = expr.evaluate({"output": {"score": 0.0}}, "w", "b", 1.0, store)
        # Group 'b' must not see group 'a' history.
        assert observed["mean_over(output.score, 60.0s)"] == 0.0

    def test_window_update_is_not_biased_by_short_circuit(self):
        """Regression test for a real bug.

        ``and`` short-circuits, so the right-hand window used to be fed only by
        records where the left-hand side already held -- silently averaging a
        filtered subset. The window pre-pass must feed every record.
        """
        store = WindowStore()
        expr = Expression.compile("output.flag == 1 and mean_over(output.score, 600s) > 0.9")
        observed = None
        for index, (flag, score) in enumerate([(1, 1.0), (0, 0.0), (0, 0.0), (1, 1.0)]):
            _, observed = expr.evaluate(
                {"output": {"flag": flag, "score": score}}, "bias", None, float(index), store
            )
        # All four samples, not just the two where flag == 1.
        assert observed["mean_over(output.score, 600.0s)"] == pytest.approx(0.5)

    def test_out_of_order_sample_is_placed_in_time_order(self):
        store = WindowStore()
        expr = Expression.compile("mean_over(output.score, 60s) > 0")
        expr.evaluate({"output": {"score": 1.0}}, "w", None, 10.0, store)
        # Arrives late but still inside the window: must be counted, in order.
        _, observed = expr.evaluate({"output": {"score": 3.0}}, "w", None, 5.0, store)
        assert observed["mean_over(output.score, 60.0s)"] == pytest.approx(2.0)
        assert store.out_of_order_samples == 1
        assert store.late_samples == 0

    def test_sample_older_than_the_window_is_counted_as_late_and_dropped(self):
        store = WindowStore()
        expr = Expression.compile("mean_over(output.score, 10s) > 0")
        expr.evaluate({"output": {"score": 1.0}}, "w", None, 100.0, store)
        # 80 simulated seconds behind the watermark: its window already closed.
        _, observed = expr.evaluate({"output": {"score": 99.0}}, "w", None, 20.0, store)
        assert observed["mean_over(output.score, 10.0s)"] == 1.0
        assert store.late_samples == 1
        assert store.disorder_detected

    def test_watermark_does_not_move_backwards(self):
        store = WindowStore()
        expr = Expression.compile("mean_over(output.score, 10s) > 0")
        expr.evaluate({"output": {"score": 1.0}}, "w", None, 100.0, store)
        expr.evaluate({"output": {"score": 2.0}}, "w", None, 95.0, store)
        key = store.key("w:{}".format(expr.fingerprint), 0, None)
        assert store.watermark(key) == 100.0

    def test_ordered_stream_reports_no_disorder(self):
        store = WindowStore()
        expr = Expression.compile("mean_over(output.score, 60s) > 0")
        for i in range(50):
            expr.evaluate({"output": {"score": 1.0}}, "w", None, float(i), store)
        assert not store.disorder_detected

    def test_editing_expression_starts_fresh_window(self):
        """Regression test: window state must not leak across rule edits.

        Site ids restart at zero for every expression, so keying window state on
        (rule, site) alone let a rewritten rule inherit the previous rule's
        samples. The expression fingerprint is part of the key to prevent that.
        """
        store = WindowStore()
        old = Expression.compile("mean_over(output.score, 60s) > 0")
        new = Expression.compile("rate_over(output.score > 0.5, 60s) > 0.9")
        old.evaluate({"output": {"score": 1.0}}, "same_rule", None, 0.0, store)
        _, observed = new.evaluate({"output": {"score": 1.0}}, "same_rule", None, 1.0, store)
        # One boolean sample, all true -- not contaminated by the numeric 1.0.
        assert observed["rate_over((output.score > 0.5), 60.0s)"] == 1.0
