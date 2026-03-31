#!/bin/bash
set -euo pipefail

# cc-matrix-display uninstaller
# Removes the server service, hooks, and config.

CONFIG_DIR="$HOME/.cc-matrix"
CLAUDE_DIR="$HOME/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
SETTINGS="$CLAUDE_DIR/settings.json"
PLIST_NAME="com.cc-matrix-display.server"
PLIST_DIR="$HOME/Library/LaunchAgents"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }

# ---------------------------------------------------------------------------
# Stop and remove service
# ---------------------------------------------------------------------------

if [ "$(uname)" = "Darwin" ]; then
    if launchctl print "gui/$(id -u)/$PLIST_NAME" >/dev/null 2>&1; then
        launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true
        info "Stopped launchd service"
    fi
    rm -f "$PLIST_DIR/$PLIST_NAME.plist"
    info "Removed launchd plist"
elif command -v systemctl >/dev/null 2>&1; then
    systemctl --user stop cc-matrix-display.service 2>/dev/null || true
    systemctl --user disable cc-matrix-display.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/cc-matrix-display.service"
    systemctl --user daemon-reload 2>/dev/null || true
    info "Removed systemd service"
fi

# ---------------------------------------------------------------------------
# Remove hooks from settings.json
# ---------------------------------------------------------------------------

if [ -f "$SETTINGS" ] && command -v jq >/dev/null 2>&1; then
    cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"
    info "Backed up settings.json"

    # Remove hook entries that reference our scripts
    jq '
      if .hooks.Stop then .hooks.Stop |= map(select(.hooks | all(.command | contains("matrix-stop.sh") | not))) else . end |
      if .hooks.PreToolUse then .hooks.PreToolUse |= map(select(.hooks | all(.command | contains("matrix-resume.sh") | not))) else . end |
      if .hooks.PostToolUse then .hooks.PostToolUse |= map(select(.hooks | all(.command | contains("matrix-complete.sh") | not))) else . end
    ' "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
    info "Removed hooks from settings.json"
else
    warn "Could not update settings.json (missing jq or file). Remove matrix hooks manually."
fi

# ---------------------------------------------------------------------------
# Remove hook scripts
# ---------------------------------------------------------------------------

rm -f "$HOOKS_DIR/matrix-stop.sh" "$HOOKS_DIR/matrix-resume.sh" "$HOOKS_DIR/matrix-complete.sh"
info "Removed hook scripts"

# ---------------------------------------------------------------------------
# Remove config
# ---------------------------------------------------------------------------

if [ -d "$CONFIG_DIR" ]; then
    rm -rf "$CONFIG_DIR"
    info "Removed config directory ($CONFIG_DIR)"
fi

# ---------------------------------------------------------------------------
# Clean up flag files
# ---------------------------------------------------------------------------

rm -f /tmp/claude-waiting-* /tmp/claude-pending-*
info "Cleaned up flag files"

echo ""
info "Uninstall complete."
echo "  Note: The Matrix Portal still has its code on the CIRCUITPY drive."
echo "  To reset it, reflash CircuitPython or delete code.py from the drive."
echo ""
