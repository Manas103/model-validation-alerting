"""Alert routing.

Each rule names the channels its breaches go to. A sink is anything with
``emit(alerts)`` and ``close()``; ``FanoutSink`` dispatches each alert to the
channels its own rule asked for, so a critical guardrail can page while an
informational one only lands in the table.
"""

import json
import sys
import urllib.error
import urllib.request

from . import db

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}


class Sink:
    name = "sink"

    def emit(self, alerts):
        raise NotImplementedError

    def close(self):
        pass


class PostgresSink(Sink):
    """Durable storage. Always in the channel list -- it is the audit trail."""

    name = "postgres"

    def __init__(self, conn):
        self.conn = conn
        self.written = 0

    def emit(self, alerts):
        written = db.insert_alerts(self.conn, alerts)
        self.written += written
        return written

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


class ConsoleSink(Sink):
    """Human-readable output for running the service in a terminal."""

    name = "console"

    def __init__(self, stream=None, min_severity="info", colour=True):
        self.stream = stream if stream is not None else sys.stdout
        self.min_severity = SEVERITY_ORDER.get(min_severity, 0)
        self.colour = colour and getattr(self.stream, "isatty", lambda: False)()
        self.written = 0

    _COLOURS = {"info": "\033[36m", "warning": "\033[33m", "critical": "\033[31m"}
    _RESET = "\033[0m"

    def emit(self, alerts):
        count = 0
        for alert in alerts:
            if SEVERITY_ORDER.get(alert.severity, 0) < self.min_severity:
                continue
            label = alert.severity.upper()
            if self.colour:
                label = "{}{}{}".format(
                    self._COLOURS.get(alert.severity, ""), label, self._RESET
                )
            group = " [{}]".format(alert.group_key) if alert.group_key else ""
            folded = (
                " (+{} suppressed)".format(alert.suppressed_count)
                if alert.suppressed_count
                else ""
            )
            observed = ", ".join(
                "{}={}".format(key, _short(value))
                for key, value in sorted(alert.observed.items())
            )
            self.stream.write(
                "{:>8}  {}{}{}  when: {}\n          observed: {}\n".format(
                    label, alert.rule_name, group, folded, alert.expression, observed
                )
            )
            count += 1
        self.stream.flush()
        self.written += count
        return count


class WebhookSink(Sink):
    """POST each alert as JSON. Best-effort by design.

    A webhook that is down must not stall the consumer or lose the alert, and
    the Postgres sink has already stored it durably, so failures here are
    counted and logged rather than retried or raised.
    """

    name = "webhook"

    def __init__(self, url, timeout=3.0, min_severity="warning"):
        self.url = url
        self.timeout = timeout
        self.min_severity = SEVERITY_ORDER.get(min_severity, 1)
        self.written = 0
        self.failures = 0

    def emit(self, alerts):
        count = 0
        for alert in alerts:
            if SEVERITY_ORDER.get(alert.severity, 0) < self.min_severity:
                continue
            payload = json.dumps(
                {
                    "rule": alert.rule_name,
                    "severity": alert.severity,
                    "group": alert.group_key,
                    "event_ts": alert.event_ts,
                    "expression": alert.expression,
                    "observed": db._jsonable(alert.observed),
                    "snapshot": db._jsonable(alert.snapshot),
                }
            ).encode("utf-8")
            request = urllib.request.Request(
                self.url, data=payload, headers={"Content-Type": "application/json"}
            )
            try:
                urllib.request.urlopen(request, timeout=self.timeout).close()
                count += 1
            except (urllib.error.URLError, OSError):
                self.failures += 1
        self.written += count
        return count


class FanoutSink(Sink):
    """Route each alert to the channels its rule named."""

    name = "fanout"

    def __init__(self, channels):
        self.channels = dict(channels)
        self.unknown_channels = set()

    def emit(self, alerts):
        by_channel = {}
        for alert in alerts:
            targets = alert.channels or ("postgres",)
            for channel in targets:
                if channel not in self.channels:
                    self.unknown_channels.add(channel)
                    continue
                by_channel.setdefault(channel, []).append(alert)

        total = 0
        for channel, batch in by_channel.items():
            total += self.channels[channel].emit(batch) or 0
        return total

    def close(self):
        for sink in self.channels.values():
            sink.close()


def _short(value, limit=48):
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."
