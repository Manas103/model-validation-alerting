"""Configuration, read from environment variables with local-dev defaults.

The defaults point at the local Kafka broker and PostgreSQL instance described
in the README. They are development values, not secrets management -- a real
deployment would inject these from its own secret store, and the password would
not have a default at all.
"""

import os


def _env(name, default):
    value = os.environ.get(name)
    return default if value is None or value == "" else value


class KafkaConfig:
    def __init__(self):
        self.bootstrap_servers = _env("MVGUARD_KAFKA_BOOTSTRAP", "localhost:9092")
        self.topic = _env("MVGUARD_TOPIC", "model-scores")
        self.group_id = _env("MVGUARD_GROUP_ID", "mvguard-service")
        self.auto_offset_reset = _env("MVGUARD_AUTO_OFFSET_RESET", "earliest")

    def consumer_conf(self):
        return {
            "bootstrap.servers": self.bootstrap_servers,
            "group.id": self.group_id,
            "auto.offset.reset": self.auto_offset_reset,
            # Offsets are committed explicitly after alerts are durably written,
            # so a crash replays the affected records rather than losing them.
            "enable.auto.commit": False,
        }

    def producer_conf(self):
        return {
            "bootstrap.servers": self.bootstrap_servers,
            "linger.ms": 5,
            "acks": "all",
        }

    def __repr__(self):
        return "KafkaConfig(bootstrap={!r}, topic={!r}, group={!r})".format(
            self.bootstrap_servers, self.topic, self.group_id
        )


class PostgresConfig:
    def __init__(self):
        self.host = _env("MVGUARD_PG_HOST", "127.0.0.1")
        self.port = int(_env("MVGUARD_PG_PORT", "5432"))
        self.user = _env("MVGUARD_PG_USER", "mvuser")
        self.password = _env("MVGUARD_PG_PASSWORD", "mvpass")
        self.database = _env("MVGUARD_PG_DATABASE", "model_validation")

    def dsn_kwargs(self):
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "dbname": self.database,
        }

    def __repr__(self):
        return "PostgresConfig(host={!r}, port={}, db={!r}, user={!r})".format(
            self.host, self.port, self.database, self.user
        )


class Config:
    def __init__(self):
        self.kafka = KafkaConfig()
        self.postgres = PostgresConfig()
        self.rules_path = _env("MVGUARD_RULES", "rules/guardrails.yaml")
        self.webhook_url = _env("MVGUARD_WEBHOOK_URL", "")


def load():
    return Config()
