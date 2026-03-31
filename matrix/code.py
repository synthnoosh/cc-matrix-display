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

SERVER_URL = os.getenv("CC_MATRIX_URL", "http://192.168.1.100:8321")
SECRET = os.getenv("CC_MATRIX_SECRET", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_S", "5"))
WIFI_SSID = os.getenv("WIFI_SSID", "")
WIFI_PASSWORD = os.getenv("WIFI_PASSWORD", "")

# ---------------------------------------------------------------------------
# Display constants
# ---------------------------------------------------------------------------

WIDTH = 64
HEIGHT = 32
BAR_X = 14  # x offset for usage bars (after "5h " / "7d " label)
BAR_WIDTH = 34  # pixels wide for the bar
BAR_HEIGHT = 4  # pixels tall
PCT_X = 50  # x offset for percentage text
SEPARATOR_Y = 12
SESSION_SLOTS = 3
SESSION_START_Y = 14  # first session row y
SESSION_ROW_HEIGHT = 6  # per row
SCROLL_SPEED = 0.06  # seconds between scroll steps
PULSE_SPEED = 0.5  # seconds between pulse toggles
CYCLE_SPEED = 4.0  # seconds between session group rotation
FLASH_DURATION = 1.5  # seconds for full-screen alert

# Colors (RGB 565 compatible — use 24-bit hex, displayio handles conversion)
COLOR_GREEN = 0x00CC00
COLOR_YELLOW = 0xCCCC00
COLOR_ORANGE = 0xFF6600
COLOR_RED = 0xFF0000
COLOR_AMBER = 0xFFAA00
COLOR_AMBER_DIM = 0x664400
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

# ---------------------------------------------------------------------------
# Initialize ESP32 WiFi co-processor (Matrix Portal M4)
# ---------------------------------------------------------------------------

esp32_cs = DigitalInOut(board.ESP_CS)
esp32_ready = DigitalInOut(board.ESP_BUSY)
esp32_reset = DigitalInOut(board.ESP_RESET)
spi = busio.SPI(board.SCK, board.MOSI, board.MISO)
esp = adafruit_esp32spi.ESP_SPIcontrol(spi, esp32_cs, esp32_ready, esp32_reset)

display = framebufferio.FramebufferDisplay(matrix, auto_refresh=True)

# Load font
try:
    font = bitmap_font.load_font("/fonts/tom-thumb.bdf")
except OSError:
    font = terminalio.FONT  # fallback

# ---------------------------------------------------------------------------
# Display groups
# ---------------------------------------------------------------------------

root = displayio.Group()
display.root_group = root


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

label_5h = label.Label(font, text="5h", color=COLOR_WHITE_DIM, x=1, y=3)
label_7d = label.Label(font, text="7d", color=COLOR_WHITE_DIM, x=1, y=9)
usage_group.append(label_5h)
usage_group.append(label_7d)

bar_5h_grid, bar_5h_bmp, bar_5h_pal = create_bar_bitmap(0, 1)
bar_7d_grid, bar_7d_bmp, bar_7d_pal = create_bar_bitmap(0, 7)
usage_group.append(bar_5h_grid)
usage_group.append(bar_7d_grid)

pct_5h_label = label.Label(font, text="  0%", color=COLOR_WHITE_DIM, x=PCT_X, y=3)
pct_7d_label = label.Label(font, text="  0%", color=COLOR_WHITE_DIM, x=PCT_X, y=9)
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

sep_label = label.Label(font, text="", color=COLOR_GRAY, x=2, y=SEPARATOR_Y)
root.append(sep_label)

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


pool = None
requests = None


def init_http():
    """Initialize HTTP session using ESP32 co-processor radio."""
    global pool, requests
    pool = adafruit_connection_manager.get_radio_socketpool(esp)
    ssl_context = adafruit_connection_manager.get_radio_ssl_context(esp)
    requests = adafruit_requests.Session(pool, ssl_context)


def fetch_status():
    """Fetch /status from the host server. Returns parsed dict or None."""
    if requests is None:
        return None
    url = f"{SERVER_URL}/status"
    headers = {}
    if SECRET:
        headers["Authorization"] = f"Bearer {SECRET}"
    try:
        resp = requests.get(url, headers=headers, timeout=8)
        data = resp.json()
        resp.close()
        return data
    except Exception as e:
        print(f"Fetch error: {e}")
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
    return s.rjust(4)


# State
current_sessions = []  # full list from server
prev_session_names_waiting = set()  # for transition detection
display_offset = 0  # for cycling through sessions
scroll_positions = [0] * SESSION_SLOTS  # x offset for each label


def get_visible_sessions():
    """Apply priority pinning: waiting first, then working in cycle slots."""
    waiting = [s for s in current_sessions if s["status"] == "waiting"]
    working = [s for s in current_sessions if s["status"] == "working"]

    visible = []
    # Pin waiting sessions to top slots
    for i, s in enumerate(waiting[:SESSION_SLOTS]):
        visible.append(s)

    # Fill remaining slots with cycling working sessions
    remaining_slots = SESSION_SLOTS - len(visible)
    if remaining_slots > 0 and working:
        start = display_offset % len(working) if working else 0
        for i in range(remaining_slots):
            idx = (start + i) % len(working)
            visible.append(working[idx])
    elif remaining_slots <= 0 and len(waiting) > SESSION_SLOTS:
        # More waiting than slots: cycle among waiting
        start = display_offset % len(waiting)
        visible = []
        for i in range(SESSION_SLOTS):
            idx = (start + i) % len(waiting)
            visible.append(waiting[idx])

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

    # Detect new waiting transitions
    new_waiting = set()
    for s in sessions:
        if s["status"] == "waiting":
            new_waiting.add(s["name"])

    new_transitions = new_waiting - prev_session_names_waiting
    prev_session_names_waiting = new_waiting

    # Update separator
    total = len(sessions)
    waiting_count = len(new_waiting)
    if total > SESSION_SLOTS:
        sep_label.text = f"{waiting_count}/{total} wait"
    elif total > 0:
        sep_label.text = ""
    else:
        sep_label.text = ""

    # Update visible session slots
    visible = get_visible_sessions()
    for i in range(SESSION_SLOTS):
        if i < len(visible):
            s = visible[i]
            name = s["name"]
            is_waiting = s["status"] == "waiting"

            session_labels[i].text = name
            session_labels[i].color = COLOR_WHITE if is_waiting else COLOR_WHITE_DIM

            session_dots[i].text = "*"
            session_dots[i].color = COLOR_AMBER if is_waiting else COLOR_GREEN

            # Reset scroll for new content
            session_labels[i].x = 7
            scroll_positions[i] = 0
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
    global display_offset

    # Startup display
    sep_label.text = "connecting..."
    pct_5h_label.text = "  -"
    pct_7d_label.text = "  -"

    # WiFi
    if not connect_wifi():
        show_offline()
        while not connect_wifi():
            time.sleep(10)

    init_http()
    sep_label.text = ""

    # Timers (monotonic)
    last_poll = 0
    last_scroll = 0
    last_pulse = 0
    last_cycle = 0
    flash_start = 0
    pulse_on = True
    is_flashing = False

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
                transition = update_display(data)
                if transition and not is_flashing:
                    show_flash(transition)
                    flash_start = now
                    is_flashing = True
                if not data.get("sessions"):
                    show_no_sessions()
            else:
                # Check WiFi
                if not esp.is_connected:
                    show_offline()
                    connect_wifi()
                    if esp.is_connected:
                        init_http()

        # --- Scroll long names ---
        if not is_flashing and now - last_scroll > SCROLL_SPEED:
            last_scroll = now
            visible = get_visible_sessions()
            for i in range(min(len(visible), SESSION_SLOTS)):
                name = visible[i]["name"]
                text_width = len(name) * 4  # approximate with tom-thumb
                max_visible = WIDTH - 7  # pixels available for name
                if text_width > max_visible:
                    scroll_positions[i] += 1
                    total_scroll = text_width + 16  # 16px gap before wrap
                    if scroll_positions[i] > total_scroll:
                        scroll_positions[i] = 0
                    session_labels[i].x = 7 - scroll_positions[i]

        # --- Pulse waiting dots ---
        if not is_flashing and now - last_pulse > PULSE_SPEED:
            last_pulse = now
            pulse_on = not pulse_on
            visible = get_visible_sessions()
            for i in range(min(len(visible), SESSION_SLOTS)):
                if visible[i]["status"] == "waiting":
                    session_dots[i].color = COLOR_AMBER if pulse_on else COLOR_AMBER_DIM

        # --- Cycle working sessions ---
        if not is_flashing and now - last_cycle > CYCLE_SPEED:
            last_cycle = now
            waiting = [s for s in current_sessions if s["status"] == "waiting"]
            working = [s for s in current_sessions if s["status"] == "working"]
            remaining_slots = max(0, SESSION_SLOTS - len(waiting))
            if len(working) > remaining_slots:
                display_offset += 1
                # Re-render visible sessions
                visible = get_visible_sessions()
                for i in range(SESSION_SLOTS):
                    if i < len(visible):
                        s = visible[i]
                        session_labels[i].text = s["name"]
                        session_labels[i].color = (
                            COLOR_WHITE if s["status"] == "waiting" else COLOR_WHITE_DIM
                        )
                        session_dots[i].text = "*"
                        session_dots[i].color = (
                            COLOR_AMBER if s["status"] == "waiting" else COLOR_GREEN
                        )
                        session_labels[i].x = 7
                        scroll_positions[i] = 0

        # Small sleep to prevent tight-looping
        time.sleep(0.01)


main()
