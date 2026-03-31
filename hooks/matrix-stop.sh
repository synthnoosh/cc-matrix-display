#\!/bin/bash
# Claude Code Stop hook — marks session as "waiting for input"
# Writes a flag file that the cc-matrix-display server checks.
# Installed by: host/install.sh
#
# Hook input (stdin JSON): {"session_id": "...", "hook_event_name": "Stop", ...}

session_id=$(cat | jq -r '.session_id // empty' 2>/dev/null)
[ -n "$session_id" ] && touch "/tmp/claude-waiting-${session_id}"
exit 0
