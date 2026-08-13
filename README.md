# Model Validation and Alerting Framework

A service that lets non-engineers define guardrails on a live model's output in a
small expression language, evaluates those rules against a streaming Kafka feed,
and routes every breach to an alert with the triggering input snapshot attached.

Python, Kafka, PostgreSQL. The expression language -- tokenizer, parser,
evaluator -- is written from scratch; there is no `eval()` anywhere in the
evaluation path.

## Honest framing, up front

- **The scoring traffic is synthetic.** `mvguard/producer.py` generates a seeded,
  deterministic stream for a fictional credit-risk model that moves through four
  phases (nominal, drift, degraded, recovery). There is no real model and no real
  customer data behind this.
- **Everything measured below is a real run** against a real single-broker Kafka
  4.3.1 cluster and a real PostgreSQL 14.23 instance. The alert counts, the SQL
  output, and the test results are transcribed from actual terminal output, not
  written by hand to look plausible.
- **Single broker, single partition, one consumer.** This is a correctness and
  design project, not a throughput project. The ordering section below explains
  why the partition count is load-bearing rather than incidental.
- **Event timestamps are simulated.** The producer stamps records on its own
  clock at a fixed simulated rate, which is what makes a run reproducible from
  its seed. Wall-clock ingestion timing is real; event time is synthetic.

## Why a custom expression language instead of `eval()`

The entire premise is that someone who is not an engineer -- a risk owner, a data
scientist, whoever owns the model's behaviour -- can add a guardrail by editing a
YAML file. That premise sets the constraints:

**`eval()` is disqualified on safety.** Rules are configuration. Configuration
gets edited by people who are not thinking about sandbox escapes, gets copied
between environments, and eventually gets loaded from somewhere less trusted than
a file in the repo. `eval("__import__('os').system('...')")` is a working rule in
any design that reaches for Python's evaluator, and no amount of `__builtins__`
stripping makes that a defensible foundation.

**Python semantics are wrong for this domain anyway.** Scoring traffic has holes
in it. In Python, `None > 0.9` raises and `bool(None)` is `False` -- so a missing
feature either crashes the consumer or, far worse, silently reads as "no breach".
A guardrail that quietly stops firing when its input goes missing is the failure
mode this whole system exists to prevent. The language instead uses SQL's
three-valued logic, where a rule over missing data is **undetermined** and gets
counted as such.

**A parsed AST buys things a string cannot.** Because rules are parsed rather
than exec'd, `lint` can reject a bad rule before it is ever armed and point at
the exact column; `explain` can list precisely which fields a rule reads; window
call sites can be identified and fed independently of evaluation order; and every
sub-expression's value can be captured and attached to the alert as the *why*.

The cost is real -- roughly 900 lines of tokenizer, parser, and evaluator -- and
it is the right trade for something whose only job is to be trusted.

## What the language looks like

```yaml
- name: score_drift_high
  severity: warning
  when: mean_over(output.score, 60s, 200) > 0.60
  group_by: model_version
  cooldown: 60s
  channels: [postgres, console]
```

Comparisons, `and`/`or`/`not`, arithmetic, parentheses, `in [...]` lists, dotted
field references into the record, scalar functions (`abs`, `is_null`,
`is_missing`, `coalesce`, `len`, `lower`, `min`, `max`, `round`, ...), and
time-windowed aggregates (`mean_over`, `rate_over`, `count_over`, `stddev_over`,
`min_over`, `max_over`, `sum_over`).

Full grammar, type rules, and null semantics: [`docs/grammar.md`](docs/grammar.md).

### Errors are written for the person who made them

Real output from the parser:

```
$ 0 < output.score < 1
comparisons cannot be chained; write 'a < b and b < c' instead (column 18)
0 < output.score < 1
                 ^

$ median(output.score) > 1
unknown function 'median'; available functions are: abs, ceil, coalesce,
count_over, floor, is_missing, is_null, len, lower, max, max_over, mean_over,
min, min_over, rate_over, round, stddev_over, sum_over, upper (column 1)
median(output.score) > 1
^^^^^^

$ mean_over(output.score, 60) > 1
mean_over() expects a window duration as its second argument, such as 60s or 5m (column 25)
mean_over(output.score, 60) > 1
                        ^
```

`output.score = 1` suggests `==`; `a & b` suggests `and`; `5min` names the units
that do exist. Each one is a mistake someone will actually make.

### `lint` is what makes this safe to hand to a non-engineer

`lint` parses every rule and then **dry-runs the whole rule file against sample
traffic**, so an author can see what their guardrail would have done before it is
armed. Real output:

```
$ python -m mvguard.cli lint
parsed 10 rule(s) from rules/guardrails.yaml
  score_out_of_range           critical output.score < 0 or output.score > 1
  score_missing                critical is_null(output.score)  [cooldown=10s]
  score_drift_high             warning  mean_over(output.score, 60s, 200) > 0.60  [windowed<=60s, group_by=model_version, cooldown=60s]
  ...

dry run over 12000 sample record(s) -- 40 alert(s) would have fired
  rule                           breach     held  suppressed  undetermined   error
  age_out_of_domain                   8    11911          81             0       0
  income_null_rate                    1     9071        2729           199       0
  ...
```

It exits non-zero if any rule fails to evaluate, and separately warns about rules
that no sample record tripped -- which is usually fine, but is also what a rule
that can *never* fire looks like.

The dry run reproduces the live run **exactly**: the same 40 alerts with the same
per-rule breakdown that the streaming service produces against real Kafka further
down this page. That is a consequence of windows and cooldowns being measured in
event time rather than wall-clock time -- the evaluation result is a function of
the records and the rules, not of consumer scheduling -- and it is what makes a
dry run trustworthy as a preview rather than an approximation.

## Architecture

```
producer.py                    Kafka topic              service.py
  synthetic scoring traffic  --> model-scores  -->  consume, decode JSON
  seeded, deterministic          (1 partition)            |
  4 drift phases                                          v
                                                    engine.py
                                                      for each rule:
                                                        group_key = rule.group_by
                                                        window pre-pass
                                                        evaluate AST
                                                        cooldown / dedup
                                                          |
                                                  +-------+-------+
                                                  v               v
                                          PostgresSink      ConsoleSink
                                          alerts table      (severity-filtered)
                                          + rules table      WebhookSink
                                                             (optional)
```

```
mvguard/expr/      the language: errors, values, tokenizer, nodes, parser,
                   functions, windows, evaluator
mvguard/rules.py   YAML rule loading and strict validation
mvguard/engine.py  per-record evaluation, outcome accounting, cooldown
mvguard/sinks.py   Postgres / console / webhook routing by severity
mvguard/db.py      schema init, rule sync, alert persistence
mvguard/service.py Kafka consume loop and offset commit policy
mvguard/cli.py     lint, explain, init-db, produce, run, report
```

### Every record produces one of five outcomes per rule

`ok`, `breach`, `suppressed` (a breach inside another's cooldown),
`undetermined` (UNKNOWN -- missing or null input), and `error` (the rule could
not be evaluated at all). Keeping `undetermined` and `error` out of `ok` is the
difference between a guardrail that is passing and a guardrail that has quietly
stopped being evaluated. A rule that raises an evaluation error is counted and
skipped for that record; it never takes the consumer down, and it never stops the
other nine rules from running.

### Why the alert carries a JSONB snapshot

An alert that says "drift rule fired" is nearly useless at 3am. The alert row
carries `observed` (the value each evaluated sub-expression produced -- the
*why*) and `snapshot` (the entire triggering record). JSONB rather than a fixed
column set because feature schemas differ per model and change without warning: a
rigid table needs a migration every time a team adds a feature, and would silently
drop the features it did not know about -- exactly the ones someone debugging a
breach needs. A GIN index keeps the snapshot queryable without predefining which
feature anyone will want to filter on:

```sql
SELECT snapshot->'input'->>'segment' AS segment, count(*)
FROM alerts GROUP BY 1 ORDER BY 2 DESC;

  segment   | count
------------+-------
 smb        |    19
 enterprise |    11
 retail     |     8
 smb-plus   |     2
```

### Cooldown, and making suppression add up

A drifting model breaches on nearly every record. Without suppression, one bad
deploy writes ten thousand identical rows and the signal is gone. Each rule
declares a `cooldown:`; further breaches of the same rule *and group* inside that
window are folded, and the count travels on the next alert as
`suppressed_count`, so nothing is silently discarded.

Cooldown is measured in **event time**, matching the windows. A wall-clock
cooldown would fold thousands of replayed breaches into a single alert purely
because the replay ran faster than real time.

Breaches still inside an unexpired cooldown when the run ends belong to no alert
row, so the service reports them separately at shutdown. In the run below that is
9,235 breaches -- and 9,593 + 9,235 = 18,828, which is exactly the engine's total
suppressed count. The accounting reconciles.

## The bug worth reading about: event-time windows over a partitioned topic

The first full run produced this:

```
score_drift_high    alerts=0    held=11142    undetermined=858
```

Zero. But the drift rule is the centrepiece of the whole demo, and an offline
check over the same generated traffic said it should have fired:

```
credit-risk-v3.1.0          peak 60s mean = 0.5619
credit-risk-v3.2.0-canary   peak 60s mean = 0.6593   <-- threshold is 0.60
```

The canary's windowed mean clearly crosses 0.60. Evaluated offline it breached;
evaluated through Kafka it did not. The difference had to be the transport, so I
measured the event-time ordering the consumer actually saw:

```
records consumed: 12000
partition distribution: {0: 4033, 1: 3960, 2: 4007}
records arriving with an EARLIER event ts than predecessor: 3 (0.0%)
largest backwards jump: 239.9 simulated seconds
```

Only **three** backwards jumps -- but each one is 239.9 seconds, the entire length
of the run. The topic had three partitions. Kafka guarantees ordering *within* a
partition, not across them, so the consumer drained partition 0's whole timeline
(t=0 to t=240s), then jumped back to t=0 and replayed the timeline again from
partition 1, then again from partition 2. The stream was three concatenated
copies of the run.

A 60-second event-time window over that is meaningless. Records from t=240 and
t=0 sat in the same window, the average was taken across the entire timeline
instead of a one-minute slice, and the drift peak was diluted out of existence.
The rule was not broken; its input was, and it failed **silently** -- which is the
part that actually matters, because a monitoring system that quietly computes the
wrong aggregate is worse than one that crashes.

Two fixes, both kept:

1. **Make disorder impossible to miss.** `WindowStore` now tracks a watermark --
   the highest event time it has seen -- and prunes relative to that, never
   relative to a timestamp that moved backwards. A moderately out-of-order record
   is inserted in timestamp order so left-pruning stays correct; a record older
   than `watermark - span` has missed its window entirely and is counted as
   **late** and dropped rather than folded into an aggregate it does not belong
   to. Both counters print at the end of every run, and a non-zero count prints
   an explicit warning naming the partitioning cause.

2. **Order the input.** Global windowed rules need a totally ordered stream, so
   the demo topic uses a single partition. Scaling past one consumer means
   partitioning by each rule's `group_by` key so every group's stream stays
   ordered on one partition -- the standard Kafka Streams model. That is a real
   limitation of this implementation, not a solved problem, and it is listed
   below rather than papered over.

After the fix, on a single-partition topic:

```
score_drift_high    alerts=1    held=11038   suppressed=563   undetermined=398
windows=5 samples=7505 dropped=0 late=0 out_of_order=0
```

The window sample count is the tell: 7,505 after the fix versus 26,437 before.
The windows are now actually holding 60 seconds of traffic instead of
accumulating across the whole blended timeline.

### A second, smaller bug: short-circuiting corrupted the windows

Window functions have a side effect -- every record must be added to the trailing
window -- but `and`/`or` short-circuit. In a rule like

```
output.latency_ms > 250 and mean_over(output.score, 60s) > 0.8
```

the `mean_over` sub-expression is only *reached* on records where the first
condition already held, so updating windows during evaluation built the average
out of a biased subset: only the slow requests. The rule silently measured
something other than what it said.

The fix is a **window pre-pass**: before evaluating the tree, walk it and feed
every window call site unconditionally, then evaluate with short-circuiting
intact. Both properties are worth keeping, and separating the update from the
read is what allows it. `test_window_update_is_not_biased_by_short_circuit`
pins the behaviour.

### And a third: window state leaking across rule edits

Window state was keyed by `(rule name, call site, group key)`, and call-site ids
restart at zero for every expression. Rewriting a rule therefore inherited the
previous version's samples -- so changing `mean_over(...)` to `rate_over(...)`
kept feeding the new aggregate from the old one's numeric history. The key now
includes a fingerprint of the expression text, so an edited rule starts clean.
Found by a unit test that reused one rule name across two expressions and got a
`rate_over` of 0.5 where 1.0 was correct.

## Real measured results

12,000 synthetic records, seed 20240517, 10 rules, single-partition topic, real
Kafka 4.3.1 and real PostgreSQL 14.23. Full run output:

```
--- run summary ---
messages=12000 decode_errors=0 alerts=40 elapsed=13.8s (869 msg/s)
windows=5 samples=7505 dropped=0 late=0 out_of_order=0

rule                           alerts      held  suppressed  undetermined   error
age_out_of_domain                   8     11911          81             0       0
income_null_rate                    1      9071        2729           199       0
label_contradicts_score             0     11995           0             5       0
latency_spike                       6     11503         491             0       0
latency_sustained                   2      8986        2814           198       0
missing_utilisation_feature         4         0       11996             0       0
score_drift_high                    1     11038         563           398       0
score_missing                       3     11995           2             0       0
score_out_of_range                 13     11982           0             5       0
unknown_segment                     2     11846         152             0       0

9235 breach(es) were still inside an unexpired cooldown at shutdown and are
not reflected in any alert row's suppressed_count:
  age_out_of_domain                        7
  income_null_rate                         2729
  latency_spike                            68
  latency_sustained [credit-risk-v3.1.0]   2247
  latency_sustained [credit-risk-v3.2.0-canary] 567
  missing_utilisation_feature              2999
  score_drift_high [credit-risk-v3.2.0-canary] 563
  unknown_segment                          55
```

And the alerts as they actually landed in PostgreSQL:

```
$ python -m mvguard.cli report
rule                         severity   alerts    folded  event window
score_out_of_range           critical       13         0  17:15:44 .. 17:16:30
age_out_of_domain            info            8        74  17:13:22 .. 17:16:58
latency_spike                warning         6       423  17:15:32 .. 17:16:22
missing_utilisation_feature  info            4      8997  17:13:20 .. 17:16:20
score_missing                critical        3         2  17:15:52 .. 17:16:18
latency_sustained            warning         2         0  17:15:48 .. 17:15:49
unknown_segment              warning         2        97  17:16:32 .. 17:17:03
income_null_rate             warning         1         0  17:16:06 .. 17:16:06
score_drift_high             warning         1         0  17:15:26 .. 17:15:26

40 alert row(s), 9593 further breach(es) folded by cooldown
```

40 alerts out of 12,000 records, from 18,828 underlying breaches. That ratio is
the cooldown doing its job: `missing_utilisation_feature` breaches on literally
every record (the feature is never sent) and produces 4 alerts rather than 12,000.

### One breach, in full

This is the drift alert, straight out of `psql`:

```
id               | 52
rule_name        | score_drift_high
severity         | warning
group_key        | credit-risk-v3.2.0-canary
event_ts         | 2023-11-14 17:15:26.08-05
suppressed_count | 0
record_key       | req-0006304
kafka_offset     | 6304
observed         | {
                 |     "output.score": 0.846274,
                 |     "mean_over(output.score, 60.0s, 200)": 0.6001237111486492
                 | }
snapshot         | {
                 |     "ts": 1700000126.08,
                 |     "input": {
                 |         "age": 39,
                 |         "income": 10877.34,
                 |         "segment": "smb",
                 |         "tenure_months": 19,
                 |         "prior_defaults": 0
                 |     },
                 |     "phase": "drift",
                 |     "output": {
                 |         "label": "reject",
                 |         "score": 0.846274,
                 |         "latency_ms": 47.333
                 |     },
                 |     "request_id": "req-0006304",
                 |     "model_version": "credit-risk-v3.2.0-canary"
                 | }
```

Everything needed to reproduce the breach is on the row: the windowed mean that
crossed the line (0.60012 against a 0.60 threshold), the record that pushed it
over, and the Kafka offset to replay from. `group_key` shows the rule correctly
attributing the drift to the **canary** version -- the primary model's own 60s
mean peaked at 0.5619 and never fired, which is the entire point of `group_by`.

### Two rules that correctly stayed silent

`label_contradicts_score` fired zero times across 12,000 records, with 5
undetermined. It is a genuine invariant (a score above the reject threshold must
produce a reject label) that the traffic never violates, and the 5 undetermined
are the records where the score was null -- correctly *not* counted as passing.
A guardrail suite where every rule fires is a suite that has been tuned to fire.

## Test results

117 tests, all passing. 113 unit tests plus 4 end-to-end tests that run against
the live broker and database (skipped automatically if either is unreachable).

```
$ python -m pytest tests/ -q
.........................................................................
.............................................
117 passed in 19.46s
```

Coverage is weighted toward the language, since that is the part everything else
trusts: tokenizer edge cases (scientific notation, escapes, duration units),
precedence and associativity, every parse-error path, three-valued logic across
`and`/`or`/`not`/`in`, null-versus-missing, type errors, window semantics
(pruning, min-samples, grouping, late and out-of-order arrival), cooldown
behaviour, and rule-file validation.

Three tests are regression tests for the bugs described above. One more,
`test_same_seed_same_alerts`, asserts that two independent runs over the same
seed produce an identical alert sequence -- the property that makes the numbers
in this README reproducible rather than anecdotal.

The end-to-end tests produce to a real throwaway topic and assert on real rows
read back from PostgreSQL: that a breach arrives with its input snapshot intact,
that the JSONB snapshot is queryable by nested field, that a windowed rule fires
over a stream where no individual record could trip it, and that committed
offsets prevent a restarted consumer from reprocessing.

## Running it

Requires Python 3.10+, a Kafka broker, and PostgreSQL.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Standing up the infrastructure from scratch (Kafka 4.3.1, KRaft mode -- no
ZooKeeper):

```bash
curl -O https://downloads.apache.org/kafka/4.3.1/kafka_2.13-4.3.1.tgz
tar -xzf kafka_2.13-4.3.1.tgz && cd kafka_2.13-4.3.1
bin/kafka-storage.sh format -t $(bin/kafka-storage.sh random-uuid) \
    -c config/server.properties --standalone
bin/kafka-server-start.sh -daemon config/server.properties

# One partition: global event-time windows need a totally ordered stream.
bin/kafka-topics.sh --create --topic model-scores \
    --bootstrap-server localhost:9092 --partitions 1 --replication-factor 1
```

```bash
sudo -u postgres psql -c "CREATE ROLE mvuser WITH LOGIN PASSWORD 'mvpass' CREATEDB;"
sudo -u postgres psql -c "CREATE DATABASE model_validation OWNER mvuser;"
```

Then:

```bash
python -m mvguard.cli lint                      # parse + dry-run the rules
python -m mvguard.cli explain score_drift_high  # what one rule reads and does
python -m mvguard.cli init-db --reset-alerts    # schema + sync rule definitions
python -m mvguard.cli produce --count 12000     # publish synthetic traffic
python -m mvguard.cli run --idle-timeout 8      # consume, evaluate, route
python -m mvguard.cli report --limit 5          # what landed in Postgres
python -m pytest tests/ -q
```

Connection settings come from the environment, with the local-dev defaults above:
`MVGUARD_KAFKA_BOOTSTRAP`, `MVGUARD_TOPIC`, `MVGUARD_GROUP_ID`, `MVGUARD_PG_HOST`,
`MVGUARD_PG_PORT`, `MVGUARD_PG_USER`, `MVGUARD_PG_PASSWORD`, `MVGUARD_PG_DATABASE`,
`MVGUARD_RULES`, `MVGUARD_WEBHOOK_URL`. The credentials are throwaway local
values, not a secrets-management story -- a real deployment would inject them and
`MVGUARD_PG_PASSWORD` would have no default at all.

## Delivery semantics

Offsets are committed only after the alerts from those records are durably
written to PostgreSQL. A crash mid-batch therefore replays the affected records
and can produce a **duplicate** alert, never a missing one -- the right direction
to err for a system whose entire job is noticing things. There is no dedup key on
the alerts table, so exactly-once would need one (rule name, group, event time,
offset) plus an upsert; at-least-once was the deliberate choice here.

## Limitations

- **One partition, one consumer.** Global windowed rules need a totally ordered
  stream. Scaling out requires partitioning by each rule's `group_by` key and
  running one consumer per partition; ungrouped global rules would additionally
  need a shuffle or a two-stage aggregation. Not implemented -- the system now
  *detects and reports* the disorder rather than silently producing wrong
  aggregates, which is a diagnostic, not a fix.
- **Window state is in-process and lost on restart.** A restarted consumer
  rebuilds windows from whatever it re-consumes, so a drift rule is blind for its
  first window after a restart (`min_samples` at least makes this explicit rather
  than producing a confident wrong answer). Durable state would mean a state
  store keyed the same way the windows are.
- **Windows are unbounded in count.** One window exists per (rule, call site,
  group key). A `group_by` on something high-cardinality like `request_id` would
  allocate a window per request. There is a per-window sample cap but no cap on
  the number of windows.
- **Rules are loaded at startup, not hot-reloaded.** Editing the file needs a
  restart, which drops window state as above.
- **The webhook sink is best-effort**, with no retry or dead-letter queue. It is
  deliberately non-blocking because Postgres has already stored the alert
  durably, but a real integration would need retries.
- **No authentication or TLS** on either Kafka or PostgreSQL connections.
- **`stddev_over` uses the population formula**, and returns 0.0 for a single
  sample rather than undefined.
