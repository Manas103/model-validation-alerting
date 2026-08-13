"""End-to-end test against a live Kafka broker and a live PostgreSQL database.

Skipped automatically when either is unreachable, so the unit suite still runs
on a machine with no infrastructure. Nothing here is mocked: real messages go
through a real broker, and the assertions read back real rows.
"""

import json
import time
import uuid

import pytest

from mvguard import config as config_module
from mvguard import db as db_module
from mvguard.engine import GuardrailEngine
from mvguard.producer import generate_records
from mvguard.rules import Rule, RuleSet
from mvguard.service import GuardrailService
from mvguard.sinks import FanoutSink, PostgresSink

TEST_TOPIC_PREFIX = "mvguard-e2e-"


def _kafka_available(cfg):
    try:
        from confluent_kafka.admin import AdminClient

        admin = AdminClient({"bootstrap.servers": cfg.kafka.bootstrap_servers})
        metadata = admin.list_topics(timeout=5)
        return metadata is not None
    except Exception:
        return False


def _postgres_available(cfg):
    try:
        conn = db_module.connect(cfg.postgres)
        conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def cfg():
    return config_module.load()


@pytest.fixture(scope="module")
def infra(cfg):
    if not _kafka_available(cfg):
        pytest.skip("kafka broker not reachable at {}".format(cfg.kafka.bootstrap_servers))
    if not _postgres_available(cfg):
        pytest.skip("postgres not reachable at {}".format(cfg.postgres.host))
    return cfg


@pytest.fixture
def topic(infra):
    """A throwaway topic per test, deleted afterwards."""
    from confluent_kafka.admin import AdminClient, NewTopic

    name = TEST_TOPIC_PREFIX + uuid.uuid4().hex[:8]
    admin = AdminClient({"bootstrap.servers": infra.kafka.bootstrap_servers})
    futures = admin.create_topics([NewTopic(name, num_partitions=1, replication_factor=1)])
    futures[name].result(timeout=20)
    yield name
    try:
        admin.delete_topics([name])
    except Exception:
        pass


@pytest.fixture
def conn(infra):
    connection = db_module.connect(infra.postgres)
    db_module.init_schema(connection)
    yield connection
    connection.close()


def _produce(cfg, topic_name, records):
    from confluent_kafka import Producer

    producer = Producer(cfg.kafka.producer_conf())
    for record in records:
        producer.produce(
            topic_name,
            key=record["request_id"].encode(),
            value=json.dumps(record).encode(),
        )
    remaining = producer.flush(30)
    assert remaining == 0, "producer failed to flush {} message(s)".format(remaining)


def _consume(cfg, topic_name, ruleset, conn, max_records):
    kafka_cfg = config_module.KafkaConfig()
    kafka_cfg.bootstrap_servers = cfg.kafka.bootstrap_servers
    kafka_cfg.topic = topic_name
    kafka_cfg.group_id = "mvguard-e2e-" + uuid.uuid4().hex[:8]
    kafka_cfg.auto_offset_reset = "earliest"

    engine = GuardrailEngine(ruleset)
    sink = FanoutSink({"postgres": PostgresSink(conn)})
    service = GuardrailService(engine, sink, kafka_cfg, verbose=False)
    stats = service.run(max_records=max_records, idle_timeout=8.0)
    return engine, stats


def test_breach_flows_from_kafka_to_postgres_with_snapshot(infra, topic, conn):
    """One deliberately breaching record must land in Postgres with its input."""
    db_module.truncate_alerts(conn)

    ruleset = RuleSet([
        Rule("e2e_score_high", "output.score > 0.9", severity="critical",
             channels=["postgres"]),
    ])
    db_module.sync_rules(conn, ruleset)

    marker = uuid.uuid4().hex
    records = [
        {
            "request_id": "req-ok",
            "ts": time.time(),
            "model_version": "e2e",
            "input": {"age": 40, "segment": "retail", "marker": marker},
            "output": {"score": 0.10, "label": "approve", "latency_ms": 20.0},
        },
        {
            "request_id": "req-breach",
            "ts": time.time(),
            "model_version": "e2e",
            "input": {"age": 71, "segment": "enterprise", "marker": marker},
            "output": {"score": 0.97, "label": "reject", "latency_ms": 33.0},
        },
    ]

    _produce(infra, topic, records)
    engine, stats = _consume(infra, topic, ruleset, conn, max_records=len(records))

    assert stats.messages == 2
    assert stats.alerts_routed == 1
    assert engine.stats["e2e_score_high"].breach == 1
    assert engine.stats["e2e_score_high"].ok == 1

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rule_name, severity, observed, snapshot, record_key, kafka_offset
            FROM alerts WHERE rule_name = 'e2e_score_high'
            """
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    rule_name, severity, observed, snapshot, record_key, offset = rows[0]
    assert rule_name == "e2e_score_high"
    assert severity == "critical"
    assert observed["output.score"] == 0.97
    # The triggering input snapshot is what makes an alert actionable.
    assert snapshot["input"]["age"] == 71
    assert snapshot["input"]["segment"] == "enterprise"
    assert snapshot["input"]["marker"] == marker
    assert snapshot["output"]["score"] == 0.97
    assert record_key == "req-breach"
    assert offset is not None


def test_jsonb_snapshot_is_queryable(infra, topic, conn):
    """The snapshot must be queryable as JSONB, not an opaque blob."""
    db_module.truncate_alerts(conn)

    ruleset = RuleSet([
        Rule("e2e_any", "output.score > 0.5", severity="warning", channels=["postgres"]),
    ])
    db_module.sync_rules(conn, ruleset)

    records = [
        {
            "request_id": "req-{}".format(i),
            "ts": time.time(),
            "model_version": "e2e",
            "input": {"segment": segment},
            "output": {"score": 0.9, "label": "reject", "latency_ms": 10.0},
        }
        for i, segment in enumerate(["retail", "enterprise", "enterprise"])
    ]

    _produce(infra, topic, records)
    _consume(infra, topic, ruleset, conn, max_records=len(records))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM alerts WHERE snapshot->'input'->>'segment' = 'enterprise'"
        )
        (enterprise_count,) = cur.fetchone()

    assert enterprise_count == 2


def test_windowed_rule_fires_over_the_stream(infra, topic, conn):
    """A drift rule that no single record could trip must fire in aggregate."""
    db_module.truncate_alerts(conn)

    ruleset = RuleSet([
        Rule("e2e_drift", "mean_over(output.score, 60s, 50) > 0.60",
             severity="warning", cooldown_seconds=30.0, channels=["postgres"]),
    ])
    db_module.sync_rules(conn, ruleset)

    # Every score is individually in range; only the average crosses the line.
    base = time.time()
    records = [
        {
            "request_id": "req-{:04d}".format(i),
            "ts": base + i * 0.02,
            "model_version": "e2e",
            "input": {"segment": "retail"},
            "output": {"score": 0.80, "label": "reject", "latency_ms": 12.0},
        }
        for i in range(200)
    ]

    _produce(infra, topic, records)
    engine, stats = _consume(infra, topic, ruleset, conn, max_records=len(records))

    assert stats.messages == 200
    # min_samples=50 means the first 49 records are undetermined, not passing.
    assert engine.stats["e2e_drift"].undetermined == 49
    assert engine.stats["e2e_drift"].breach >= 1

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM alerts WHERE rule_name = 'e2e_drift'")
        (count,) = cur.fetchone()
    assert count >= 1


def test_offsets_commit_so_a_restart_does_not_reprocess(infra, topic, conn):
    """A second consumer in the same group must not re-see committed records."""
    db_module.truncate_alerts(conn)

    ruleset = RuleSet([
        Rule("e2e_noop", "output.score > 99", channels=["postgres"]),
    ])
    db_module.sync_rules(conn, ruleset)

    records = list(generate_records(50))
    _produce(infra, topic, records)

    kafka_cfg = config_module.KafkaConfig()
    kafka_cfg.bootstrap_servers = infra.kafka.bootstrap_servers
    kafka_cfg.topic = topic
    kafka_cfg.group_id = "mvguard-e2e-restart-" + uuid.uuid4().hex[:8]
    kafka_cfg.auto_offset_reset = "earliest"

    sink = FanoutSink({"postgres": PostgresSink(conn)})

    first = GuardrailService(GuardrailEngine(ruleset), sink, kafka_cfg, verbose=False)
    first_stats = first.run(max_records=len(records), idle_timeout=8.0)
    assert first_stats.messages == len(records)

    second = GuardrailService(GuardrailEngine(ruleset), sink, kafka_cfg, verbose=False)
    second_stats = second.run(idle_timeout=5.0)
    assert second_stats.messages == 0
