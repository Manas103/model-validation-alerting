-- mvguard schema. Idempotent: safe to run on every service start.

CREATE TABLE IF NOT EXISTS rules (
    name            TEXT PRIMARY KEY,
    expression      TEXT        NOT NULL,
    severity        TEXT        NOT NULL,
    description     TEXT,
    group_by        TEXT,
    cooldown_secs   DOUBLE PRECISION NOT NULL DEFAULT 0,
    -- Fingerprint of the expression text. Lets you tell, months later, whether
    -- an old alert was raised under the rule's current definition or a previous
    -- one -- the alert row records the fingerprint that was live when it fired.
    fingerprint     TEXT        NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS alerts (
    id                BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rule_name         TEXT        NOT NULL REFERENCES rules(name) ON DELETE CASCADE,
    severity          TEXT        NOT NULL,
    group_key         TEXT,
    -- Event time from the record itself, not ingest time. Two separate clocks:
    -- event_ts answers "when did the model score this", detected_ts answers
    -- "when did we notice". Replay makes them diverge, which is the point.
    event_ts          TIMESTAMPTZ NOT NULL,
    detected_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    fingerprint       TEXT        NOT NULL,
    -- Values of the sub-expressions that were actually evaluated: the "why".
    observed          JSONB       NOT NULL,
    -- The complete triggering record: input features, model output, metadata.
    -- JSONB rather than a fixed column set because feature schemas differ per
    -- model and change without warning; a rigid table would need a migration
    -- every time a team added a feature, and would drop the ones it did not
    -- know about -- exactly the fields someone debugging a breach needs.
    snapshot          JSONB       NOT NULL,
    -- How many further breaches of this rule were folded into this alert by the
    -- cooldown window. Zero means this alert stands alone.
    suppressed_count  INTEGER     NOT NULL DEFAULT 0,
    record_key        TEXT,
    kafka_partition   INTEGER,
    kafka_offset      BIGINT
);

CREATE INDEX IF NOT EXISTS alerts_rule_detected_idx
    ON alerts (rule_name, detected_ts DESC);

CREATE INDEX IF NOT EXISTS alerts_severity_detected_idx
    ON alerts (severity, detected_ts DESC);

CREATE INDEX IF NOT EXISTS alerts_event_ts_idx
    ON alerts (event_ts DESC);

-- GIN over the snapshot so post-hoc questions like "show me every breach where
-- the customer segment was enterprise" stay indexable without predefining which
-- feature anyone will want to filter on.
CREATE INDEX IF NOT EXISTS alerts_snapshot_gin_idx
    ON alerts USING GIN (snapshot);
