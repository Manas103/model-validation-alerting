"""Command-line interface.

    python -m mvguard.cli lint          parse + dry-run a rule file
    python -m mvguard.cli explain       show what one rule reads and how it parses
    python -m mvguard.cli init-db       create the schema, sync rule definitions
    python -m mvguard.cli produce       publish synthetic scoring traffic
    python -m mvguard.cli run           consume the topic and route breaches
    python -m mvguard.cli report        summarise what is in the alerts table

``lint`` is the command that makes this safe for non-engineers: it parses every
rule, points at the exact column of any syntax error, and then evaluates the
rules against sample traffic so an author can see what their guardrail would
actually have done before it is armed.
"""

import argparse
import json
import sys

from . import config as config_module
from . import db as db_module
from .engine import GuardrailEngine
from .expr import ExpressionError
from .producer import generate_records
from .rules import RuleError, load_ruleset


def _load(path):
    try:
        return load_ruleset(path)
    except RuleError as exc:
        print("rule file error: {}".format(exc), file=sys.stderr)
        raise SystemExit(2)


def cmd_lint(args):
    ruleset = _load(args.rules)

    print("parsed {} rule(s) from {}".format(len(ruleset), ruleset.source_path))
    for rule in ruleset:
        flags = []
        if not rule.enabled:
            flags.append("disabled")
        if rule.is_stateful:
            flags.append("windowed<={:.0f}s".format(rule.expression.max_window_seconds))
        if rule.group_by:
            flags.append("group_by={}".format(rule.group_by))
        if rule.cooldown_seconds:
            flags.append("cooldown={:.0f}s".format(rule.cooldown_seconds))
        suffix = "  [{}]".format(", ".join(flags)) if flags else ""
        print("  {:<28} {:<8} {}{}".format(rule.name, rule.severity, rule.when, suffix))

    if args.no_dry_run:
        return 0

    if args.samples:
        with open(args.samples, "r", encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle if line.strip()]
    else:
        records = list(generate_records(args.sample_count))

    engine = GuardrailEngine(ruleset)
    alerts = 0
    for record in records:
        alerts += len(engine.process(record))

    summary = engine.summary()
    print(
        "\ndry run over {} sample record(s) -- {} alert(s) would have fired".format(
            summary["records"], alerts
        )
    )
    print(
        "  {:<28} {:>8} {:>8} {:>11} {:>13} {:>7}".format(
            "rule", "breach", "held", "suppressed", "undetermined", "error"
        )
    )

    problems = []
    for name, stat in summary["rules"].items():
        print(
            "  {:<28} {:>8} {:>8} {:>11} {:>13} {:>7}".format(
                name,
                stat["breach"],
                stat["ok"],
                stat["suppressed"],
                stat["undetermined"],
                stat["error"],
            )
        )
        if stat["error"]:
            problems.append((name, engine.stats[name].last_error))

    if problems:
        print("\nrules that failed to evaluate:", file=sys.stderr)
        for name, message in problems:
            print("  {}: {}".format(name, message), file=sys.stderr)
        return 1

    never_fired = [
        name for name, stat in summary["rules"].items() if stat["breach"] == 0
    ]
    if never_fired:
        # Not a failure. A guardrail that stays quiet on healthy-ish sample
        # traffic is often exactly right -- but an author should be told, since
        # the other explanation is a rule that can never fire at all.
        print(
            "\nnote: no sample record tripped {} -- confirm that is intended".format(
                ", ".join(never_fired)
            )
        )
    return 0


def cmd_explain(args):
    ruleset = _load(args.rules)
    rule = ruleset.get(args.name)
    if rule is None:
        print("no rule named {!r}".format(args.name), file=sys.stderr)
        available = ", ".join(r.name for r in ruleset)
        print("available: {}".format(available), file=sys.stderr)
        return 2

    print("rule:        {}".format(rule.name))
    print("severity:    {}".format(rule.severity))
    print("expression:  {}".format(rule.when))
    print("parsed as:   {}".format(rule.expression.tree.text()))
    print("fingerprint: {}".format(rule.fingerprint))
    print("reads:       {}".format(", ".join(rule.expression.field_refs) or "(nothing)"))
    print("windowed:    {}".format(
        "yes, up to {:.0f}s".format(rule.expression.max_window_seconds)
        if rule.is_stateful else "no"
    ))
    print("group_by:    {}".format(rule.group_by or "(ungrouped)"))
    print("cooldown:    {:.0f}s".format(rule.cooldown_seconds))
    print("channels:    {}".format(", ".join(rule.channels)))
    if rule.description:
        print("\n{}".format(rule.description.strip()))
    return 0


def cmd_init_db(args):
    cfg = config_module.load()
    ruleset = _load(args.rules)
    conn = db_module.connect(cfg.postgres)
    try:
        db_module.init_schema(conn)
        count = db_module.sync_rules(conn, ruleset)
        print("schema ready on {}".format(cfg.postgres))
        print("synced {} rule definition(s)".format(count))
        if args.reset_alerts:
            db_module.truncate_alerts(conn)
            print("alerts table truncated")
    finally:
        conn.close()
    return 0


def cmd_produce(args):
    from .producer import publish

    cfg = config_module.load()
    print("producing {} record(s) to {}".format(args.count, cfg.kafka))
    result = publish(
        cfg.kafka,
        count=args.count,
        seed=args.seed,
        events_per_second=args.events_per_second,
    )
    print(
        "sent={sent} delivered={delivered} failed={failed}".format(**result)
    )
    return 1 if result["failed"] else 0


def cmd_run(args):
    from .service import GuardrailService
    from .sinks import ConsoleSink, FanoutSink, PostgresSink, WebhookSink

    cfg = config_module.load()
    ruleset = _load(args.rules)

    conn = db_module.connect(cfg.postgres)
    db_module.init_schema(conn)
    db_module.sync_rules(conn, ruleset)
    if args.reset_alerts:
        db_module.truncate_alerts(conn)

    channels = {
        "postgres": PostgresSink(conn),
        "console": ConsoleSink(min_severity=args.console_severity),
    }
    if cfg.webhook_url:
        channels["webhook"] = WebhookSink(cfg.webhook_url)
        print("webhook sink enabled -> {}".format(cfg.webhook_url))

    sink = FanoutSink(channels)
    engine = GuardrailEngine(ruleset)
    service = GuardrailService(engine, sink, cfg.kafka)

    print("consuming {} from {}".format(cfg.kafka.topic, cfg.kafka.bootstrap_servers))
    print("{} rule(s) armed\n".format(len(ruleset.enabled)))

    try:
        stats = service.run(max_records=args.max_records, idle_timeout=args.idle_timeout)
    finally:
        sink.close()

    summary = engine.summary()
    print("\n--- run summary ---")
    print(
        "messages={} decode_errors={} alerts={} elapsed={:.1f}s ({:.0f} msg/s)".format(
            stats.messages,
            stats.decode_errors,
            stats.alerts_routed,
            stats.elapsed,
            stats.rate,
        )
    )
    windows = summary["windows"]
    print(
        "windows={} samples={} dropped={} late={} out_of_order={}".format(
            windows["count"],
            windows["samples"],
            windows["dropped"],
            windows["late"],
            windows["out_of_order"],
        )
    )
    if windows["late"] or windows["out_of_order"]:
        # Event-time windows assume an ordered stream. Say so loudly rather than
        # reporting aggregates that were quietly computed over the wrong records.
        print(
            "\nwarning: {} late and {} out-of-order record(s) reached the windowed "
            "rules.\n  Event-time windows require an ordered stream, and Kafka only "
            "orders within\n  a partition. Use a single-partition topic, or partition "
            "by each rule's\n  group_by key, or windowed results will be computed over "
            "the wrong records.".format(windows["late"], windows["out_of_order"]),
            file=sys.stderr,
        )
    print(
        "\n{:<28} {:>8} {:>9} {:>11} {:>13} {:>7}".format(
            "rule", "alerts", "held", "suppressed", "undetermined", "error"
        )
    )
    for name, stat in summary["rules"].items():
        print(
            "{:<28} {:>8} {:>9} {:>11} {:>13} {:>7}".format(
                name,
                stat["breach"],
                stat["ok"],
                stat["suppressed"],
                stat["undetermined"],
                stat["error"],
            )
        )
        if stat["error"]:
            print("    last error: {}".format(engine.stats[name].last_error))

    pending = engine.flush_pending_suppressions()
    if pending:
        # These breaches were folded into a cooldown that had not expired when
        # the run ended, so no alert row ever carried them. Reporting them here
        # is what makes the engine's suppressed counts reconcile with the sum of
        # suppressed_count in the database.
        total_pending = sum(pending.values())
        print(
            "\n{} breach(es) were still inside an unexpired cooldown at shutdown "
            "and are\nnot reflected in any alert row's suppressed_count:".format(
                total_pending
            )
        )
        for (rule_name, group_key), count in sorted(pending.items()):
            label = rule_name if group_key is None else "{} [{}]".format(rule_name, group_key)
            print("  {:<40} {}".format(label, count))

    if sink.unknown_channels:
        print(
            "\nwarning: rules referenced unconfigured channel(s): {}".format(
                ", ".join(sorted(sink.unknown_channels))
            ),
            file=sys.stderr,
        )
    return 0


def cmd_report(args):
    cfg = config_module.load()
    conn = db_module.connect(cfg.postgres)
    try:
        rows = db_module.alert_summary(conn)
        if not rows:
            print("no alerts recorded")
            return 0

        print("{:<28} {:<9} {:>7} {:>9}  {}".format(
            "rule", "severity", "alerts", "folded", "event window"))
        total = 0
        folded_total = 0
        for rule_name, severity, count, folded, first_event, last_event in rows:
            total += count
            folded_total += int(folded)
            print(
                "{:<28} {:<9} {:>7} {:>9}  {} .. {}".format(
                    rule_name,
                    severity,
                    count,
                    folded,
                    first_event.strftime("%H:%M:%S"),
                    last_event.strftime("%H:%M:%S"),
                )
            )
        print("\n{} alert row(s), {} further breach(es) folded by cooldown".format(
            total, folded_total))

        if args.limit:
            print("\n--- {} most recent ---".format(args.limit))
            for row in db_module.recent_alerts(conn, args.limit, args.rule):
                alert_id, rule_name, severity, group_key, event_ts, observed, snapshot = row
                print(
                    "\n#{} {} [{}] group={} at {}".format(
                        alert_id, rule_name, severity, group_key,
                        event_ts.strftime("%Y-%m-%d %H:%M:%S"),
                    )
                )
                print("  observed: {}".format(json.dumps(observed, sort_keys=True)))
                print("  snapshot: {}".format(json.dumps(snapshot, sort_keys=True)))
    finally:
        conn.close()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mvguard", description="Guardrails on a live model's output."
    )
    parser.add_argument(
        "--rules", default=None, help="path to the rule file (default: rules/guardrails.yaml)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    lint = sub.add_parser("lint", help="parse and dry-run a rule file")
    lint.add_argument("--samples", help="JSONL file of sample records")
    # Matches the demo run length. A shorter horizon is misleading for the
    # windowed rules: a 60s window cannot fill with degraded-phase traffic if
    # the degraded phase itself is shorter than 60 simulated seconds, so those
    # rules look dead when they are simply never given enough history.
    lint.add_argument("--sample-count", type=int, default=12000)
    lint.add_argument("--no-dry-run", action="store_true", help="only check syntax")
    lint.set_defaults(func=cmd_lint)

    explain = sub.add_parser("explain", help="show how one rule parses")
    explain.add_argument("name")
    explain.set_defaults(func=cmd_explain)

    init = sub.add_parser("init-db", help="create schema and sync rule definitions")
    init.add_argument("--reset-alerts", action="store_true")
    init.set_defaults(func=cmd_init_db)

    produce = sub.add_parser("produce", help="publish synthetic scoring traffic")
    produce.add_argument("--count", type=int, default=12000)
    produce.add_argument("--seed", type=int, default=20240517)
    produce.add_argument("--events-per-second", type=float, default=50.0,
                         help="simulated event rate stamped on records")
    produce.set_defaults(func=cmd_produce)

    run = sub.add_parser("run", help="consume the topic and route breaches")
    run.add_argument("--max-records", type=int, default=None)
    run.add_argument("--idle-timeout", type=float, default=10.0)
    run.add_argument("--reset-alerts", action="store_true")
    run.add_argument("--console-severity", default="warning",
                     choices=["info", "warning", "critical"])
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="summarise the alerts table")
    report.add_argument("--limit", type=int, default=0, help="also show N recent alerts")
    report.add_argument("--rule", default=None, help="restrict recent alerts to one rule")
    report.set_defaults(func=cmd_report)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.rules is None:
        args.rules = config_module.load().rules_path
    try:
        return args.func(args)
    except ExpressionError as exc:
        print("expression error: {}".format(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
