#!/bin/bash
# Claude Code PostToolUse hook — clears "pending" flag after tool completes.
# If the pending flag lingered >10s before this fires, the server will have
# reported the session as "blocked" (permission prompt).
# Installed by: host/install.sh
#
# Hook input (stdin JSON): {"session_id": "...", "hook_event_name": "PostToolUse", ...}

session_id=$(cat | jq -r '.session_id // empty' 2>/dev/null)
[ -n "$session_id" ] && rm -f "/tmp/claude-pending-${session_id}"
exit 0
