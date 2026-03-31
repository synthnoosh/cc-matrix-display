#!/bin/bash
# Claude Code Stop hook — marks session as "waiting for input"
# Also clears any pending flag since the turn is fully complete.
# Installed by: host/install.sh
#
# Hook input (stdin JSON): {"session_id": "...", "hook_event_name": "Stop", ...}

session_id=$(cat | jq -r '.session_id // empty' 2>/dev/null)
if [ -n "$session_id" ]; then
  touch "/tmp/claude-waiting-${session_id}"
  rm -f "/tmp/claude-pending-${session_id}"
fi
exit 0
