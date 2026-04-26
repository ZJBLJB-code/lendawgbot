#!/usr/bin/env bash
# Mirror the latest quant-bot day0 verdicts (and live event logs, if present)
# into data/quant_bot_journal/ so CI can build without needing the
# full quant-bot worktree.
#
# Usage:
#   bash scripts/sync_quant_bot.sh
#   QB_ROOT=/path/to/quant-bot bash scripts/sync_quant_bot.sh
#
# This is the manual half of "Option A" data sync (see README.md
# Real data section). Run it whenever quant-bot emits a fresh verdict, then
# git-commit the result so the next cron build picks it up.
set -euo pipefail

QB_ROOT="${QB_ROOT:-$HOME/quant-bot}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="$HERE/../data/quant_bot_journal"

if [ ! -d "$QB_ROOT" ]; then
  echo "QB_ROOT not found: $QB_ROOT" >&2
  echo "set QB_ROOT=/path/to/quant-bot and re-run." >&2
  exit 1
fi

mkdir -p "$DEST/day0" "$DEST/live"

# Verdict files — small, always copy.
if compgen -G "$QB_ROOT/journal/day0/verdict_*.json" > /dev/null; then
  cp -v "$QB_ROOT"/journal/day0/verdict_*.json "$DEST/day0/"
else
  echo "no verdict_*.json under $QB_ROOT/journal/day0 (skipping)"
fi

# Live event logs — SKIPPED by default in the public repo because
# event payloads contain account_id ("DUP1234567") which is PII.
# Override with SYNC_SKIP_LIVE=0 if you trust the data.
if [ "${SYNC_SKIP_LIVE:-1}" = "0" ] && compgen -G "$QB_ROOT/journal/live/*.jsonl" > /dev/null; then
  # newest 7 by name (which encodes date)
  ls -1 "$QB_ROOT"/journal/live/*.jsonl | sort -r | head -n 7 | while read -r f; do
    cp -v "$f" "$DEST/live/"
  done
else
  echo "no *.jsonl under $QB_ROOT/journal/live (skipping)"
fi

echo
echo "synced from $QB_ROOT into $DEST"
echo "next: git add data/quant_bot_journal && git commit"
