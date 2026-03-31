#!/bin/bash
set -euo pipefail

# cc-matrix-display installer
# Sets up the host server, hooks, and autostart on macOS.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_DIR="$HOME/.cc-matrix"
CLAUDE_DIR="$HOME/.claude"
HOOKS_DIR="$CLAUDE_DIR/hooks"
SETTINGS="$CLAUDE_DIR/settings.json"
PLIST_NAME="com.cc-matrix-display.server"
PLIST_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
error() { echo -e "${RED}[x]${NC} $1"; exit 1; }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

info "Checking prerequisites..."

command -v python3 >/dev/null 2>&1 || error "python3 not found"
if ! command -v jq >/dev/null 2>&1; then
    if [ "$(uname)" = "Darwin" ]; then
        error "jq not found (install: brew install jq)"
    else
        error "jq not found (install: sudo apt install jq  or  sudo dnf install jq)"
    fi
fi

if [ ! -d "$CLAUDE_DIR" ]; then
    error "Claude Code not found (~/.claude missing). Install Claude Code first."
fi

info "Prerequisites OK"

# ---------------------------------------------------------------------------
# Generate config
# ---------------------------------------------------------------------------

mkdir -p "$CONFIG_DIR"

if [ -f "$CONFIG_DIR/config.json" ]; then
    warn "Config already exists at $CONFIG_DIR/config.json — skipping"
    SECRET=$(jq -r '.secret // empty' "$CONFIG_DIR/config.json")
else
    SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    PORT=${CC_MATRIX_PORT:-8321}

    cat > "$CONFIG_DIR/config.json" <<EOF
{
  "port": $PORT,
  "secret": "$SECRET",
  "bind": "0.0.0.0"
}
EOF
    info "Generated config at $CONFIG_DIR/config.json"
    info "  Port: $PORT"
    info "  Secret: $SECRET"
fi

# ---------------------------------------------------------------------------
# Install hook scripts
# ---------------------------------------------------------------------------

mkdir -p "$HOOKS_DIR"

cp "$REPO_DIR/hooks/matrix-stop.sh" "$HOOKS_DIR/matrix-stop.sh"
cp "$REPO_DIR/hooks/matrix-resume.sh" "$HOOKS_DIR/matrix-resume.sh"
cp "$REPO_DIR/hooks/matrix-complete.sh" "$HOOKS_DIR/matrix-complete.sh"
chmod +x "$HOOKS_DIR/matrix-stop.sh" "$HOOKS_DIR/matrix-resume.sh" "$HOOKS_DIR/matrix-complete.sh"

info "Installed hook scripts to $HOOKS_DIR"

# ---------------------------------------------------------------------------
# Merge hooks into settings.json (non-destructive)
# ---------------------------------------------------------------------------

if [ ! -f "$SETTINGS" ]; then
    error "Claude Code settings.json not found at $SETTINGS"
fi

# Backup
cp "$SETTINGS" "$SETTINGS.bak.$(date +%s)"
info "Backed up settings.json"

STOP_HOOK_CMD="bash $HOOKS_DIR/matrix-stop.sh"
RESUME_HOOK_CMD="bash $HOOKS_DIR/matrix-resume.sh"
COMPLETE_HOOK_CMD="bash $HOOKS_DIR/matrix-complete.sh"

# Check if hooks already installed
if jq -e '.hooks.Stop[]?.hooks[]? | select(.command == "'"$STOP_HOOK_CMD"'")' "$SETTINGS" >/dev/null 2>&1; then
    warn "Stop hook already installed — skipping"
else
    jq '.hooks.Stop += [{"hooks": [{"type": "command", "command": "'"$STOP_HOOK_CMD"'"}]}]' "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
    info "Added Stop hook for matrix-stop.sh"
fi

if jq -e '.hooks.PreToolUse[]?.hooks[]? | select(.command == "'"$RESUME_HOOK_CMD"'")' "$SETTINGS" >/dev/null 2>&1; then
    warn "PreToolUse hook already installed — skipping"
else
    jq '.hooks.PreToolUse += [{"matcher": "", "hooks": [{"type": "command", "command": "'"$RESUME_HOOK_CMD"'"}]}]' "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
    info "Added PreToolUse hook for matrix-resume.sh"
fi

if jq -e '.hooks.PostToolUse[]?.hooks[]? | select(.command == "'"$COMPLETE_HOOK_CMD"'")' "$SETTINGS" >/dev/null 2>&1; then
    warn "PostToolUse hook already installed — skipping"
else
    jq '.hooks.PostToolUse += [{"matcher": "", "hooks": [{"type": "command", "command": "'"$COMPLETE_HOOK_CMD"'"}]}]' "$SETTINGS" > "$SETTINGS.tmp" && mv "$SETTINGS.tmp" "$SETTINGS"
    info "Added PostToolUse hook for matrix-complete.sh"
fi

# ---------------------------------------------------------------------------
# Install launchd plist (macOS)
# ---------------------------------------------------------------------------

if [ "$(uname)" = "Darwin" ]; then
    mkdir -p "$PLIST_DIR" "$LOG_DIR"

    SERVER_PATH="$REPO_DIR/host/server.py"
    CONFIG_PATH="$CONFIG_DIR/config.json"

    cat > "$PLIST_DIR/$PLIST_NAME.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$PLIST_NAME</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(command -v python3)</string>
        <string>$SERVER_PATH</string>
        <string>$CONFIG_PATH</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/cc-matrix-display.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/cc-matrix-display.log</string>
</dict>
</plist>
EOF

    # Stop if already running
    launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true

    # Load and start
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DIR/$PLIST_NAME.plist"

    info "Installed and started launchd service"
    info "  Logs: $LOG_DIR/cc-matrix-display.log"
elif command -v systemctl >/dev/null 2>&1; then
    # --- Linux: systemd user service ---
    SYSTEMD_DIR="$HOME/.config/systemd/user"
    mkdir -p "$SYSTEMD_DIR"
    LOG_DIR="$HOME/.local/share/cc-matrix-display"
    mkdir -p "$LOG_DIR"

    SERVER_PATH="$REPO_DIR/host/server.py"
    CONFIG_PATH="$CONFIG_DIR/config.json"

    cat > "$SYSTEMD_DIR/cc-matrix-display.service" <<EOF
[Unit]
Description=cc-matrix-display server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$(command -v python3) $SERVER_PATH $CONFIG_PATH
Restart=on-failure
RestartSec=5
Environment=CC_MATRIX_SECRET=$SECRET

[Install]
WantedBy=default.target
EOF

    systemctl --user daemon-reload
    systemctl --user enable cc-matrix-display.service
    systemctl --user restart cc-matrix-display.service

    info "Installed and started systemd user service"
    info "  Logs: journalctl --user -u cc-matrix-display -f"
    info "  Note: run 'loginctl enable-linger $(whoami)' to keep service running after logout"
else
    warn "Neither launchd nor systemd found. Run server manually:"
    warn "  python3 $REPO_DIR/host/server.py $CONFIG_DIR/config.json"
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

sleep 1

PORT=$(jq -r '.port' "$CONFIG_DIR/config.json")
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $SECRET" \
    "http://127.0.0.1:$PORT/status" 2>/dev/null || echo "000")

if [ "$HTTP_CODE" = "200" ]; then
    info "Server verified (HTTP 200)"
else
    warn "Server not responding yet (HTTP $HTTP_CODE)."
    if [ "$(uname)" = "Darwin" ]; then
        warn "Check logs: $LOG_DIR/cc-matrix-display.log"
    else
        warn "Check logs: journalctl --user -u cc-matrix-display"
    fi
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

echo ""
info "Setup complete!"
echo ""
echo "  Server:  http://0.0.0.0:$PORT/status"
echo "  Secret:  $SECRET"
echo "  Config:  $CONFIG_DIR/config.json"
echo "  Logs:    $LOG_DIR/cc-matrix-display.log"
echo ""
LOCAL_IP=""
if [ "$(uname)" = "Darwin" ]; then
    LOCAL_IP=$(ipconfig getifaddr en0 2>/dev/null || true)
else
    LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
fi

echo "  For the Matrix Portal settings.toml, use:"
echo "    CC_MATRIX_URL = \"http://$(hostname).local:$PORT\""
if [ -n "$LOCAL_IP" ]; then
    echo "    # or by IP: CC_MATRIX_URL = \"http://$LOCAL_IP:$PORT\""
fi
echo "    CC_MATRIX_SECRET = \"$SECRET\""
echo ""
echo "  To test: curl -s -H 'Authorization: Bearer $SECRET' http://localhost:$PORT/status | python3 -m json.tool"
echo ""
