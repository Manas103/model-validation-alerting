"""PostgreSQL access: schema init, rule sync, alert persistence."""

import datetime
import json
import os

import psycopg2
import psycopg2.extras

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sql", "schema.sql")


def connect(pg_config):
    conn = psycopg2.connect(**pg_config.dsn_kwargs())
    conn.autocommit = False
    return conn


def init_schema(conn, schema_path=SCHEMA_PATH):
    with open(schema_path, "r", encoding="utf-8") as handle:
        ddl = handle.read()
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def sync_rules(conn, ruleset):
    """Upsert the rule definitions so alerts have something to reference.

    Rules removed from the file are left in place rather than deleted: the
    alerts table references them, and deleting would cascade away the history of
    breaches that a since-retired guardrail caught.
    """
    rows = [
        (
            rule.name,
            rule.when,
            rule.severity,
            rule.description,
            rule.group_by,
            rule.cooldown_seconds,
            rule.fingerprint,
        )
        for rule in ruleset
    ]
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            INSERT INTO rules (name, expression, severity, description,
                               group_by, cooldown_secs, fingerprint, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (name) DO UPDATE SET
                expression    = EXCLUDED.expression,
                severity      = EXCLUDED.severity,
                description   = EXCLUDED.description,
                group_by      = EXCLUDED.group_by,
                cooldown_secs = EXCLUDED.cooldown_secs,
                fingerprint   = EXCLUDED.fingerprint,
                updated_at    = now()
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def _to_timestamp(epoch_seconds):
    return datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.timezone.utc)


def _jsonable(value):
    """Make evaluator values JSON-safe.

    The observed map can contain the MISSING sentinel, which json cannot encode.
    It is rendered as the string '<missing>' so a reader can tell "the field was
    absent" apart from "the field was null" (which stays a JSON null).
    """
    from .expr import MISSING

    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is MISSING:
        return "<missing>"
    return value


def insert_alerts(conn, alerts):
    """Persist a batch of alerts in one transaction. Returns the row count."""
    if not alerts:
        return 0

    rows = [
        (
            alert.rule_name,
            alert.severity,
            alert.group_key,
            _to_timestamp(alert.event_ts),
            _to_timestamp(alert.detected_ts),
            alert.fingerprint,
            json.dumps(_jsonable(alert.observed)),
            json.dumps(_jsonable(alert.snapshot)),
            alert.suppressed_count,
            alert.record_key,
            alert.kafka_partition,
            alert.kafka_offset,
        )
        for alert in alerts
    ]

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            INSERT INTO alerts (rule_name, severity, group_key, event_ts,
                                detected_ts, fingerprint, observed, snapshot,
                                suppressed_count, record_key, kafka_partition,
                                kafka_offset)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s)
            """,
            rows,
        )
    conn.commit()
    return len(rows)


def alert_summary(conn):
    """Breach counts per rule, for the report command."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.rule_name,
                   a.severity,
                   count(*)                        AS alerts,
                   coalesce(sum(a.suppressed_count), 0) AS folded,
                   min(a.event_ts)                 AS first_event,
                   max(a.event_ts)                 AS last_event
            FROM alerts a
            GROUP BY a.rule_name, a.severity
            ORDER BY count(*) DESC, a.rule_name
            """
        )
        return cur.fetchall()


def recent_alerts(conn, limit=10, rule_name=None):
    with conn.cursor() as cur:
        if rule_name:
            cur.execute(
                """
                SELECT id, rule_name, severity, group_key, event_ts, observed, snapshot
                FROM alerts WHERE rule_name = %s
                ORDER BY event_ts DESC LIMIT %s
                """,
                (rule_name, limit),
            )
        else:
            cur.execute(
                """
                SELECT id, rule_name, severity, group_key, event_ts, observed, snapshot
                FROM alerts ORDER BY event_ts DESC LIMIT %s
                """,
                (limit,),
            )
        return cur.fetchall()


def truncate_alerts(conn):
    with conn.cursor() as cur:
        cur.execute("TRUNCATE alerts")
    conn.commit()
