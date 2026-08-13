"""Trailing-time-window state for the ``*_over`` aggregate functions.

Windows are keyed by ``(rule name + expression fingerprint, call site, group
key)``. The call site keeps two different ``mean_over`` calls in the same rule
independent; the fingerprint keeps a rewritten rule from inheriting the previous
version's samples; the group key keeps one model version's drift from
contaminating another's.

Windows are pruned by **event time taken from the record**, not wall-clock time,
so replaying the same messages produces the same alerts regardless of how fast
the consumer runs.

Ordering
--------
Event-time windowing is only meaningful over an ordered stream, and Kafka orders
messages within a partition, not across them. A record whose timestamp precedes
the highest one already seen is handled here rather than silently corrupting the
aggregate:

* the window tracks a **watermark** -- the highest event time it has observed --
  and prunes relative to that, never relative to a timestamp that moved
  backwards;
* a moderately out-of-order record is inserted in timestamp order, so the window
  stays sorted and left-pruning stays correct;
* a record older than ``watermark - span`` is **late**: its window has already
  closed, so it is counted and dropped rather than being folded into an
  aggregate it does not belong to.

The counters are the point. Silent misordering produced a real bug in this
project -- see the README -- and a counter that an operator can see beats an
aggregate that is quietly wrong.
"""

from collections import deque


class WindowStore:
    """Bounded per-(rule, site, group) sample history over event time."""

    def __init__(self, max_samples_per_window=100_000):
        # key -> [deque of (event_ts, value), watermark]
        self._windows = {}
        self._max_samples = max_samples_per_window
        self.dropped_samples = 0
        self.late_samples = 0
        self.out_of_order_samples = 0

    def key(self, rule_scope, site_id, group_key):
        return (rule_scope, site_id, group_key)

    def observe(self, key, event_ts, value, span_seconds):
        """Add a sample, maintaining sort order and the trailing window."""
        state = self._windows.get(key)
        if state is None:
            state = [deque(), event_ts]
            self._windows[key] = state

        window = state[0]
        if event_ts > state[1]:
            state[1] = event_ts
        watermark = state[1]
        cutoff = watermark - span_seconds

        if event_ts < cutoff:
            # The window this sample belonged to has already closed.
            self.late_samples += 1
            return

        if window and event_ts < window[-1][0]:
            self.out_of_order_samples += 1
            _insert_sorted(window, event_ts, value)
        else:
            window.append((event_ts, value))

        while window and window[0][0] < cutoff:
            window.popleft()

        # Safety valve: a very wide window on a very fast topic could otherwise
        # grow without bound. Dropping the oldest samples degrades the aggregate
        # gracefully instead of exhausting memory, and the counter makes the
        # degradation visible rather than silent.
        while len(window) > self._max_samples:
            window.popleft()
            self.dropped_samples += 1

    def values(self, key):
        state = self._windows.get(key)
        if not state or not state[0]:
            return []
        return [value for _, value in state[0]]

    def sample_count(self, key):
        state = self._windows.get(key)
        return len(state[0]) if state else 0

    def watermark(self, key):
        state = self._windows.get(key)
        return state[1] if state else None

    def window_count(self):
        return len(self._windows)

    def total_samples(self):
        return sum(len(state[0]) for state in self._windows.values())

    @property
    def disorder_detected(self):
        return bool(self.late_samples or self.out_of_order_samples)

    def reset(self):
        self._windows.clear()
        self.dropped_samples = 0
        self.late_samples = 0
        self.out_of_order_samples = 0


def _insert_sorted(window, event_ts, value):
    """Insert into a sorted deque, scanning from the newest end.

    Out-of-order arrival is expected to be local (a few records), so scanning
    back from the right is cheap in practice and leaves the common in-order path
    as a plain O(1) append.
    """
    buffer = []
    while window and window[-1][0] > event_ts:
        buffer.append(window.pop())
    window.append((event_ts, value))
    while buffer:
        window.append(buffer.pop())
