"""The streaming service: consume scoring records, evaluate rules, route alerts.

Offsets are committed only after the alerts from those records are durably in
PostgreSQL. If the process dies mid-batch the affected records are re-consumed
and re-evaluated, so the failure mode is a duplicate alert rather than a missing
one -- the right direction to err for a system whose entire job is noticing.
"""

import json
import signal
import time


class ServiceStats:
    def __init__(self):
        self.messages = 0
        self.decode_errors = 0
        self.alerts_routed = 0
        self.started_at = time.time()
        self.finished_at = None

    @property
    def elapsed(self):
        end = self.finished_at if self.finished_at is not None else time.time()
        return end - self.started_at

    @property
    def rate(self):
        elapsed = self.elapsed
        return self.messages / elapsed if elapsed > 0 else 0.0


class GuardrailService:
    def __init__(self, engine, sink, kafka_config, commit_every=500, verbose=True):
        self.engine = engine
        self.sink = sink
        self.kafka_config = kafka_config
        self.commit_every = commit_every
        self.verbose = verbose
        self.stats = ServiceStats()
        self._stop = False

    def request_stop(self, *_args):
        self._stop = True

    def run(self, max_records=None, idle_timeout=10.0, poll_timeout=1.0):
        from confluent_kafka import Consumer, KafkaError

        from .engine import RecordMeta

        consumer = Consumer(self.kafka_config.consumer_conf())
        consumer.subscribe([self.kafka_config.topic])

        previous_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[sig] = signal.signal(sig, self.request_stop)
            except ValueError:
                # Not on the main thread (integration tests); Ctrl-C handling is
                # a convenience, not a correctness requirement.
                pass

        pending = []
        uncommitted = 0
        last_message_at = time.time()

        try:
            while not self._stop:
                message = consumer.poll(poll_timeout)

                if message is None:
                    if pending:
                        self._flush(pending)
                        pending = []
                    if uncommitted:
                        consumer.commit(asynchronous=False)
                        uncommitted = 0
                    if idle_timeout and time.time() - last_message_at > idle_timeout:
                        if self.verbose:
                            print(
                                "  no messages for {:.0f}s, stopping".format(idle_timeout),
                                flush=True,
                            )
                        break
                    continue

                if message.error():
                    if message.error().code() == KafkaError._PARTITION_EOF:
                        continue
                    raise RuntimeError("kafka error: {}".format(message.error()))

                last_message_at = time.time()

                try:
                    record = json.loads(message.value().decode("utf-8"))
                    if not isinstance(record, dict):
                        raise ValueError("record is not a JSON object")
                except (ValueError, UnicodeDecodeError):
                    # A malformed message must not stall the partition forever.
                    self.stats.decode_errors += 1
                    uncommitted += 1
                    continue

                meta = RecordMeta(
                    key=message.key().decode("utf-8", "replace") if message.key() else None,
                    partition=message.partition(),
                    offset=message.offset(),
                )

                alerts = self.engine.process(record, meta)
                if alerts:
                    pending.extend(alerts)

                self.stats.messages += 1
                uncommitted += 1

                if len(pending) >= 200:
                    self._flush(pending)
                    pending = []

                if uncommitted >= self.commit_every:
                    if pending:
                        self._flush(pending)
                        pending = []
                    consumer.commit(asynchronous=False)
                    uncommitted = 0

                if self.verbose and self.stats.messages % 2000 == 0:
                    print(
                        "  consumed {} messages, {} alerts".format(
                            self.stats.messages, self.stats.alerts_routed
                        ),
                        flush=True,
                    )

                if max_records and self.stats.messages >= max_records:
                    break

            if pending:
                self._flush(pending)
            if uncommitted:
                consumer.commit(asynchronous=False)
        finally:
            self.stats.finished_at = time.time()
            consumer.close()
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)

        return self.stats

    def _flush(self, alerts):
        self.sink.emit(alerts)
        self.stats.alerts_routed += len(alerts)
