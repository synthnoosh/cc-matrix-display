#\!/bin/bash
# Claude Code PreToolUse hook — clears "waiting" flag when Claude starts working
# Removes the flag file written by matrix-stop.sh.
# Installed by: host/install.sh
#
# Hook input (stdin JSON): {"session_id": "...", "hook_event_name": "PreToolUse", ...}

session_id=$(cat | jq -r '.session_id // empty' 2>/dev/null)
[ -n "$session_id" ] && rm -f "/tmp/claude-waiting-${session_id}"
exit 0
