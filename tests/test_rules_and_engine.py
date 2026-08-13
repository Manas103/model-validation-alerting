"""Rule-file loading, cooldown/dedup behaviour, and engine outcome accounting."""

import textwrap

import pytest

from mvguard.engine import GuardrailEngine
from mvguard.rules import Rule, RuleError, RuleSet, load_ruleset, parse_duration


def write_rules(tmp_path, body):
    path = tmp_path / "rules.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


class TestDurationParsing:
    @pytest.mark.parametrize(
        "text,expected",
        [("30s", 30.0), ("5m", 300.0), ("1h", 3600.0), ("500ms", 0.5), ("45", 45.0), (12, 12.0)],
    )
    def test_valid(self, text, expected):
        assert parse_duration(text) == expected

    def test_negative_rejected(self):
        with pytest.raises(RuleError):
            parse_duration("-5s")

    def test_garbage_rejected(self):
        with pytest.raises(RuleError):
            parse_duration("soon")


class TestRuleFileLoading:
    def test_minimal_file(self, tmp_path):
        path = write_rules(tmp_path, """
            rules:
              - name: too_high
                when: output.score > 0.9
        """)
        ruleset = load_ruleset(path)
        assert len(ruleset) == 1
        assert ruleset.get("too_high").severity == "warning"

    def test_unknown_key_is_an_error(self, tmp_path):
        # A typo'd key must not silently disarm part of a guardrail.
        path = write_rules(tmp_path, """
            rules:
              - name: r
                when: output.score > 1
                severty: critical
        """)
        with pytest.raises(RuleError) as exc:
            load_ruleset(path)
        assert "unknown key" in str(exc.value)

    def test_bad_severity(self, tmp_path):
        path = write_rules(tmp_path, """
            rules:
              - name: r
                when: output.score > 1
                severity: apocalyptic
        """)
        with pytest.raises(RuleError) as exc:
            load_ruleset(path)
        assert "severity must be one of" in str(exc.value)

    def test_duplicate_names_rejected(self, tmp_path):
        path = write_rules(tmp_path, """
            rules:
              - name: r
                when: output.score > 1
              - name: r
                when: output.score > 2
        """)
        with pytest.raises(RuleError) as exc:
            load_ruleset(path)
        assert "duplicate rule name" in str(exc.value)

    def test_syntax_error_names_the_rule_and_column(self, tmp_path):
        path = write_rules(tmp_path, """
            rules:
              - name: broken
                when: output.score >
        """)
        with pytest.raises(RuleError) as exc:
            load_ruleset(path)
        message = str(exc.value)
        assert "rule 'broken'" in message
        assert "column" in message

    def test_missing_when(self, tmp_path):
        path = write_rules(tmp_path, """
            rules:
              - name: r
        """)
        with pytest.raises(RuleError) as exc:
            load_ruleset(path)
        assert "missing required key 'when'" in str(exc.value)

    def test_missing_file(self):
        with pytest.raises(RuleError) as exc:
            load_ruleset("/nonexistent/rules.yaml")
        assert "not found" in str(exc.value)

    def test_real_project_ruleset_loads(self):
        ruleset = load_ruleset("rules/guardrails.yaml")
        assert len(ruleset) >= 10
        assert all(rule.expression is not None for rule in ruleset)


class TestGroupKey:
    def test_ungrouped_is_none(self):
        rule = Rule("r", "output.score > 1")
        assert rule.group_key_for({"model_version": "v1"}) is None

    def test_grouped_reads_dotted_path(self):
        rule = Rule("r", "output.score > 1", group_by="input.segment")
        assert rule.group_key_for({"input": {"segment": "smb"}}) == "smb"

    def test_missing_group_field_collapses(self):
        rule = Rule("r", "output.score > 1", group_by="model_version")
        assert rule.group_key_for({}) == "<none>"
        assert rule.group_key_for({"model_version": None}) == "<none>"


class TestEngineOutcomes:
    def _engine(self, rules):
        return GuardrailEngine(RuleSet(rules), clock=lambda: 0.0)

    def test_breach_produces_alert_with_snapshot(self):
        engine = self._engine([Rule("high", "output.score > 0.9", severity="critical")])
        record = {"ts": 10.0, "input": {"age": 30}, "output": {"score": 0.95}}
        (alert,) = engine.process(record)
        assert alert.rule_name == "high"
        assert alert.severity == "critical"
        # The full triggering record travels with the alert, inputs included.
        assert alert.snapshot == record
        assert alert.observed["output.score"] == 0.95
        assert alert.event_ts == 10.0

    def test_holding_rule_produces_no_alert(self):
        engine = self._engine([Rule("high", "output.score > 0.9")])
        assert engine.process({"ts": 1.0, "output": {"score": 0.1}}) == []
        assert engine.stats["high"].ok == 1

    def test_unknown_counted_separately_from_ok(self):
        engine = self._engine([Rule("high", "output.score > 0.9")])
        engine.process({"ts": 1.0, "output": {"score": None}})
        assert engine.stats["high"].undetermined == 1
        assert engine.stats["high"].ok == 0
        assert engine.stats["high"].breach == 0

    def test_evaluation_error_counted_not_raised(self):
        engine = self._engine([Rule("bad", "output.label > 1")])
        assert engine.process({"ts": 1.0, "output": {"label": "x"}}) == []
        assert engine.stats["bad"].error == 1
        assert "cannot compare" in engine.stats["bad"].last_error

    def test_one_bad_rule_does_not_stop_the_others(self):
        engine = self._engine([
            Rule("bad", "output.label > 1"),
            Rule("good", "output.score > 0.5"),
        ])
        alerts = engine.process({"ts": 1.0, "output": {"label": "x", "score": 0.9}})
        assert [a.rule_name for a in alerts] == ["good"]


class TestCooldown:
    def _engine(self, cooldown, group_by=None):
        rule = Rule("r", "output.score > 0.5", cooldown_seconds=cooldown, group_by=group_by)
        return GuardrailEngine(RuleSet([rule]), clock=lambda: 0.0)

    def test_no_cooldown_alerts_every_time(self):
        engine = self._engine(0)
        fired = sum(len(engine.process({"ts": float(i), "output": {"score": 1.0}}))
                    for i in range(5))
        assert fired == 5

    def test_cooldown_suppresses_within_window(self):
        engine = self._engine(10.0)
        fired = sum(len(engine.process({"ts": float(i), "output": {"score": 1.0}}))
                    for i in range(5))
        assert fired == 1
        assert engine.stats["r"].suppressed == 4

    def test_cooldown_expires_by_event_time(self):
        engine = self._engine(10.0)
        alerts = []
        for ts in (0.0, 5.0, 11.0, 30.0):
            alerts.extend(engine.process({"ts": ts, "output": {"score": 1.0}}))
        assert [a.event_ts for a in alerts] == [0.0, 11.0, 30.0]

    def test_suppressed_count_folded_into_next_alert(self):
        engine = self._engine(10.0)
        alerts = []
        for ts in (0.0, 1.0, 2.0, 3.0, 20.0):
            alerts.extend(engine.process({"ts": ts, "output": {"score": 1.0}}))
        assert len(alerts) == 2
        assert alerts[0].suppressed_count == 0
        # The three breaches at ts 1..3 are accounted for on the next alert.
        assert alerts[1].suppressed_count == 3

    def test_cooldown_is_per_group(self):
        engine = self._engine(10.0, group_by="model_version")
        alerts = []
        for version in ("a", "b", "a"):
            alerts.extend(engine.process(
                {"ts": 1.0, "model_version": version, "output": {"score": 1.0}}
            ))
        # a and b each alert once; a's second breach is suppressed.
        assert [a.group_key for a in alerts] == ["a", "b"]

    def test_flush_reports_unaccounted_suppressions(self):
        engine = self._engine(100.0)
        for ts in (0.0, 1.0, 2.0):
            engine.process({"ts": ts, "output": {"score": 1.0}})
        assert engine.flush_pending_suppressions() == {("r", None): 2}


class TestDeterminism:
    def test_same_seed_same_alerts(self):
        from mvguard.producer import generate_records

        def run():
            ruleset = load_ruleset("rules/guardrails.yaml")
            engine = GuardrailEngine(ruleset, clock=lambda: 0.0)
            fired = []
            for record in generate_records(3000, seed=99):
                for alert in engine.process(record):
                    fired.append((alert.rule_name, alert.event_ts, alert.group_key))
            return fired

        first, second = run(), run()
        assert first == second
        assert first, "expected the sample traffic to trip at least one guardrail"
