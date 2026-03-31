#!/bin/bash
# Claude Code PreToolUse hook — clears "waiting" flag, writes "pending" flag.
# Pending flag tracks tool-in-progress; if it lingers >10s, a permission prompt
# is likely blocking the session.
# Installed by: host/install.sh
#
# Hook input (stdin JSON): {"session_id": "...", "hook_event_name": "PreToolUse", ...}

session_id=$(cat | jq -r '.session_id // empty' 2>/dev/null)
if [ -n "$session_id" ]; then
  rm -f "/tmp/claude-waiting-${session_id}"
  touch "/tmp/claude-pending-${session_id}"
fi
exit 0
