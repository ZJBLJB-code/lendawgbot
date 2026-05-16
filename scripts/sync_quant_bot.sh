#!/usr/bin/env bash
# sync_quant_bot.sh — produce data/dashboard.json from a local quant-bot.
#
# v2 architecture: this script no longer copies raw quant-bot files. Instead
# it invokes the canonical publisher, which reads quant-bot's raw artifacts
# and emits a single schema-validated, PII-free dashboard JSON. The schema
# allow-list (tools/publisher_schema.py) is what guarantees PII safety —
# there is no separate "sanitize" pass.
#
# Usage:
#   bash scripts/sync_quant_bot.sh
#   QB_ROOT=/path/to/quant-bot bash scripts/sync_quant_bot.sh
#   SYNC_SKIP_LIVE=1 bash scripts/sync_quant_bot.sh   # force DAY0 mode
#
# Behavior:
#   - QB_ROOT defaults to $HOME/quant-bot
#   - SYNC_SKIP_LIVE=1 passes --mode DAY0 to the publisher, ignoring live JSONL.
#   - On publisher failure: hard exit (NO silent fallback). The dashboard
#     should NEVER drift forward on stale data because nobody noticed.
#
# Exit codes:
#   0  success
#   1  QB_ROOT not found / not a directory
#   2  publisher hard failure
set -euo pipefail

QB_ROOT="${QB_ROOT:-$HOME/quant-bot}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TOMCASH="$(cd "$HERE/.." && pwd)"
OUT="$TOMCASH/data/dashboard.json"
PUBLISHER="$TOMCASH/tools/publisher.py"

if [ ! -d "$QB_ROOT" ]; then
  echo "QB_ROOT not found: $QB_ROOT" >&2
  echo "set QB_ROOT=/path/to/quant-bot and re-run." >&2
  exit 1
fi
if [ ! -f "$PUBLISHER" ]; then
  echo "publisher not found: $PUBLISHER" >&2
  exit 1
fi

MODE_FLAG=""
if [ "${SYNC_SKIP_LIVE:-0}" != "0" ]; then
  echo "SYNC_SKIP_LIVE=$SYNC_SKIP_LIVE — forcing publisher --mode DAY0"
  MODE_FLAG="--mode DAY0"
fi

mkdir -p "$(dirname "$OUT")"

echo "publishing  qb_root=$QB_ROOT  out=$OUT"
# Run the publisher. Any failure (PublisherError, missing artifacts, …)
# bubbles up via `set -e`. We deliberately do NOT fall back to a stale
# previous artifact — silent staleness is the worst failure mode.
if ! python3 "$PUBLISHER" --quant-bot-root "$QB_ROOT" --out "$OUT" $MODE_FLAG; then
  echo "publisher failed — refusing to leave stale dashboard.json in place" >&2
  exit 2
fi

# Surface the headline facts so the operator can sanity-check at a glance.
python3 - "$OUT" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
prov = p.get("_provenance") or {}
print(f"  schema_version = {p.get('schema_version')}")
print(f"  mode           = {p.get('mode')}")
print(f"  source_detail  = {prov.get('source_detail')}")
print(f"  data_as_of     = {prov.get('data_as_of')}")
v = p.get("verdict") or {}
if v:
    print(f"  verdict        = {v.get('verdict')} ({v.get('gates_passed')}/5 gates)")
PY

echo
echo "wrote $OUT"
echo "next: bash scripts/refresh.sh  (or commit data/dashboard.json)"
