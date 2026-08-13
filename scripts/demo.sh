#!/usr/bin/env bash
# End-to-end demo: lint the rules, publish synthetic traffic, evaluate it, report.
#
# Assumes a Kafka broker and PostgreSQL are already running with the defaults in
# mvguard/config.py. See the README for standing those up from scratch.
#
# The topic must have exactly one partition: global event-time windows need a
# totally ordered stream, and Kafka only orders within a partition.

set -euo pipefail

COUNT="${1:-12000}"

echo "==> linting rules (parse + dry run)"
python -m mvguard.cli lint

echo
echo "==> preparing schema"
python -m mvguard.cli init-db --reset-alerts

echo
echo "==> producing ${COUNT} synthetic scoring records"
python -m mvguard.cli produce --count "${COUNT}"

echo
echo "==> evaluating the stream"
python -m mvguard.cli run --idle-timeout 8 --console-severity critical

echo
echo "==> what landed in postgres"
python -m mvguard.cli report --limit 3
