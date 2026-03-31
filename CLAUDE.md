# cc-matrix-display

A physical desk dashboard for Claude Code — drives a 64x32 RGB LED matrix via Adafruit Matrix Portal M4.

## Architecture

Three components:
1. **`host/server.py`** — Python 3 stdlib HTTP server. Aggregates Claude Code session data + usage from Anthropic API. Zero external dependencies.
2. **`hooks/`** — Shell scripts added to Claude Code's hook system. `Stop` writes a waiting flag; `PreToolUse` clears it.
3. **`matrix/code.py`** — CircuitPython for the Matrix Portal M4. Polls the server over WiFi, renders to the 64x32 LED panel.

## Conventions

- **Host code**: Python 3.9+ stdlib only. No pip dependencies. Single-file server.
- **Matrix code**: CircuitPython 9.x targeting SAMD51 + ESP32 co-processor (Matrix Portal M4). Libraries from the Adafruit CircuitPython bundle.
- **Hooks**: POSIX shell + jq. Must execute in <10ms.
- **Config**: `host/config.json` (server) and `matrix/settings.toml` (CircuitPython). Neither is committed — `.example` templates are.
- **Security**: Shared secret (Bearer token) required for API access. No sensitive data (paths, PIDs, session IDs) in API responses.

## Key Data Sources (Claude Code standard)

- `~/.claude/sessions/{PID}.json` — session registry. Named sessions include `"name"` field.
- `~/.claude/.credentials.json` — OAuth token (cross-platform). Falls back to macOS Keychain, then `CC_MATRIX_API_KEY` env var.
- `https://api.anthropic.com/oauth/usage` — 5h/7d usage via the above token.
- `/tmp/claude-waiting-{sessionId}` — flag files written by our hooks.

## Display

64x32 pixels. Tom-thumb font (3x5px). Priority pinning: waiting sessions pinned to top slots, working sessions cycle through remaining. Full-screen flash on new waiting events.
