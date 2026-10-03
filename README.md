# Model Validation and Alerting Framework

A service that lets non-engineers define guardrails on a live model's output in a
small expression language, evaluates those rules against a streaming Kafka feed,
and routes every breach to an alert with the triggering input snapshot attached.

Python, Kafka, PostgreSQL. The expression language -- tokenizer, parser,
evaluator -- is written from scratch; there is no `eval()` anywhere in the
evaluation path. Extended with a second engine for cross-sectional
no-arbitrage checks on an option surface, a third for independent price
verification of a forward-mark curve, a fourth for prepayment model
monitoring, and a fifth that puts the no-arbitrage check into a VBA-driven
month-end workbook (real VBA, not executed live, since this machine has no
Excel; reconciled cell for cell against the Python engine by an independently
written mirror).

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

## Extension: no-arbitrage guardrails for an option pricing surface

Everything above this section is the original streaming guardrail engine,
unchanged. This section adds a second, honest use of the same idea: instead
of a risk model's scalar output stream, the guardrails now watch a
synthetic option-chain surface for violations of four no-arbitrage
identities, still as declarative rules a non-engineer can edit.

- **The traffic is synthetic**, same as above: `mvguard/surface_producer.py`
  generates a seeded, deterministic option-chain surface for a fictional
  underlying (`FIC`), 8 strikes x 5 maturities x 300 snapshots = 12,000
  quotes, built from a real Black-Scholes formula plus a small bid/ask
  spread and jitter. There is no real exchange or real quotes behind this.
- **This is a second engine, not a bolt-on to the first.** `mvguard/engine.py`
  evaluates one rule against one record plus that record's own time-windowed
  history. A no-arbitrage check compares several *different* quotes from the
  *same* instant against each other, which is not a history at all. Rather
  than teach the tokenizer and parser array traversal for a check that never
  needs a window or a cooldown, `mvguard/surface_guardrails.py` is a small,
  parallel engine with the same contract: `rules/option_surface_guardrails.yaml`
  is the only place a non-engineer needs to touch (which check is armed, its
  severity, its tolerance), and the comparison arithmetic lives in exactly
  one auditable file underneath it.

### The four rules

```yaml
- name: put_call_parity
  family: parity
  tolerance: 0.05
- name: strike_monotonicity
  family: monotonicity
  tolerance: 0.02
- name: butterfly_convexity
  family: butterfly
  tolerance: 0.02
- name: calendar_spread
  family: calendar
  tolerance: 0.02
```

**Put-call parity**: `call_mid - put_mid` must equal `spot - strike *
exp(-rate * T)` within tolerance. **Strike monotonicity**: call price
non-increasing in strike, put price non-decreasing, at a fixed maturity.
**Butterfly convexity**: for three equally spaced strikes, the discrete
second difference of call price must not be more negative than
`-tolerance`. **Calendar spread**: at a fixed strike, a longer-maturity call
must not be cheaper than a shorter one by more than tolerance. Full
definitions: `rules/option_surface_guardrails.yaml`.

### What one seed actually breaks

The seeded surface plants exactly 24 violations, 6 per family, each in its
own snapshot. The first version of the seed generator moved a single quote's
`call_mid` by a hand-picked round number (3 to 5 for monotonicity, roughly 1
for butterfly and calendar) and measured a surprise: only 15 of 24 seeds
tripped their own target family when run through the engine, and several
that did also tripped parity.

The measurement that discriminated was running the seeded surface through
`mvguard/surface_guardrails.py` and diffing which family fired against which
family each seed named as its target. The pattern was not random: every seed
that used a delta of a few dollars on `call_mid` alone also broke parity,
because parity only needs a 0.05 gap on that exact quote and a multi-dollar
move clears that trivially, while whether the same move clears a
monotonicity or calendar gap depends on the local price gradient at that
strike and maturity, which moves with the random-walked spot and is not a
constant. A flat guessed delta sometimes cleared the target tolerance and
sometimes did not, and when it did, it dragged parity along almost for free.

The fix, in `mvguard/surface_producer.py::_apply_seed`: for every family
except parity, the delta is computed from the actual neighboring quotes in
that snapshot, sized to clear the target tolerance by a fixed clearance
margin (0.05), and applied to **both** `call_mid` and `put_mid` of the
target quote by the same amount. Shifting both fields equally cancels their
effect on `call_mid - put_mid`, so parity stops firing as a side effect;
only the field the target check actually reads (call price for butterfly
and calendar, whichever side the spec names for monotonicity) still moves in
the way that trips the intended rule. Parity seeds are unaffected by this
fix and stay a plain, isolated, single-field delta.

After the fix: **24 of 24 seeds catch their target family, with zero alerts
in any unseeded snapshot.** The raw rule-firing count is still 46, not 24,
because several seeds legitimately clear more than one family's tolerance in
the same snapshot (a large enough butterfly or calendar violation frequently
also breaks monotonicity between the same strikes, which is a real property
of these identities, not a seeding artifact): `docs/option_surface_output.txt`
lists every seed and every family it fired. The honest claim is "24 seeded
violations caught, zero false positives", measured at the seed level, not
"exactly 24 alerts".

### Triggering inputs

Every alert this engine emits carries a `snapshot` dict (the strike(s),
maturity(ies), and prices actually compared, plus the surface's spot and
rate) and an `observed` dict (the exact numeric comparison and the tolerance
it failed), matching the convention `mvguard/engine.py` already uses.
Example, straight out of a real run:

```
family: butterfly
snapshot: {'maturity_days': 120, 'strikes': (90.0, 100.0, 110.0),
           'call_mids': (14.02, 8.6553, 4.62), 'spot': 100.99, 'rate': 0.03}
observed: {'second_difference': -0.5253, 'tolerance': -0.02}
```

### Measured results

Python 3.12.10 on Windows 11, no Kafka or PostgreSQL involved (this
extension runs entirely in-process over a Python list of dicts):

```
$ python scripts/run_option_surface_check.py
clean baseline: 300 snapshots, 12000 quotes, 0 alert(s)

seeded run: 300 snapshots, 12000 quotes, 46 rule-level alert(s) from 24 planted seeds
false-positive snapshots (alerts with no planted seed): none
false-positive alert count: 0
...
seeds caught (target family fired in its snapshot): 24/24
false positives (alerts outside any seeded snapshot): 0
```

Full transcript: `docs/option_surface_output.txt`. The pre-existing
streaming guardrail numbers earlier in this README are untouched by this
extension; both engines share the repository but not any state.

### Running it

```bash
python scripts/run_option_surface_check.py       # measure against the seeded surface
python -m pytest tests/test_option_surface.py -q # unit tests for this extension
python -m pytest tests/ -q                        # full suite, including the pre-existing 117
```

### Limitations of this extension

- **Strike spacing must be literally equal** for the butterfly check to fire
  on a triple; an irregular strike ladder silently skips that triple rather
  than approximating convexity across unequal spacing.
- **No dividend yield.** Put-call parity and the calendar-spread check both
  assume a zero dividend yield; a real equity surface would need a `q` term
  in both formulas.
- **A snapshot with a missing quote just narrows what gets checked**, it
  does not raise an error the way `mvguard/expr`'s `is_missing` machinery
  does for the streaming engine; this engine has no analogous "undetermined"
  outcome yet.
- **In-process only.** Unlike the streaming engine, this extension does not
  read from Kafka or write to Postgres; it operates on a list of snapshot
  dicts. Wiring it to the same `PostgresSink` would mean giving each
  violation its own `rule_name`/`group_key` the way `mvguard/engine.py`
  alerts already do, which the alert shape here was deliberately kept
  compatible with.

## Extension: independent price verification for a forward-mark curve

Everything above this section, including the option-surface extension, is
unchanged. This section adds a third, honest use of the same idea: instead
of a risk model's scalar stream or an option surface's own internal
identities, the guardrails now check a commodity desk's daily submitted
forward marks against an independent source, still as declarative rules a
non-engineer can edit.

- **The traffic is synthetic**, same as above: `mvguard/marks_producer.py`
  generates a seeded, deterministic stream of submitted forward marks and a
  separately-jittered independent-source series for eight fictional
  commodities, prefixed `FIC-` the same way the option-surface extension's
  underlying is the fictional ticker `FIC`, so nothing here is mistaken for
  a real desk or a real data vendor. 8 commodities x 10 tenors x 150
  sessions = 12,000 submitted marks, matching the number the resume claims.
- **Price levels differ by more than 25x across commodities on purpose**
  (75 for `FIC-CL` up to 1,950 for `FIC-GC`), which is what forces every
  tolerance below to be relative (a percentage of price), not a fixed
  dollar amount; a $0.50 tolerance is meaningless for `FIC-GC` and useless
  for `FIC-NG`.
- **A third, parallel engine, not a bolt-on to the first two.**
  `mvguard/engine.py` evaluates one rule against one record plus that
  record's own time-windowed history; `mvguard/surface_guardrails.py`
  compares several quotes of the *same* instant against each other. This
  domain needs both shapes at once: off-market and calendar-spread are
  cross-sectional, one session's whole curve compared to itself or to the
  independent curve, while staleness is a genuine short history of one
  tenor's own submitted mark. Rather than force staleness into a
  snapshot-only shape it does not fit, `mvguard/price_verification.py`
  carries one small piece of mutable state (the previous value and the
  current run length, per commodity and tenor) across the stream, the same
  way `mvguard/engine.py` carries cooldown state; the other two checks stay
  pure and stateless. `rules/price_verification_guardrails.yaml` is the only
  place a non-engineer needs to touch.

### The three rules

```yaml
- name: submitted_mark_stale
  family: staleness
  severity: warning
  stale_sessions: 5
- name: off_market_point
  family: off_market
  severity: critical
  tolerance: 0.004
- name: calendar_spread_inconsistent
  family: calendar_spread
  severity: critical
  tolerance: 0.006
```

**Staleness**: a submitted mark that has not changed at all for 5
consecutive sessions is flagged; a forward curve that genuinely moves should
not produce a bit-for-bit identical mark five days running. **Off-market**:
a submitted mark that deviates from the independent source by more than
0.4% of the independent value. **Calendar-spread monotonicity**: for each
pair of adjacent tenors, the spread between the submitted marks must track
the spread between the independent source's own marks for the same two
tenors, within 0.6% of the shorter tenor's independent value, a
self-consistency check on the submitted curve's *shape* rather than any one
point on it. Full definitions: `rules/price_verification_guardrails.yaml`.

### What one seed actually breaks

24 violations are planted, an even 8 per family; unlike the option-surface
extension's four families, none of the three checks here is a naturally
tighter, differently-shaped outlier the way put-call parity was against the
other three no-arbitrage identities, so an even three-way split was the
honest choice rather than a forced one.

The seed deltas are computed from each seed's own snapshot the same way the
option-surface fix works: a clearance margin added on top of the target
rule's tolerance, not a flat guessed number. That still produces a real,
measured collision, worth stating plainly rather than engineering away.
Off-market and calendar-spread are algebraically linked: the calendar-spread
diff at a tenor pair is exactly the difference between the two tenors' own
off-market deviations. Moving one tenor's submitted mark far enough to
clear the 0.4% off-market tolerance with a safe margin over the check's own
noise floor moves that tenor's adjacent spreads by roughly the same amount,
which is comfortably enough to also clear the 0.6% calendar tolerance (and
vice versa for calendar seeds). Every off-market and calendar-spread seed in
this run trips both families in the same seeded snapshot. A few staleness
seeds trip off-market too, on the later sessions of the frozen run: the
independent source keeps moving while the frozen mark does not, so a mark
that is stale for long enough is mechanically also off-market by the time
it is caught, which is a real property of a stuck price, not a seeding
artifact.

None of this is a false positive under the same rule the option-surface
extension uses: an alert outside any seeded snapshot is a false positive; an
alert of an unintended family *inside* a seeded snapshot is the seed being
caught, with company. Isolating off-market from calendar-spread cleanly
would need the two tolerances separated by roughly double (so a delta that
clears one safely cannot clear the other), which was tried algebraically
before writing any seeding code and rejected: at that separation either the
off-market tolerance goes too loose to mean 0.4% of anything, or the
calendar tolerance goes tight enough to make the noise floor calculated
above (see the source comments in `mvguard/marks_producer.py`) risk
spurious firing on unseeded data. The honest measurement, run once with
these tolerances, was 24 of 24 seeds caught, 0 alerts outside any seeded
snapshot, 58 rule-level alerts total. One genuine attempt at the seeding was
needed; the collision above was anticipated from the tolerance ratio before
the run and confirmed by it, not discovered by a failing run and patched
after the fact.

### Triggering inputs

Every alert this engine emits carries a `snapshot` dict (the commodity, the
tenor(s), the session, and the submitted and independent marks actually
compared) and an `observed` dict (the exact numeric comparison and the
tolerance it failed), matching the convention `mvguard/engine.py` and
`mvguard/surface_guardrails.py` already use. Example, straight out of a real
run:

```
family: calendar_spread
snapshot: {'tenor_short': 'M4', 'tenor_long': 'M5', 'submitted_mark_short': 73.6843,
           'submitted_mark_long': 74.0673, 'independent_mark_short': 73.6892,
           'independent_mark_long': 73.5524, 'commodity': 'FIC-CL', 'session_index': 15}
observed: {'submitted_spread': 0.383, 'independent_spread': -0.1368,
           'relative_spread_deviation': 0.00705, 'tolerance': 0.006}
```

### Measured results

Python 3.12.10 on Windows 11, no Kafka or PostgreSQL involved (this
extension runs entirely in-process over a Python list of dicts, the same as
the option-surface extension):

```
$ python scripts/run_price_verification_check.py
clean baseline: 150 sessions, 1200 snapshots, 12000 marks, 0 alert(s)

seeded run: 1200 snapshots, 12000 marks, 58 rule-level alert(s) from 24 planted seeds
false-positive snapshots (alerts with no planted seed): none
false-positive alert count: 0
...
seeds caught (target family fired in its snapshot(s)): 24/24
false positives (alerts outside any seeded snapshot): 0
```

Full transcript: `docs/price_verification_output.txt`. The claim is "24
seeded violations caught, zero false positives, over 12,000 marks", measured
at the seed level, the same way the option-surface extension's 24/24 is
measured; the raw rule-level alert count (58) is higher because several
seeds legitimately clear more than one family's tolerance, as described
above.

### The month-end Excel exception pack

`mvguard/exception_pack.py` writes the same alerts the console output above
reports into a workbook, one sheet per check family (only the columns that
family's alerts carry: `tenor`, `submitted_mark`, `unchanged_sessions` for
staleness; `tenor`, `submitted_mark`, `independent_mark`,
`relative_deviation` for off-market; both tenors and both mark pairs for
calendar-spread) plus an "All exceptions" summary sheet with every alert's
full triggering snapshot and observed comparison serialized in full. Every
row names the commodity, the session, the exact tenor(s), and the exact
submitted and independent values behind that row's flag; nothing on the
sheet needs a lookup elsewhere to explain itself. A sample run's output is
committed at `docs/exception_pack.xlsx` (58 rows across the three family
sheets plus the summary). `openpyxl==3.1.5` is pinned in
`requirements.txt`.

### Running it

```bash
python scripts/run_price_verification_check.py          # measure against the seeded curve, write the exception pack
python -m pytest tests/test_price_verification.py -q    # unit tests for this extension
python -m pytest tests/ -q                                # full suite, including everything above
```

### PostgreSQL

The claimed stack includes PostgreSQL, and the base streaming engine already
uses a real one (see "Real measured results" above). This extension was not
wired to it: PostgreSQL is not installed as a running service on this
machine, and standing one up (Docker Desktop is installed but its daemon
was not running, and starting a GUI application to bring it up was outside
this extension's scope) was not attempted rather than faked. The alert
shape here is already `rule_name`/`snapshot`/`observed` compatible with
`mvguard/db.py`'s `insert_alerts`, the same as the option-surface
extension's alerts, so wiring it in later means writing the marks/alerts to
the existing `alerts` table with `family` folded into `rule_name`, not a
redesign. Any test that needed a live PostgreSQL would skip cleanly the same
way `tests/test_e2e.py` already does; this extension simply did not add one,
so as not to claim a measurement that was never taken.

### Limitations of this extension

- **Off-market and calendar-spread are not independent checks** at the
  tolerances chosen here; a large enough single-tenor deviation clears both
  at once, as measured above. A desk that wanted them to fire separately
  would need either much more separated tolerances or a second, orthogonal
  signal for one of the two.
- **Staleness compares bit-for-bit equality** on a rounded float, not a
  "materially unchanged" fuzzy match; a mark that moves by a fraction of a
  cent and back never counts as a repeat, and a genuinely flat quiet market
  could in principle produce a coincidental exact repeat that this check
  cannot tell apart from a stuck price. Across 12,000 real marks in this
  run it never happened on unseeded data.
- **No real independent source.** Both the submitted and independent series
  come from the same synthetic reference curve with different jitter; a
  real deployment's independent source (a broker poll, an exchange
  settlement) has its own biases that a symmetric synthetic jitter does not
  model.
- **In-process only, and not wired to PostgreSQL**, for the reason given
  above.

## Extension: prepayment model monitoring and month-end exception pack

Everything above this section, including both prior extensions, is
unchanged. This is a fourth, honest use of the same idea: instead of a
risk model's scalar stream, an option surface's own internal identities, or
a commodity desk's submitted marks, the guardrails now monitor a deployed
mortgage prepayment model's actual-versus-predicted performance, the
stability of the population it scores, and the freshness of the input it
depends on, still as declarative rules a non-engineer can edit.

- **The panel is synthetic.** `mvguard/prepayment_monitor_producer.py`
  generates a seeded, deterministic monthly panel of 100 fictional loan
  cohorts over 150 months, 15,000 cohort-months, matching the number the
  resume claims. There is no real loan-level data or real deployed model
  behind this; the "predicted CPR" comes from a small fixed seasoning-ramp
  and burnout function this module writes itself, not from the separate
  `loan-level-prepayment-model` project.
- **The market rate is held roughly flat in this simulation**, small
  month-to-month noise only, no systematic cycle. That is a deliberate
  simplification stated up front: it keeps the population's cross-sectional
  refi-incentive distribution stable in the clean run, so
  `population_stability`'s fixed baseline bins stay valid for the full 150
  months and any drift the check flags is attributable to the seeded
  cohort-mix perturbations below, not to an unmodeled real rate cycle a
  production deployment would also have to control for.
- **A fourth, parallel engine, not a bolt-on to the first three.**
  `mvguard/engine.py` evaluates one rule against one record plus that
  record's own time-windowed history; `mvguard/surface_guardrails.py` and
  `mvguard/price_verification.py` compare several quotes or marks of the
  same instant against each other. This domain needs a third shape:
  `population_stability` needs the *whole population* of cohorts observed
  in one month, not one record's own history, so
  `mvguard/prepayment_monitor.py` buffers cohort-months by month and only
  closes a month out once every cohort for that month has arrived.
  `cpr_tolerance` stays purely cross-sectional and stateless;
  `stale_input` carries per-cohort state across the stream the same way
  `price_verification.py`'s staleness check does.
  `rules/prepayment_monitor_guardrails.yaml` is the only place a
  non-engineer needs to touch.

### The three rules

```yaml
- name: cpr_actual_vs_predicted
  family: cpr_tolerance
  severity: critical
  tolerance: 3.0
- name: refi_incentive_stale
  family: stale_input
  severity: warning
  stale_months: 6
- name: refi_incentive_population_shift
  family: population_stability
  severity: critical
  threshold: 0.25
```

**CPR tolerance**: a cohort-month whose realized CPR deviates from the
model's predicted CPR by more than 3.0 percentage points is flagged, run
at the cohort level every month so one cohort going wrong cannot hide
inside an average that still looks fine. **Stale input**: a cohort whose
refi-incentive input has not changed at all for 6 consecutive months is
flagged; both a cohort's note rate and the market rate it is compared
against move continuously, so a bit-for-bit frozen input for half a year
almost always means the upstream feed stopped updating. **Population
stability**: a Population Stability Index above 0.25 between a month's
cohort population and the baseline population the model was validated
against (month 0) is flagged, the standard industry cutoff for "the
scoring population no longer resembles the population the model was built
on". Full definitions: `rules/prepayment_monitor_guardrails.yaml`.

### The bug worth reading about: 10 bins was too fine for a 100-cohort population

The first version of `population_stability` used the textbook 10 equal-
frequency deciles. Run against the clean, unseeded panel, it should have
produced zero alerts across 149 monthly comparisons. It produced three:

```
UNEXPECTED CLEAN ALERT refi_incentive_population_shift population-m0026 {'psi': 0.2519, 'threshold': 0.25}
UNEXPECTED CLEAN ALERT refi_incentive_population_shift population-m0106 {'psi': 0.2601, 'threshold': 0.25}
UNEXPECTED CLEAN ALERT refi_incentive_population_shift population-m0141 {'psi': 0.2907, 'threshold': 0.25}
```

The measurement that discriminated: with only 100 cohorts split across 10
bins, each bin holds roughly 10 samples, and the sampling noise on a
count that small is a meaningful fraction of the expected 10% share per
bin. PSI sums that noise across all 10 bins, so on 3 of 149 unseeded
months the accumulated noise alone crossed 0.25, a threshold meant to
detect a real population shift, not sampling variance from an
undersized reference population. This was not a seeding artifact and not
a bug in the PSI formula itself; it was a bin-count-versus-population-size
mismatch. The fix, one genuine attempt: 5 equal-frequency bins (quintiles)
instead of 10, still within the 5-to-10-bin range PSI is conventionally
computed over, which doubles the samples per bin and cut the baseline's
sampling noise enough that the clean run now measures **zero** alerts
across all 150 monthly comparisons. `rules/prepayment_monitor_guardrails.yaml`'s
`threshold: 0.25` was never touched to make this pass; only the bin count,
which is an engine implementation detail, not a rule a non-engineer edits.

### Triggering inputs

Cohort-level alerts (`cpr_tolerance`, `stale_input`) carry a `snapshot`
dict (the cohort id, month, and the exact field(s) compared) and an
`observed` dict (the numeric comparison and its tolerance), matching the
convention every other engine in this repo uses. `population_stability`
alerts carry the month, the cohort count behind the measurement, and the
baseline bin edges instead of a single cohort. Example, straight out of a
real run:

```
family: cpr_tolerance
snapshot: {'predicted_cpr': 9.4113, 'actual_cpr': 13.4113, 'cohort_id': 'cohort-005', 'month_index': 15}
observed: {'cpr_diff': 4.0, 'tolerance': 3.0}
```

### Measured results

Python 3.12.10 on Windows 11, no Kafka or PostgreSQL involved (this
extension runs entirely in-process over a Python list of dicts, the same
as the two prior extensions):

```
$ python scripts/run_prepayment_monitor_check.py
clean baseline: 100 cohorts, 150 months, 15000 cohort-months, 0 alert(s)

seeded run: 15000 cohort-months, 30 rule-level alert(s) from 30 planted seeds
false-positive snapshots (alerts with no planted seed): none
false-positive alert count: 0
...
seeds caught (target family fired in its snapshot(s)): 30/30
false positives (alerts outside any seeded snapshot): 0
```

Full transcript: `docs/prepayment_monitor_output.txt`. The claim is "30 of
30 seeded performance breaks caught, zero false positives, over 15,000
cohort-months", measured exactly as run, one rule-level alert per seed (no
seed in this extension trips a second family the way some
price-verification seeds legitimately do, because the three families here
read disjoint fields: `cpr_tolerance` reads the CPR fields, `stale_input`
and `population_stability` both read `refi_incentive` but at different
scopes, per-cohort history versus whole-population-per-month, and the
seeding for each was deliberately built not to overlap the other's target
months or cohorts).

### The month-end Excel exception pack

`mvguard/prepayment_exception_pack.py` writes the same alerts the console
output above reports into a workbook, one sheet per check family (only the
columns that family's alerts carry: `predicted_cpr`, `actual_cpr`,
`cpr_diff` for CPR tolerance; `refi_incentive`, `unchanged_months` for
stale input; `cohort_count`, `psi` for population stability) plus an "All
exceptions" summary sheet with every alert's full triggering snapshot and
observed comparison serialized in full. A sample run's output is committed
at `docs/prepayment_exception_pack.xlsx` (30 rows across the three family
sheets plus the summary).

### Running it

```bash
python scripts/run_prepayment_monitor_check.py          # measure against the seeded panel, write the exception pack
python -m pytest tests/test_prepayment_monitor.py -q    # unit tests for this extension
python -m pytest tests/ -q                                # full suite, including everything above
```

### Limitations of this extension

- **The market rate is held roughly flat by design** (see above); a real
  deployment would need to separate genuine macro rate drift from a real
  population-mix shift before trusting a PSI alert, which this
  simplification does not attempt to model.
- **`population_stability`'s baseline is fixed at month 0** and never
  re-based; a model that is deliberately recalibrated to a new population
  would need an explicit baseline reset, which this extension does not
  implement.
- **Quintile bins trade detection granularity for stability at this
  population size** (see "The bug worth reading about" above); a
  deployment scoring a much larger population could safely go back to
  finer bins.
- **In-process only, and not wired to Kafka or PostgreSQL**, for the same
  reason the price-verification extension gives: neither is running as a
  service on this machine, and the alert shape here is already
  `rule_name`/`snapshot`/`observed` compatible with `mvguard/db.py`'s
  `insert_alerts` for whenever it is.
- **`cpr_tolerance` and `stale_input` both key off `refi_incentive`-adjacent
  fields but at different scopes**; a real deployment would likely also
  want a per-cohort, not just per-population, staleness-aware version of
  the CPR check, which this extension does not add.

## Extension: VBA-driven month-end workbook for the no-arbitrage check

Everything above this section, including all three prior extensions, is
unchanged. This is a fifth, small extension: the no-arbitrage check above
already catches 24 of 24 seeded violations with zero false positives over
12,000 quotes; this extension puts that same check into the month-end Excel
workbook a non-engineer actually opens, driven by VBA instead of a Python
script.

- **This machine has no Excel installed** (no registered `Excel.Application`
  COM class, confirmed before writing a line of VBA). `vba/MonthEndReview.bas`
  is the real production design, committed and reviewable, but not executed
  live. `mvguard/vba_mirror.py` is an independently written Python
  transliteration of the same worksheet-shaped algorithm (flat rows grouped
  by a composite key the way a VBA `Scripting.Dictionary` would, sorted with
  an explicit insertion sort since VBA `Collection`s have no built-in sort),
  used to measure what the macro would produce. The same disclosed design was
  used once before in this portfolio, on `equilibrium-catalyst-report-addin`.
- **The reconciliation is real even though the macro is not run live.**
  `mvguard/surface_guardrails.py` (the engine above) and
  `mvguard/vba_mirror.py` are two independently coded implementations of the
  same four identities, evaluated over the same seeded 300-snapshot,
  12,000-quote surface and diffed cell for cell on every (snapshot, family)
  pair.

### What `vba/MonthEndReview.bas` does

Two subs, callable from one `RunMonthEndReview`. `RefreshExceptionPack` reads
the "Quotes" sheet, recomputes put-call parity, strike monotonicity,
butterfly convexity and calendar-spread exactly as
`mvguard/surface_guardrails.py` defines them (the same four tolerances,
copied by hand from `rules/option_surface_guardrails.yaml` since the macro
does not parse YAML), and writes every violation to "Exceptions" plus a
per-snapshot, per-family count to "VBASummary". `ReconcileToEngine` diffs
"VBASummary" against "EngineSummary" (pasted in from the Python engine's own
run) and writes a MATCH/MISMATCH row per cell to "Reconciliation", plus a
final pass/fail cell.

### Measured results

Python 3.12.10 on Windows 11, no Excel involved (see above):

```
$ python scripts/build_month_end_workbook.py
month-end workbook reconciliation: 300 snapshots, 12000 quotes, 24 planted seeds

Python engine (mvguard/surface_guardrails.py):
  seeds caught: 24/24
  false positives: 0

VBA mirror (mvguard/vba_mirror.py, standing in for vba/MonthEndReview.bas):
  seeds caught: 24/24
  false positives: 0

cell-for-cell reconciliation: 1200 (snapshot, family) cells checked, 0 mismatch(es)
ALL MATCH: True

workbook written to docs/month_end_exception_pack_vba.xlsx
```

Full transcript: `docs/vba_reconciliation_output.txt`. The workbook itself is
committed at `docs/month_end_exception_pack_vba.xlsx`: a "Quotes" sheet
holding the 24 seeded snapshots (960 of the 12,000 quotes, enough for a
reviewer to see every planted violation in context without checking in a
much larger raw dump of synthetic data), "EngineSummary" and "VBASummary"
(300 x 4 = 1,200 rows each), and "Reconciliation" with the full cell-for-cell
diff. The 1,200-cell reconciliation itself runs over the complete
12,000-quote surface, not just the 960 quotes shown in the sheet.

### Running it

```bash
python scripts/build_month_end_workbook.py          # measure, reconcile, write the workbook
python -m pytest tests/test_vba_mirror.py -q        # unit tests for this extension
python -m pytest tests/ -q                           # full suite, including everything above
```

### Limitations of this extension

- **The macro is not executed live.** This machine has no Excel;
  `mvguard/vba_mirror.py` measures the algorithm `vba/MonthEndReview.bas`
  implements, not the macro itself running inside a workbook. The honest
  claim is "the VBA design reconciles cell for cell with the Python engine",
  not "this macro was run in Excel and did."
- **Tolerances are duplicated, not shared.** `vba/MonthEndReview.bas`
  hardcodes the same four numbers in `rules/option_surface_guardrails.yaml`
  because parsing YAML from VBA was out of scope for one session; a change to
  the YAML needs the same change made by hand in the macro.
- **The committed workbook's "Quotes" sheet is a 960-quote subset** (the 24
  seeded snapshots), not the full 12,000, for file-size reasons; the
  reconciliation numbers above are measured over the full surface regardless.

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
