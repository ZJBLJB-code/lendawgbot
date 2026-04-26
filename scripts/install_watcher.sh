#!/usr/bin/env bash
# Install a launchd agent that auto-runs refresh.sh --deploy whenever quant-bot
# writes a new verdict or live event log. Substitutes the @TOKEN@ placeholders
# in the plist template with absolute paths, then loads the agent.
#
# Usage:
#   LENDAWGBOT_DIR=$HOME/lendawgbot QUANT_BOT_DIR=$HOME/quant-bot \
#     bash scripts/install_watcher.sh
#
# Uninstall:
#   bash scripts/install_watcher.sh --uninstall
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
TEMPLATE="$HERE/com.lendawgbot.watcher.plist.template"
PLIST_DST="$HOME/Library/LaunchAgents/com.lendawgbot.watcher.plist"

if [ "${1:-}" = "--uninstall" ]; then
  if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "✓ uninstalled"
  fi
  exit 0
fi

: "${LENDAWGBOT_DIR:?set LENDAWGBOT_DIR to absolute path of this repo's checkout}"
: "${QUANT_BOT_DIR:?set QUANT_BOT_DIR to absolute path of the quant-bot repo}"

[ -f "$TEMPLATE" ] || { echo "template not found: $TEMPLATE"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents"
[ -f "$PLIST_DST" ] && launchctl unload "$PLIST_DST" 2>/dev/null || true

sed -e "s|@LENDAWGBOT_DIR@|$LENDAWGBOT_DIR|g" \
    -e "s|@QUANT_BOT_DIR@|$QUANT_BOT_DIR|g" \
    "$TEMPLATE" > "$PLIST_DST"

launchctl load "$PLIST_DST"

echo "✓ installed and loaded:"
echo "  $PLIST_DST"
echo "Log: tail -f /tmp/lendawgbot-watcher.log"
