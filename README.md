# cc-matrix-display

A physical desk dashboard for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) powered by an Adafruit Matrix Portal M4 and a 64x32 RGB LED matrix.

**What it does:**
- Shows your Claude Code **usage limits** (5-hour and 7-day rolling windows) as color-coded progress bars
- Monitors your **named sessions** and alerts you when any session is waiting for input
- Priority-pinned display: waiting sessions are always visible; working sessions cycle through remaining slots

## Hardware

| Component | Link |
|-|-|
| Adafruit Matrix Portal M4 | [thepihut.com](https://thepihut.com/products/adafruit-matrix-portal-circuitpython-powered-internet-display) |
| 64x32 RGB LED Matrix Panel (4mm pitch) | [thepihut.com](https://thepihut.com/products/rgb-full-colour-led-matrix-panel-4mm-pitch-64x32-pixels) |
| USB-C data cable | For power and programming (must support data, not charge-only) |

## How It Works

```
┌─────────────────────────┐              ┌──────────────────┐
│  Your Mac/Linux         │  HTTP/JSON   │  Matrix Portal   │
│                         │ ◄─────────── │  M4 (SAMD51 +    │
│  server.py              │    :8321     │   ESP32 WiFi)    │
│  - session data         │ ───────────► │                  │
│  - usage API            │              │  code.py         │
│  - auth: Bearer         │              │  - renders bars  │
│                         │              │  - shows sessions│
│  Hooks:                 │              │  - pulses alerts │
│  - Stop: waiting        │              │                  │
│  - PreToolUse: pending  │              │  64x32 LED panel │
│  - PostToolUse: clear   │              └──────────────────┘
└─────────────────────────┘
```

Three Claude Code hooks track session state:
- **Stop** (`matrix-stop.sh`): marks session as "waiting" (turn complete, needs user input)
- **PreToolUse** (`matrix-resume.sh`): clears the waiting flag, writes a "pending" flag (tool in progress)
- **PostToolUse** (`matrix-complete.sh`): clears the pending flag (tool finished)

The server derives a **blocked** state from pending flag age: if a pending flag lingers >10 seconds, a permission prompt is likely blocking the session. After 60 seconds, stale pending flags decay to "waiting".

### Named Sessions

This display **only tracks named Claude Code sessions**. Unnamed sessions are invisible to it. This is by design: you control which sessions appear on your dashboard by choosing to name them.

To name a session:
- Start with a name: `claude --name my-feature`
- Name an existing session: type `/rename my-session` in Claude Code

The server reads `~/.claude/sessions/*.json` and filters for entries with a `"name"` field. Only sessions with a live process (PID check) are included.

## Quick Start

### 1. Assemble the Hardware

1. Slot the Matrix Portal M4 onto the back of the 64x32 LED panel — the pin headers on the board align with the HUB75 connector on the panel. Press firmly until seated.
2. Connect a USB-C **data** cable (not charge-only) from the Matrix Portal to your computer.

### 2. Flash CircuitPython

This installs the CircuitPython runtime on the board. You only need to do this once.

1. Download the latest CircuitPython 9.x `.uf2` file for the Matrix Portal M4 from [circuitpython.org/board/matrixportal_m4](https://circuitpython.org/board/matrixportal_m4/).
2. **Enter bootloader mode**: double-tap the **Reset** button on the Matrix Portal. The onboard NeoPixel LED should turn **green**. If it turns red, try a different USB cable or port.
3. A drive called **MATRIXBOOT** will appear on your computer.
4. Drag the downloaded `.uf2` file onto the **MATRIXBOOT** drive. The LED will flash as it writes.
5. When complete, **MATRIXBOOT** disappears and a new drive called **CIRCUITPY** appears. CircuitPython is now installed.

### 3. Install Prerequisites

The host installer requires `jq` for JSON processing:
```bash
# macOS
brew install jq
# Linux (Debian/Ubuntu)
sudo apt install jq
```

### 4. Install CircuitPython Libraries

With the board connected and the CIRCUITPY drive mounted:
```bash
pip install circup
circup install adafruit_display_text adafruit_bitmap_font adafruit_esp32spi adafruit_requests adafruit_connection_manager
```

### 5. Copy Display Code

Copy the contents of `matrix/` to your CIRCUITPY drive:
```bash
# macOS
cp -r matrix/* /Volumes/CIRCUITPY/
# Linux
cp -r matrix/* /media/$USER/CIRCUITPY/
```

Then create your config:
```bash
# macOS
cp /Volumes/CIRCUITPY/settings.toml.example /Volumes/CIRCUITPY/settings.toml
# Linux
cp /media/$USER/CIRCUITPY/settings.toml.example /media/$USER/CIRCUITPY/settings.toml
```

Edit `settings.toml` on the CIRCUITPY drive with your WiFi credentials and server URL (see [Configuration](#configuration) below).

### 6. Install Host Server + Hooks

```bash
./host/install.sh
```

This will:
- Generate a shared secret for secure communication
- Install Claude Code hooks for session monitoring
- Set up the server as a background service (launchd on macOS, systemd on Linux)
- Print the secret and URL to put in your `settings.toml`

### 7. Verify

```bash
curl -s -H "Authorization: Bearer YOUR_SECRET" http://localhost:8321/status | python3 -m json.tool
```

The board will reboot automatically after you copied the files. If everything is connected, you should see usage bars appear on the LED panel within a few seconds.

## Display Layout

```
┌──────────────────────────── 64px ─────────────────────────────┐
│  5h ▓▓▓▓░░░░░░ 21%        usage bars (green/yellow/red)     │
│  7d ▓▓▓▓▓▓▓░░░ 47%                                          │
│  ────────────────────      separator line                     │
│  ◆ pipeline-upgrade        blocked: red pulse                │
│  ● my-feature              working: green, cycles            │
└───────────────────────────────────────────────────────────────┘
```

Session states: **working** (green dot), **waiting** (amber pulse — needs user input), **blocked** (red pulse — permission prompt). When a session transitions to waiting or blocked, a full-screen flash alerts you. With more sessions than slots, working sessions cycle automatically.

## Configuration

### Server (`~/.cc-matrix/config.json`)

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

**Linux note:** Run `loginctl enable-linger $(whoami)` to keep the systemd service running after logout.

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

The serial port path may vary — check `ls /dev/cu.usbmodem*` (macOS) or `ls /dev/ttyACM*` (Linux).

## Uninstalling

```bash
./host/uninstall.sh
```

This removes the background service, Claude Code hooks, config directory (`~/.cc-matrix`), and flag files. Your Matrix Portal code on the CIRCUITPY drive is not affected — delete `code.py` from the drive or reflash CircuitPython to reset it.

## License

MIT
