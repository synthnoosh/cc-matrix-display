# cc-matrix-display

A physical desk dashboard for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) powered by an Adafruit Matrix Portal M4 and a 64x32 RGB LED matrix.

**What it does:**
- Shows your Claude Code **usage limits** (5-hour and 7-day rolling windows) as color-coded progress bars
- Monitors your **named sessions** and alerts you when any session is waiting for input
- Priority-pinned display: waiting sessions are always visible; working sessions cycle through remaining slots

## Hardware

| Component | Link |
|-|-|
| Adafruit Matrix Portal M4 | [adafruit.com/product/4745](https://www.adafruit.com/product/4745) |
| 64x32 RGB LED Matrix Panel (4mm pitch) | [adafruit.com/product/2278](https://www.adafruit.com/product/2278) |
| USB-C cable | For power and initial programming |

## How It Works

```
┌─────────────────────┐   HTTP/JSON   ┌──────────────────┐
│  Your Mac/Linux     │ ◄──────────── │  Matrix Portal   │
│                     │   :8321       │  (ESP32-S3)      │
│  server.py          │ ────────────► │                  │
│  - session data     │               │  code.py         │
│  - usage API        │               │  - renders bars  │
│  - auth: Bearer     │               │  - shows sessions│
│                     │               │  - pulses alerts │
│  Hook scripts:      │               │                  │
│  - Stop: waiting    │               │  64x32 LED panel │
│  - PreToolUse: clear│               └──────────────────┘
└─────────────────────┘
```

Only **named** Claude Code sessions appear (started with `--name` or renamed with `/rename`). Unnamed sessions are invisible to the display.

## Quick Start

### 1. Flash CircuitPython

Download the latest CircuitPython 9.x UF2 for [Matrix Portal M4](https://circuitpython.org/board/matrixportal_m4/) and flash it.

### 2. Install CircuitPython Libraries

```bash
pip install circup
circup install adafruit_display_text adafruit_bitmap_font adafruit_requests adafruit_connection_manager
```

### 3. Copy Display Code

Copy the contents of `matrix/` to your CIRCUITPY drive:
```bash
cp -r matrix/* /Volumes/CIRCUITPY/
```

Then create your config:
```bash
cp /Volumes/CIRCUITPY/settings.toml.example /Volumes/CIRCUITPY/settings.toml
# Edit settings.toml with your WiFi credentials and server URL
```

### 4. Install Host Server + Hooks

```bash
./host/install.sh
```

This will:
- Generate a shared secret for secure communication
- Install Claude Code hooks for session monitoring
- Set up the server as a background service (launchd on macOS, systemd on Linux)

### 5. Verify

```bash
curl -s -H "Authorization: Bearer YOUR_SECRET" http://localhost:8321/status | python3 -m json.tool
```

## Display Layout

```
┌──────────────────────────── 64px ─────────────────────────────┐
│  5h ▓▓▓▓░░░░░░ 21%        usage bars (green/yellow/red)     │
│  7d ▓▓▓▓▓▓▓░░░ 47%                                          │
│  ── 2/5 waiting ──         separator with counts             │
│  ◆ pipeline-upgrade        waiting: amber pulse              │
│  ◆ statusline              waiting: amber pulse              │
│  ● my-feature              working: green, cycles            │
└───────────────────────────────────────────────────────────────┘
```

When a session transitions to "waiting", a full-screen flash alerts you.

## Configuration

### Server (`host/config.json`)

```json
{
  "port": 8321,
  "secret": "your-shared-secret",
  "bind": "0.0.0.0"
}
```

### Matrix Portal (`matrix/settings.toml`)

```toml
WIFI_SSID = "your-network"
WIFI_PASSWORD = "your-password"
CC_MATRIX_URL = "http://your-mac.local:8321"
CC_MATRIX_SECRET = "your-shared-secret"
POLL_INTERVAL_S = "5"
```

## Platform Support

| Platform | Credential Source | Autostart |
|-|-|-|
| macOS | `~/.claude/.credentials.json` (automatic) | launchd LaunchAgent |
| Linux | `~/.claude/.credentials.json` (automatic) | systemd user service |

The server reads your existing Claude Code OAuth token directly from `~/.claude/.credentials.json` — no manual key setup required. Just be logged into Claude Code.

## Security

The server binds to `0.0.0.0` so the Matrix Portal can reach it over WiFi. Mitigations:

- **Shared secret**: All requests require `Authorization: Bearer {secret}`
- **Minimal exposure**: API returns only session names and usage percentages — no file paths, PIDs, or session IDs
- **Firewall**: On macOS, enable the application firewall and allow only the server process

## Operations

### Checking Server Status

```bash
# Is the server running?
curl -s http://localhost:8321/health

# Full status (sessions + usage)
curl -s -H "Authorization: Bearer YOUR_SECRET" http://localhost:8321/status | python3 -m json.tool

# Check service state
launchctl list | grep cc-matrix                        # macOS
systemctl --user status cc-matrix-display              # Linux
```

### Restarting the Server

**macOS (launchd)**

```bash
# Graceful restart
launchctl kickstart -k gui/$(id -u)/com.cc-matrix-display.server
```

**Linux (systemd)**

```bash
systemctl --user restart cc-matrix-display
```

**If the server is hung** (socket open but not serving requests):

```bash
# 1. Find and kill the stuck process
lsof -i :8321                  # macOS: find PID
ss -tlnp sport = :8321         # Linux: find PID
kill -9 <PID>                  # force kill (SIGTERM may not work if hung)

# 2. Reload the service
# macOS:
launchctl bootout gui/$(id -u)/com.cc-matrix-display.server 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.cc-matrix-display.server.plist
# Linux:
systemctl --user restart cc-matrix-display

# 3. Verify
curl -s http://localhost:8321/health   # should return {"ok": true}
```

### Server Logs

```bash
tail -f ~/Library/Logs/cc-matrix-display.log           # macOS
journalctl --user -u cc-matrix-display -f              # Linux
```

### Deploying Display Code

When you change `matrix/code.py`, deploy to the Matrix Portal over USB:

```bash
# Board must be connected via USB and CIRCUITPY drive mounted
cp matrix/code.py /Volumes/CIRCUITPY/code.py           # macOS
cp matrix/code.py /media/$USER/CIRCUITPY/code.py       # Linux
```

The board auto-reboots on file save. To watch for errors during boot:

```bash
# Monitor serial output (requires pyserial: pip install pyserial)
# Serial port: /dev/cu.usbmodem* (macOS) or /dev/ttyACM* (Linux)
python3 -c "
import serial, time
ser = serial.Serial('PORT', 115200, timeout=2)
ser.write(b'\x04')  # soft reboot
while True:
    data = ser.read(ser.in_waiting or 1)
    if data: print(data.decode('utf-8', errors='replace'), end='')
    time.sleep(0.1)
"
```

The serial port path may vary — check `ls /dev/cu.usbmodem*`.

## Uninstalling

```bash
./host/uninstall.sh
```

This removes the background service, Claude Code hooks, config directory (`~/.cc-matrix`), and flag files. Your Matrix Portal code on the CIRCUITPY drive is not affected — delete `code.py` from the drive or reflash CircuitPython to reset it.

## License

MIT
