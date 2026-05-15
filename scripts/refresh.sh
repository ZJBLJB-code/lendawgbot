#!/usr/bin/env bash
# refresh.sh — the "tell Claude to refresh" workflow.
#
# v2 architecture (canonical publisher):
#   1. sync_quant_bot.sh runs tools/publisher.py and writes data/dashboard.json
#   2. build.py reads data/dashboard.json (--from canonical) and emits dist/
#
# Usage:
#   bash scripts/refresh.sh                # local: sync + build, no commit
#   bash scripts/refresh.sh --deploy       # sync + build + commit + push
#   bash scripts/refresh.sh --diff-only    # show what changed, no build
#   QB_ROOT=/path/to/quant-bot bash scripts/refresh.sh --deploy
#
# Exit codes:
#   0  ok (built, optionally pushed)
#   1  sync failed (publisher error / quant-bot not reachable)
#   2  build failed
#   3  git step failed (when --deploy)

set -euo pipefail

# --- Args -------------------------------------------------------------------
DEPLOY=0
DIFF_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --deploy) DEPLOY=1 ;;
    --diff-only) DIFF_ONLY=1 ;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $arg" >&2; exit 1 ;;
  esac
done

# --- Locate paths -----------------------------------------------------------
HERE="$(cd "$(dirname "$0")" && pwd)"
TOMCASH="$(cd "$HERE/.." && pwd)"
# lendawgbot is now its own repo (was a subdir of zach-pocs in v1). REPO_ROOT
# and TOMCASH are the same directory now — keep the alias for readability.
REPO_ROOT="$TOMCASH"
QB_ROOT="${QB_ROOT:-$HOME/quant-bot}"
CANONICAL="$TOMCASH/data/dashboard.json"

cd "$TOMCASH"

# --- Pretty logging ---------------------------------------------------------
ts() { date +%H:%M:%S; }
say() { printf "[%s] %s\n" "$(ts)" "$*"; }
ok()  { printf "[%s] ✓ %s\n" "$(ts)" "$*"; }
err() { printf "[%s] ✗ %s\n" "$(ts)" "$*" >&2; }

START_TS=$(date +%s)
say "refresh start  qb_root=$QB_ROOT"

# --- 1. Sync via the canonical publisher ------------------------------------
say "sync (canonical publisher) …"
if ! bash "$HERE/sync_quant_bot.sh" > /tmp/lendawg-sync.log 2>&1; then
  err "sync failed — see /tmp/lendawg-sync.log"
  cat /tmp/lendawg-sync.log
  exit 1
fi

# Show the canonical artifact's headline facts.
if [ -f "$CANONICAL" ]; then
  python3 - "$CANONICAL" <<'PY' || true
import json, sys
p = json.load(open(sys.argv[1]))
prov = p.get("_provenance") or {}
v = p.get("verdict") or {}
print(f"  canonical: mode={p.get('mode')} schema={p.get('schema_version')} "
      f"source={prov.get('source_detail')}")
if v:
    print(f"  verdict:   {v.get('verdict')} ({v.get('gates_passed')}/5 gates)")
PY
  ok "synced  canonical=$CANONICAL"
else
  err "no canonical dashboard.json after sync — aborting"
  exit 1
fi

if [ "$DIFF_ONLY" = "1" ]; then
  cd "$REPO_ROOT"
  git diff --stat data/dashboard.json || true
  exit 0
fi

# --- 2. Build the dashboard from the canonical artifact ---------------------
say "build (--from canonical) …"
if ! python3 "$TOMCASH/scripts/build.py" \
        --from canonical \
        --root "$CANONICAL" \
        --also-demo \
        --publisher local \
        > /tmp/lendawg-build.log 2>&1; then
  err "build failed — see /tmp/lendawg-build.log"
  tail -40 /tmp/lendawg-build.log >&2
  exit 2
fi

# Pull a couple of provenance facts out of the build log
SOURCE_LINE=$(grep -E "^Built " /tmp/lendawg-build.log | head -1 || true)
ok "built  $SOURCE_LINE"

# Read _status.json so the operator sees the truth one place
STATUS="$TOMCASH/dist/_status.json"
if [ -f "$STATUS" ]; then
  python3 - "$STATUS" <<'PY' || true
import json, sys
s = json.load(open(sys.argv[1]))
warn = s.get("warnings") or []
errs = s.get("errors") or []
ok = "✓" if s["ok"] else "✗"
print(f"  status: {ok} mode={s['mode']} source={s['last_build_source']} age={s.get('data_age_hours','?')}h")
for w in warn:
    print(f"  warn: {w}")
for e in errs:
    print(f"  err:  {e}")
PY
fi

# --- 3. (optional) commit + push so the public site picks it up -------------
if [ "$DEPLOY" = "1" ]; then
  cd "$REPO_ROOT"
  if [ -z "$(git status --porcelain data/dashboard.json dist/)" ]; then
    ok "no canonical/dist changes — skipping commit"
  else
    say "git: staging data/dashboard.json"
    git add data/dashboard.json || { err "git add failed"; exit 3; }
    # Don't commit dist/ — it's gitignored as a build artifact. CI rebuilds.
    BR=$(git rev-parse --abbrev-ref HEAD)
    MSG="refresh: publish canonical dashboard ($(date -u +%FT%TZ))"
    git commit -m "$MSG" || { err "nothing to commit"; }
    say "git: pushing to origin/$BR …"
    git push origin "$BR" || { err "push failed"; exit 3; }
    ok "deployed — CI will rebuild + publish in ~1-2 min"
  fi
fi

ELAPSED=$(( $(date +%s) - START_TS ))
ok "done in ${ELAPSED}s"
echo
echo "→ open  http://localhost:8765  (local preview)"
if [ "$DEPLOY" = "1" ]; then
  echo "→ live  https://zjbljb-code.github.io/lendawgbot/  (~1-2 min after CI)"
fi
