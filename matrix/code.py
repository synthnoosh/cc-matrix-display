"""cc-matrix-display — CircuitPython code for Matrix Portal M4 + 64x32 RGB LED panel.

Polls a local HTTP server for Claude Code session data and usage stats,
then renders them on the LED matrix with color-coded bars, scrolling text,
and attention-grabbing pulses for sessions waiting for input.

Display layout (64x32 pixels, tom-thumb font 4x6):
  Row 0-5:   5h usage bar + percentage
  Row 6-11:  7d usage bar + percentage
  Row 12:    separator line with session count
  Row 13-18: session slot 1
  Row 19-24: session slot 2
  Row 25-30: session slot 3

Priority pinning: waiting sessions always pinned to top slots.
"""

import os
import time
import board
import busio
import displayio
import framebufferio
import rgbmatrix
import terminalio
from digitalio import DigitalInOut

from adafruit_bitmap_font import bitmap_font
from adafruit_display_text import label
from adafruit_esp32spi import adafruit_esp32spi
import adafruit_connection_manager
import adafruit_requests

# ---------------------------------------------------------------------------
# Configuration from settings.toml
# ---------------------------------------------------------------------------

SERVER_URL = os.getenv("CC_MATRIX_URL", "")
if not SERVER_URL:
    raise RuntimeError("CC_MATRIX_URL not set in settings.toml — see settings.toml.example")
SECRET = os.getenv("CC_MATRIX_SECRET", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_S", "5"))
WIFI_SSID = os.getenv("WIFI_SSID", "")
WIFI_PASSWORD = os.getenv("WIFI_PASSWORD", "")

STATUS_URL = SERVER_URL.rstrip("/") + "/status"

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

WIDTH = 64
HEIGHT = 32
BAR_X = 14  # x offset for usage bars (after "5h " / "7d " label)
BAR_WIDTH = 30  # pixels wide for the bar
BAR_HEIGHT = 4  # pixels tall
PCT_X = 46  # x offset for percentage text
SEPARATOR_Y = 13
SESSION_SLOTS = 2
SESSION_START_Y = 15  # first session row y
SESSION_ROW_HEIGHT = 9  # per row
SCROLL_SPEED = 0.3  # seconds between character scroll steps
SCROLL_PAUSE = 2.0  # seconds to show name start before scrolling
SCROLL_END_PAUSE = 3.0  # seconds to show name end before resetting
PULSE_SPEED = 0.5  # seconds between pulse toggles
CYCLE_SPEED = 4.0  # seconds between session group rotation
FLASH_DURATION = 5.0  # seconds for full-screen alert

# Colors (RGB 565 compatible — use 24-bit hex, displayio handles conversion)
COLOR_GREEN = 0x00CC00
COLOR_YELLOW = 0xCCCC00
COLOR_ORANGE = 0xFF6600
COLOR_RED = 0xFF0000
COLOR_AMBER = 0xFFAA00
COLOR_AMBER_DIM = 0x664400
COLOR_RED_BRIGHT = 0xFF0000
COLOR_RED_DIM = 0x660000
COLOR_WHITE = 0xFFFFFF
COLOR_WHITE_DIM = 0x666666
COLOR_GRAY = 0x333333
COLOR_BLACK = 0x000000
COLOR_SEPARATOR = 0x222222

# ---------------------------------------------------------------------------
# Initialize display
# ---------------------------------------------------------------------------

displayio.release_displays()

matrix = rgbmatrix.RGBMatrix(
    width=WIDTH,
    height=HEIGHT,
    bit_depth=3,
    rgb_pins=[
        board.MTX_R1, board.MTX_G1, board.MTX_B1,
        board.MTX_R2, board.MTX_G2, board.MTX_B2,
    ],
    addr_pins=[
        board.MTX_ADDRA, board.MTX_ADDRB,
        board.MTX_ADDRC, board.MTX_ADDRD,
    ],
    clock_pin=board.MTX_CLK,
    latch_pin=board.MTX_LAT,
    output_enable_pin=board.MTX_OE,
)

display = framebufferio.FramebufferDisplay(matrix, auto_refresh=True)

# Show boot splash before WiFi init (which can take several seconds)
boot_group = displayio.Group()
boot_label = label.Label(terminalio.FONT, text="booting...", color=0x666666, x=8, y=16)
boot_group.append(boot_label)
display.root_group = boot_group

# ---------------------------------------------------------------------------
# Initialize ESP32 WiFi co-processor (Matrix Portal M4 — SAMD51 + ESP32 over SPI)
# ---------------------------------------------------------------------------

esp32_cs = DigitalInOut(board.ESP_CS)
esp32_ready = DigitalInOut(board.ESP_BUSY)
esp32_reset = DigitalInOut(board.ESP_RESET)
spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)
esp.reset()
time.sleep(1)

# Load fonts — small for usage bars, larger for session names
try:
    font_small = bitmap_font.load_font("/fonts/tom-thumb.bdf")
except OSError:
    font_small = terminalio.FONT
try:
    font = bitmap_font.load_font("/fonts/5x8.bdf")
except OSError:
    font = terminalio.FONT

# ---------------------------------------------------------------------------
# Display groups
# ---------------------------------------------------------------------------

root = displayio.Group()


def bar_color(pct):
    """Return color for a usage percentage."""
    if pct <= 50:
        return COLOR_GREEN
    elif pct <= 75:
        return COLOR_YELLOW
    elif pct <= 90:
        return COLOR_ORANGE
    return COLOR_RED


def create_bar_bitmap(pct, y_offset):
    """Create a filled usage bar bitmap + palette."""
    palette = displayio.Palette(2)
    palette[0] = COLOR_BLACK
    palette[1] = bar_color(pct)

    bmp = displayio.Bitmap(BAR_WIDTH, BAR_HEIGHT, 2)
    fill_width = max(0, min(BAR_WIDTH, int(BAR_WIDTH * pct / 100)))
    for x in range(fill_width):
        for y in range(BAR_HEIGHT):
            bmp[x, y] = 1

    grid = displayio.TileGrid(bmp, pixel_shader=palette, x=BAR_X, y=y_offset)
    return grid, bmp, palette


# --- Usage section ---
usage_group = displayio.Group()

label_5h = label.Label(font_small, text="5h", color=COLOR_WHITE_DIM, x=1, y=3)
label_7d = label.Label(font_small, text="7d", color=COLOR_WHITE_DIM, x=1, y=10)
usage_group.append(label_5h)
usage_group.append(label_7d)

bar_5h_grid, bar_5h_bmp, bar_5h_pal = create_bar_bitmap(0, 1)
bar_7d_grid, bar_7d_bmp, bar_7d_pal = create_bar_bitmap(0, 8)
usage_group.append(bar_5h_grid)
usage_group.append(bar_7d_grid)

pct_5h_label = label.Label(font_small, text="  0%", color=COLOR_WHITE_DIM, x=PCT_X, y=3)
pct_7d_label = label.Label(font_small, text="  0%", color=COLOR_WHITE_DIM, x=PCT_X, y=10)
usage_group.append(pct_5h_label)
usage_group.append(pct_7d_label)

root.append(usage_group)

# --- Separator ---
sep_palette = displayio.Palette(1)
sep_palette[0] = COLOR_SEPARATOR
sep_bmp = displayio.Bitmap(WIDTH, 1, 1)
for x in range(WIDTH):
    sep_bmp[x, 0] = 0
sep_grid = displayio.TileGrid(sep_bmp, pixel_shader=sep_palette, x=0, y=SEPARATOR_Y)
root.append(sep_grid)

sep_label = None  # removed — was causing visual noise on separator line

# --- Session slots ---
session_group = displayio.Group()
session_labels = []
session_dots = []

for i in range(SESSION_SLOTS):
    y = SESSION_START_Y + i * SESSION_ROW_HEIGHT + 2
    # Status dot (single character)
    dot = label.Label(font, text=" ", color=COLOR_GREEN, x=1, y=y)
    session_group.append(dot)
    session_dots.append(dot)
    # Session name
    lbl = label.Label(font, text="", color=COLOR_WHITE_DIM, x=7, y=y)
    session_group.append(lbl)
    session_labels.append(lbl)

root.append(session_group)

# --- Flash overlay (hidden by default) ---
flash_group = displayio.Group()
flash_group.hidden = True
flash_bg_palette = displayio.Palette(1)
flash_bg_palette[0] = COLOR_BLACK
flash_bg_bmp = displayio.Bitmap(WIDTH, HEIGHT, 1)
flash_bg_grid = displayio.TileGrid(flash_bg_bmp, pixel_shader=flash_bg_palette)
flash_group.append(flash_bg_grid)

flash_name_label = label.Label(font, text="", color=COLOR_AMBER, x=4, y=12)
flash_group.append(flash_name_label)
flash_action_label = label.Label(font, text="NEEDS INPUT", color=COLOR_WHITE, x=8, y=20)
flash_group.append(flash_action_label)

root.append(flash_group)

# ---------------------------------------------------------------------------
# WiFi + HTTP
# ---------------------------------------------------------------------------


def connect_wifi():
    """Connect to WiFi via ESP32 co-processor, retry on failure."""
    if esp.is_connected:
        return True
    if not WIFI_SSID:
        print("No WIFI_SSID configured")
        return False
    for attempt in range(3):
        try:
            print(f"WiFi connecting to {WIFI_SSID} (attempt {attempt + 1})...")
            esp.connect_AP(WIFI_SSID, WIFI_PASSWORD)
            print(f"WiFi connected: {esp.pretty_ip(esp.ip_address)}")
            return True
        except (ConnectionError, RuntimeError) as e:
            print(f"WiFi failed: {e}")
            time.sleep(2)
    return False


session = None


def init_http():
    """Create a fresh adafruit_requests Session."""
    global session
    pool = adafruit_connection_manager.get_radio_socketpool(esp)
    ssl_ctx = adafruit_connection_manager.get_radio_ssl_context(esp)
    session = adafruit_requests.Session(pool, ssl_ctx)


_consecutive_failures = 0


def fetch_status():
    """Fetch /status from the host server. Rebuilds the HTTP session on repeated failures."""
    global _consecutive_failures
    if session is None:
        return None

    headers = {"Connection": "close"}
    if SECRET:
        headers["Authorization"] = f"Bearer {SECRET}"
    try:
        resp = session.get(STATUS_URL, headers=headers, timeout=10)
        data = resp.json()
        resp.close()
        _consecutive_failures = 0
        return data
    except Exception as e:
        _consecutive_failures += 1
        print(f"Fetch error ({_consecutive_failures}): {e}")
        if _consecutive_failures >= 2:
            # Socket is likely stuck — rebuild the entire session
            print("Rebuilding HTTP session...")
            _consecutive_failures = 0
            init_http()
        return None


# ---------------------------------------------------------------------------
# Display update logic
# ---------------------------------------------------------------------------


def update_bar(bmp, palette, pct):
    """Update a usage bar bitmap with new percentage."""
    palette[1] = bar_color(pct)
    fill_width = max(0, min(BAR_WIDTH, int(BAR_WIDTH * pct / 100)))
    for x in range(BAR_WIDTH):
        val = 1 if x < fill_width else 0
        for y in range(BAR_HEIGHT):
            bmp[x, y] = val


def format_pct(pct):
    """Format percentage for display, right-aligned in 4 chars."""
    s = f"{pct}%"
    while len(s) < 4:
        s = " " + s
    return s


# State
current_sessions = []  # full list from server
prev_session_names_waiting = set()  # for transition detection
display_offset = 0  # for cycling through sessions
scroll_names = [""] * SESSION_SLOTS    # last name shown per slot
scroll_offsets = [0] * SESSION_SLOTS   # current scroll position per slot
scroll_phase = "idle"                  # idle | pause | scroll | done
scroll_timer = 0.0                     # monotonic time for phase transitions


def get_visible_sessions():
    """Return sessions visible on display, paging through all when they exceed slots.

    Priority sort: blocked first, then waiting, then working.
    When all sessions fit in SESSION_SLOTS, show them all.
    When they don't, page through in groups of SESSION_SLOTS.
    """
    all_sorted = sorted(
        current_sessions,
        key=lambda s: {"blocked": 0, "waiting": 1, "working": 2}.get(s["status"], 2),
    )

    if len(all_sorted) <= SESSION_SLOTS:
        return all_sorted

    # Page through all sessions in slot-sized groups
    start = (display_offset * SESSION_SLOTS) % len(all_sorted)
    visible = []
    for i in range(SESSION_SLOTS):
        idx = (start + i) % len(all_sorted)
        visible.append(all_sorted[idx])
    return visible


def update_display(data):
    """Update all display elements from server response data."""
    global current_sessions, prev_session_names_waiting

    if not data:
        return False  # no transition

    usage = data.get("usage", {})
    five_h = usage.get("five_hour", {}).get("pct", 0)
    seven_d = usage.get("seven_day", {}).get("pct", 0)

    # Update usage bars
    update_bar(bar_5h_bmp, bar_5h_pal, five_h)
    update_bar(bar_7d_bmp, bar_7d_pal, seven_d)
    pct_5h_label.text = format_pct(five_h)
    pct_5h_label.color = bar_color(five_h)
    pct_7d_label.text = format_pct(seven_d)
    pct_7d_label.color = bar_color(seven_d)

    # Update sessions
    sessions = data.get("sessions", [])
    current_sessions = sessions

    # Detect new attention transitions (waiting or blocked)
    new_attention = set()
    for s in sessions:
        if s["status"] in ("waiting", "blocked"):
            new_attention.add(s["name"])

    new_transitions = new_attention - prev_session_names_waiting
    prev_session_names_waiting = new_attention

    # Update visible session slots
    visible = get_visible_sessions()
    for i in range(SESSION_SLOTS):
        if i < len(visible):
            s = visible[i]
            name = s["name"]
            status = s["status"]

            if status == "blocked":
                session_labels[i].color = COLOR_WHITE
                session_dots[i].color = COLOR_RED_BRIGHT
            elif status == "waiting":
                session_labels[i].color = COLOR_WHITE
                session_dots[i].color = COLOR_AMBER
            else:
                session_labels[i].color = COLOR_WHITE_DIM
                session_dots[i].color = COLOR_GREEN

            session_dots[i].text = "*"
        else:
            session_labels[i].text = ""
            session_dots[i].text = " "

    # Return first new transition name (for flash), or None
    if new_transitions:
        return list(new_transitions)[0]
    return None


def show_flash(session_name):
    """Show full-screen flash alert for a session needing attention."""
    flash_name_label.text = session_name
    # Center the name horizontally (approximate)
    name_width = len(session_name) * 4
    flash_name_label.x = max(1, (WIDTH - name_width) // 2)
    flash_group.hidden = False


def hide_flash():
    """Hide the flash overlay."""
    flash_group.hidden = True


def show_offline():
    """Show offline indicator in session area."""
    session_labels[0].text = "offline"
    session_labels[0].color = COLOR_RED
    session_dots[0].text = "!"
    session_dots[0].color = COLOR_RED
    for i in range(1, SESSION_SLOTS):
        session_labels[i].text = ""
        session_dots[i].text = " "


def show_no_sessions():
    """Show empty state when no named sessions are active."""
    session_labels[0].text = "no sessions"
    session_labels[0].color = COLOR_GRAY
    session_dots[0].text = " "
    for i in range(1, SESSION_SLOTS):
        session_labels[i].text = ""
        session_dots[i].text = " "


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    global display_offset, current_sessions, scroll_phase, scroll_timer

    # WiFi (boot_group still showing during connect)
    if not connect_wifi():
        show_offline()
        while not connect_wifi():
            time.sleep(10)

    time.sleep(3)  # let ESP32 settle after WiFi connect
    init_http()

    # Switch from boot splash to real UI
    display.root_group = root

    # Timers (monotonic)
    last_poll = 0
    last_pulse = 0
    last_cycle = 0
    flash_start = 0
    pulse_on = True
    is_flashing = False
    poll_failures = 0

    while True:
        now = time.monotonic()

        # --- Flash overlay timer ---
        if is_flashing and now - flash_start > FLASH_DURATION:
            hide_flash()
            is_flashing = False

        # --- Poll server ---
        if now - last_poll > POLL_INTERVAL:
            last_poll = now
            data = fetch_status()
            if data:
                poll_failures = 0
                transition = update_display(data)
                if transition and not is_flashing:
                    show_flash(transition)
                    flash_start = now
                    is_flashing = True
                if not data.get("sessions"):
                    show_no_sessions()
            else:
                poll_failures += 1
                # Only show offline after sustained failures
                if poll_failures >= 5:
                    # Clear stale session data so scroll/pulse timers
                    # don't overwrite the offline display
                    current_sessions = []
                    if not esp.is_connected:
                        show_offline()
                        connect_wifi()
                        if esp.is_connected:
                            init_http()
                            poll_failures = 0
                    else:
                        show_offline()

        # --- Scroll long names (phase-based, synchronized, one-shot) ---
        if not is_flashing and current_sessions:
            visible = get_visible_sessions()
            max_chars = (WIDTH - 7) // 5  # visible chars at 5px/char

            # Detect name changes → restart scroll cycle
            changed = False
            for i in range(SESSION_SLOTS):
                name = visible[i]["name"] if i < len(visible) else ""
                if name != scroll_names[i]:
                    changed = True
                    scroll_names[i] = name
                    scroll_offsets[i] = 0
                    session_labels[i].text = name[:max_chars] if name else ""

            if changed:
                scroll_phase = "pause"
                scroll_timer = now

            # Phase: pause → start scrolling (or done if no scroll needed)
            if scroll_phase == "pause" and now - scroll_timer >= SCROLL_PAUSE:
                needs_scroll = any(
                    len(scroll_names[i]) > max_chars
                    for i in range(SESSION_SLOTS)
                )
                if needs_scroll:
                    scroll_phase = "scroll"
                    scroll_timer = now
                else:
                    scroll_phase = "done"

            # Phase: scroll → advance one character per SCROLL_SPEED
            elif scroll_phase == "scroll" and now - scroll_timer >= SCROLL_SPEED:
                scroll_timer = now
                all_done = True
                for i in range(min(len(visible), SESSION_SLOTS)):
                    name = scroll_names[i]
                    max_offset = len(name) - max_chars
                    if max_offset > 0 and scroll_offsets[i] < max_offset:
                        scroll_offsets[i] += 1
                        pos = scroll_offsets[i]
                        session_labels[i].text = name[pos:pos + max_chars]
                        if scroll_offsets[i] < max_offset:
                            all_done = False
                if all_done:
                    scroll_phase = "hold"
                    scroll_timer = now

            # Phase: hold → show end for SCROLL_END_PAUSE, then reset to start
            elif scroll_phase == "hold" and now - scroll_timer >= SCROLL_END_PAUSE:
                for i in range(min(len(visible), SESSION_SLOTS)):
                    scroll_offsets[i] = 0
                    name = scroll_names[i]
                    session_labels[i].text = name[:max_chars] if name else ""
                scroll_phase = "settle"
                scroll_timer = now

            # Phase: settle → dwell on start for SCROLL_END_PAUSE before allowing cycle
            elif scroll_phase == "settle" and now - scroll_timer >= SCROLL_END_PAUSE:
                scroll_phase = "done"

        # --- Pulse waiting dots ---
        if not is_flashing and now - last_pulse > PULSE_SPEED:
            last_pulse = now
            pulse_on = not pulse_on
            visible = get_visible_sessions()
            for i in range(min(len(visible), SESSION_SLOTS)):
                st = visible[i]["status"]
                if st == "blocked":
                    session_dots[i].color = COLOR_RED_BRIGHT if pulse_on else COLOR_RED_DIM
                elif st == "waiting":
                    session_dots[i].color = COLOR_AMBER if pulse_on else COLOR_AMBER_DIM

        # --- Cycle sessions (wait for scroll to finish) ---
        if not is_flashing and scroll_phase in ("idle", "done") and now - last_cycle > CYCLE_SPEED:
            last_cycle = now
            if len(current_sessions) > SESSION_SLOTS:
                display_offset += 1
                visible = get_visible_sessions()
                for i in range(SESSION_SLOTS):
                    if i < len(visible):
                        s = visible[i]
                        st = s["status"]
                        session_labels[i].color = COLOR_WHITE if st != "working" else COLOR_WHITE_DIM
                        session_dots[i].text = "*"
                        if st == "blocked":
                            session_dots[i].color = COLOR_RED_BRIGHT
                        elif st == "waiting":
                            session_dots[i].color = COLOR_AMBER
                        else:
                            session_dots[i].color = COLOR_GREEN

        # Small sleep to prevent tight-looping
        time.sleep(0.01)


main()
