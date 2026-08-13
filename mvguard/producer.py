"""Synthetic scoring traffic for a fictional credit-decision model.

The generator is seeded and advances its own event clock at a fixed simulated
rate, so the same seed produces byte-identical messages every run. That is what
makes the end-to-end numbers in the README reproducible: the alerts a run
produces are a function of the seed and the rule file, not of how fast the
consumer happened to be scheduled.

Traffic moves through four phases, each designed to trip a different class of
guardrail:

======== ==================================================================
nominal  healthy scores and latency; nothing should fire
drift    the score distribution slides upward -- the failure a point-in-time
         range check cannot see and a windowed mean can
degraded latency spikes, the income feature starts arriving null, and a few
         scores escape [0, 1] entirely
recovery latency and scores return to normal, but an unrecognised customer
         segment starts appearing from an upstream change
======== ==================================================================
"""

import json
import random

MODEL_VERSIONS = ("credit-risk-v3.1.0", "credit-risk-v3.2.0-canary")
SEGMENTS = ("retail", "smb", "enterprise")
LABELS = ("approve", "review", "reject")

PHASES = ("nominal", "drift", "degraded", "recovery")


def _phase_for(index, total):
    position = index / max(1, total)
    if position < 0.25:
        return "nominal"
    if position < 0.55:
        return "drift"
    if position < 0.80:
        return "degraded"
    return "recovery"


def _clamp(value, low, high):
    return max(low, min(high, value))


def generate_records(count, seed=20240517, start_ts=1_700_000_000.0, events_per_second=50.0):
    """Yield ``count`` scoring records as plain dicts.

    Pure and deterministic -- no Kafka, no wall clock -- so the same function
    backs both the live producer and the unit tests.
    """
    rng = random.Random(seed)
    interval = 1.0 / events_per_second

    for index in range(count):
        phase = _phase_for(index, count)
        event_ts = start_ts + index * interval

        # Canary traffic is a small slice, and it is the version that drifts --
        # which is what the per-version grouped rule is there to isolate.
        is_canary = rng.random() < 0.20
        model_version = MODEL_VERSIONS[1] if is_canary else MODEL_VERSIONS[0]

        if phase == "nominal":
            score_centre = 0.35
            latency = rng.gauss(38.0, 9.0)
        elif phase == "drift":
            progress = (index / count - 0.25) / 0.30
            drift = 0.50 * _clamp(progress, 0.0, 1.0)
            score_centre = 0.35 + (drift if is_canary else drift * 0.25)
            latency = rng.gauss(42.0, 10.0)
        elif phase == "degraded":
            score_centre = 0.55
            latency = rng.gauss(180.0, 70.0)
        else:
            score_centre = 0.38
            latency = rng.gauss(45.0, 12.0)

        score = _clamp(rng.gauss(score_centre, 0.12), 0.0, 1.0)

        income = round(max(0.0, rng.gauss(58_000, 21_000)), 2)
        age = int(_clamp(rng.gauss(41, 13), 18, 92))
        segment = rng.choice(SEGMENTS)
        tenure = int(_clamp(rng.gauss(26, 18), 0, 240))

        if phase == "degraded":
            # Upstream feature service degrades: income starts arriving null.
            if rng.random() < 0.28:
                income = None
            # A serialisation bug lets a raw pre-sigmoid value through.
            if rng.random() < 0.004:
                score = round(rng.uniform(1.01, 4.7), 4)
            # And occasionally the score is absent entirely.
            if rng.random() < 0.003:
                score = None

        if phase == "recovery":
            # A new upstream release starts emitting a segment nobody declared.
            if rng.random() < 0.06:
                segment = "smb-plus"

        # A small, constant trickle of genuinely out-of-domain ages, present in
        # every phase: the kind of low-grade data-quality noise that should
        # register as informational rather than page anyone.
        if rng.random() < 0.008:
            age = int(rng.choice([15, 16, 17, 104, 117]))

        if score is None:
            label = None
        elif score >= 0.66:
            label = LABELS[2]
        elif score >= 0.40:
            label = LABELS[1]
        else:
            label = LABELS[0]

        record = {
            "request_id": "req-{:07d}".format(index),
            "model_version": model_version,
            "phase": phase,
            "ts": round(event_ts, 6),
            "input": {
                "age": age,
                "income": income,
                "segment": segment,
                "tenure_months": tenure,
                "prior_defaults": 0 if rng.random() < 0.87 else rng.randint(1, 3),
            },
            "output": {
                "score": score if score is None else round(score, 6),
                "label": label,
                "latency_ms": round(max(1.0, latency), 3),
            },
        }
        yield record


def publish(kafka_config, count, seed=20240517, events_per_second=50.0, progress_every=2000):
    """Produce ``count`` synthetic records to the configured Kafka topic."""
    from confluent_kafka import Producer

    producer = Producer(kafka_config.producer_conf())
    delivered = {"ok": 0, "failed": 0}

    def on_delivery(err, _msg):
        if err is None:
            delivered["ok"] += 1
        else:
            delivered["failed"] += 1

    sent = 0
    for record in generate_records(count, seed=seed, events_per_second=events_per_second):
        payload = json.dumps(record, separators=(",", ":")).encode("utf-8")
        while True:
            try:
                producer.produce(
                    kafka_config.topic,
                    key=record["request_id"].encode("utf-8"),
                    value=payload,
                    on_delivery=on_delivery,
                )
                break
            except BufferError:
                # Local queue is full: let librdkafka drain before retrying.
                producer.poll(0.5)
        sent += 1
        producer.poll(0)
        if progress_every and sent % progress_every == 0:
            print("  produced {}/{}".format(sent, count), flush=True)

    producer.flush(30)
    return {"sent": sent, "delivered": delivered["ok"], "failed": delivered["failed"]}
