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

import gc
import microcontroller
import os
import supervisor
import time
import board
import bitmaptools
import busio
import displayio
import framebufferio
import rgbmatrix
import terminalio
import vectorio
from digitalio import DigitalInOut
from watchdog import WatchDogMode

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

# Colors (24-bit hex — displayio converts to RGB565, bit_depth=3 quantizes further)
COLOR_GREEN = 0x00AA00
COLOR_YELLOW = 0xCCCC00
COLOR_ORANGE = 0xFF6600
COLOR_RED = 0xDD0000
COLOR_AMBER = 0xDD8800
COLOR_AMBER_DIM = 0x664400
COLOR_RED_BRIGHT = 0xDD0000
COLOR_RED_DIM = 0x660000
COLOR_TEAL = 0x009999     # working session dots — distinct from bar green
COLOR_WHITE = 0xFFFFFF
COLOR_WHITE_DIM = 0x888888
COLOR_GRAY = 0x333333
COLOR_BLACK = 0x000000
COLOR_SEPARATOR = 0x111133
COLOR_OUTLINE = 0x333333  # usage bar outlines

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
display.brightness = 0.85

# Show boot splash before WiFi init (which can take several seconds)
boot_group = displayio.Group()
boot_title = label.Label(terminalio.FONT, text="Booting..", color=COLOR_AMBER, x=10, y=10)
boot_group.append(boot_title)
boot_status = label.Label(terminalio.FONT, text="", color=COLOR_WHITE_DIM, x=12, y=26)
boot_group.append(boot_status)
# Progress bar (grows during WiFi connect)
boot_bar_pal = displayio.Palette(1)
boot_bar_pal[0] = COLOR_AMBER
boot_bar = vectorio.Rectangle(pixel_shader=boot_bar_pal, width=1, height=2, x=12, y=17)
boot_group.append(boot_bar)
# Outline for progress bar
boot_outline_pal = displayio.Palette(1)
boot_outline_pal[0] = COLOR_OUTLINE
boot_outline = vectorio.Rectangle(
    pixel_shader=boot_outline_pal, width=42, height=4, x=11, y=16,
)
# Insert outline behind the bar (index 2 = before bar at index 3)
boot_group.insert(2, boot_outline)
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
    FONT_CHAR_WIDTH = 5
except OSError:
    font = terminalio.FONT
    FONT_CHAR_WIDTH = 6

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
    if fill_width > 0:
        bitmaptools.fill_region(bmp, 0, 0, fill_width, BAR_HEIGHT, 1)

    grid = displayio.TileGrid(bmp, pixel_shader=palette, x=BAR_X, y=y_offset)
    return grid, bmp, palette


# --- Usage section ---
usage_group = displayio.Group()

label_5h = label.Label(font_small, text="5h", color=COLOR_WHITE_DIM, x=1, y=3)
label_7d = label.Label(font_small, text="7d", color=COLOR_WHITE_DIM, x=1, y=10)
usage_group.append(label_5h)
usage_group.append(label_7d)

# Bar outlines (vectorio — rendered behind fill, gives "UI widget" feel)
outline_pal = displayio.Palette(1)
outline_pal[0] = COLOR_OUTLINE
bar_5h_outline = vectorio.Rectangle(
    pixel_shader=outline_pal, width=BAR_WIDTH + 2, height=BAR_HEIGHT + 2,
    x=BAR_X - 1, y=0,
)
bar_7d_outline = vectorio.Rectangle(
    pixel_shader=outline_pal, width=BAR_WIDTH + 2, height=BAR_HEIGHT + 2,
    x=BAR_X - 1, y=7,
)
usage_group.append(bar_5h_outline)
usage_group.append(bar_7d_outline)

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
sep_grid = displayio.TileGrid(sep_bmp, pixel_shader=sep_palette, x=0, y=SEPARATOR_Y)
root.append(sep_grid)


# --- Session slots ---
session_group = displayio.Group()
session_labels = []
session_dots = []      # vectorio.Circle objects
session_dot_pals = []  # palette per dot (mutated for pulse animation)

for i in range(SESSION_SLOTS):
    y = SESSION_START_Y + i * SESSION_ROW_HEIGHT + 2
    # Status dot (vectorio circle — proper round indicator)
    dot_pal = displayio.Palette(1)
    dot_pal[0] = COLOR_TEAL
    dot = vectorio.Circle(pixel_shader=dot_pal, radius=2, x=4, y=y)
    dot.hidden = True
    session_group.append(dot)
    session_dots.append(dot)
    session_dot_pals.append(dot_pal)
    # Session name
    lbl = label.Label(font, text="", color=COLOR_WHITE_DIM, x=9, y=y)
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

# Border frame (amber outline → black inset → content)
flash_border_pal = displayio.Palette(1)
flash_border_pal[0] = COLOR_AMBER
flash_border_outer = vectorio.Rectangle(
    pixel_shader=flash_border_pal, width=62, height=30, x=1, y=1,
)
flash_group.append(flash_border_outer)
flash_border_inner_pal = displayio.Palette(1)
flash_border_inner_pal[0] = COLOR_BLACK
flash_border_inner = vectorio.Rectangle(
    pixel_shader=flash_border_inner_pal, width=60, height=28, x=2, y=2,
)
flash_group.append(flash_border_inner)

flash_name_label = label.Label(font, text="", color=COLOR_AMBER, x=4, y=13)
flash_group.append(flash_name_label)
flash_action_label = label.Label(font, text="NEEDS INPUT", color=COLOR_WHITE, x=4, y=23)
flash_group.append(flash_action_label)

root.append(flash_group)

# Flash wipe animation state
FLASH_WIPE_STEP = 8    # pixels per frame
FLASH_WIPE_INTERVAL = 0.02  # seconds between frames
WIPE_NONE = 0
WIPE_IN = 1   # slide from right to center
WIPE_OUT = 2  # slide from center to left
flash_wipe_state = WIPE_NONE

# ---------------------------------------------------------------------------
# WiFi + HTTP
# ---------------------------------------------------------------------------


def boot_progress(pct, msg=""):
    """Update boot progress bar (0-100) and optional status text."""
    boot_bar.width = max(1, int(40 * pct / 100))
    if msg:
        boot_status.text = msg


def connect_wifi():
    """Connect to WiFi via ESP32 co-processor, retry on failure."""
    if esp.is_connected:
        return True
    if not WIFI_SSID:
        print("No WIFI_SSID configured")
        return False
    for attempt in range(3):
        try:
            boot_progress(20 + attempt * 20, "wifi...")
            print(f"WiFi connecting to {WIFI_SSID} (attempt {attempt + 1})...")
            esp.connect_AP(WIFI_SSID, WIFI_PASSWORD)
            print(f"WiFi connected: {esp.pretty_ip(esp.ip_address)}")
            boot_progress(80, "connected")
            return True
        except (ConnectionError, RuntimeError) as e:
            print(f"WiFi failed: {e}")
            time.sleep(2)
    return False


session = None


def init_http(hard_reset=False):
    """Create a fresh HTTP session. Hard reset power-cycles the ESP32 coprocessor."""
    global session
    if hard_reset:
        print("Hard-resetting ESP32...")
        esp.reset()
        time.sleep(1)
        if not esp.is_connected:
            connect_wifi()
    try:
        adafruit_connection_manager.connection_manager_close_all(esp)
    except Exception:
        pass
    pool = adafruit_connection_manager.get_radio_socketpool(esp)
    ssl_ctx = adafruit_connection_manager.get_radio_ssl_context(esp)
    session = adafruit_requests.Session(pool, ssl_ctx)


def fetch_status():
    """Fetch /status from the host server. Returns data dict or None on failure."""
    if session is None:
        return None

    headers = {"Connection": "close"}
    if SECRET:
        headers["Authorization"] = f"Bearer {SECRET}"
    try:
        resp = session.get(STATUS_URL, headers=headers, timeout=10)
        data = resp.json()
        resp.close()
        return data
    except (OSError, ValueError, RuntimeError) as e:
        print(f"Fetch error: {e}")
        return None


# ---------------------------------------------------------------------------
# Display update logic
# ---------------------------------------------------------------------------


def update_bar(bmp, palette, fill_px):
    """Update a usage bar bitmap to show fill_px filled pixels (0..BAR_WIDTH)."""
    fill_px = max(0, min(BAR_WIDTH, fill_px))
    bitmaptools.fill_region(bmp, 0, 0, fill_px, BAR_HEIGHT, 1)
    bitmaptools.fill_region(bmp, fill_px, 0, BAR_WIDTH, BAR_HEIGHT, 0)


def pct_to_px(pct):
    """Convert percentage to bar pixel width."""
    return max(0, min(BAR_WIDTH, int(BAR_WIDTH * pct / 100)))


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

# Bar animation state (smooth fill transitions)
bar_target_5h = 0   # target fill in pixels
bar_target_7d = 0
bar_current_5h = 0  # current fill in pixels (animated toward target)
bar_current_7d = 0
BAR_ANIM_INTERVAL = 0.05  # seconds between 1px steps

# Session slide transition state
SLIDE_STEP = 4        # pixels per frame
SLIDE_INTERVAL = 0.03  # seconds between frames
slide_phase = 0       # 0=idle, 1=sliding down, 2=snapping back with new content

# Breathing idle state (4-step palette cycle for separator + no-sessions text)
BREATHE_COLORS = (0x111133, 0x181848, 0x222266, 0x181848)  # dim→mid→bright→mid
BREATHE_TEXT_COLORS = (0x333333, 0x444444, 0x555555, 0x444444)
BREATHE_INTERVAL = 1.0  # seconds per step
breathe_step = 0


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

    global bar_target_5h, bar_target_7d

    usage = data.get("usage", {})
    five_h = usage.get("five_hour", {}).get("pct", 0)
    seven_d = usage.get("seven_day", {}).get("pct", 0)

    # Set bar animation targets (main loop animates current toward target)
    bar_target_5h = pct_to_px(five_h)
    bar_target_7d = pct_to_px(seven_d)
    bar_5h_pal[1] = bar_color(five_h)
    bar_7d_pal[1] = bar_color(seven_d)
    pct_5h_label.text = format_pct(five_h)
    pct_5h_label.color = bar_color(five_h)
    pct_7d_label.text = format_pct(seven_d)
    pct_7d_label.color = bar_color(seven_d)

    # Update sessions
    sessions = data.get("sessions", [])
    current_sessions = sessions

    # Reset separator color from breathing when sessions appear
    if sessions:
        sep_palette[0] = COLOR_SEPARATOR

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
            status = s["status"]

            if status == "blocked":
                session_labels[i].color = COLOR_WHITE
                session_dot_pals[i][0] = COLOR_RED_BRIGHT
            elif status == "waiting":
                session_labels[i].color = COLOR_WHITE
                session_dot_pals[i][0] = COLOR_AMBER
            else:
                session_labels[i].color = COLOR_WHITE_DIM
                session_dot_pals[i][0] = COLOR_TEAL

            session_dots[i].hidden = False
        else:
            session_labels[i].text = ""
            session_dots[i].hidden = True

    # Return all new transition names (for flash alert)
    return list(new_transitions) if new_transitions else None


def show_flash(session_name):
    """Show full-screen flash alert with wipe-in animation."""
    global flash_wipe_state
    flash_name_label.text = session_name
    name_width = len(session_name) * FONT_CHAR_WIDTH
    flash_name_label.x = max(3, (WIDTH - name_width) // 2)
    # Start off-screen right, wipe-in animation will slide to x=0
    flash_group.x = WIDTH
    flash_group.hidden = False
    flash_wipe_state = WIPE_IN


def hide_flash():
    """Start wipe-out animation (center → left)."""
    global flash_wipe_state
    flash_wipe_state = WIPE_OUT


def show_offline():
    """Show offline indicator in session area."""
    session_labels[0].text = "offline"
    session_labels[0].color = COLOR_RED
    session_dot_pals[0][0] = COLOR_RED
    session_dots[0].hidden = False
    for i in range(1, SESSION_SLOTS):
        session_labels[i].text = ""
        session_dots[i].hidden = True


def show_no_sessions():
    """Show empty state when no named sessions are active."""
    session_labels[0].text = "no sessions"
    session_labels[0].color = COLOR_GRAY
    session_dots[0].hidden = True
    for i in range(1, SESSION_SLOTS):
        session_labels[i].text = ""
        session_dots[i].hidden = True


# ---------------------------------------------------------------------------
# Animation tick functions (C1 refactor — each subsystem is self-contained)
# ---------------------------------------------------------------------------

# Shared timer/state for tick functions (module-level so main() stays clean)
_timers = {
    "poll": 0, "pulse": 0, "cycle": 0, "bar_anim": 0,
    "flash_wipe": 0, "slide": 0, "breathe": 0, "flash_start": 0,
}
_state = {
    "pulse_on": True, "is_flashing": False,
    "first_poll": True, "poll_failures": 0,
}


def tick_flash(now):
    """Manage flash overlay timer and wipe animation."""
    global flash_wipe_state
    s = _state
    if s["is_flashing"] and flash_wipe_state == WIPE_NONE and now - _timers["flash_start"] > FLASH_DURATION:
        hide_flash()

    if flash_wipe_state != WIPE_NONE and now - _timers["flash_wipe"] > FLASH_WIPE_INTERVAL:
        _timers["flash_wipe"] = now
        flash_group.x -= FLASH_WIPE_STEP
        if flash_wipe_state == WIPE_IN and flash_group.x <= 0:
            flash_group.x = 0
            flash_wipe_state = WIPE_NONE
        elif flash_wipe_state == WIPE_OUT and flash_group.x <= -WIDTH:
            flash_group.hidden = True
            flash_group.x = 0
            flash_wipe_state = WIPE_NONE
            s["is_flashing"] = False


def tick_bar_animation(now):
    """Step usage bar fills 1px toward their targets."""
    global bar_current_5h, bar_current_7d
    if now - _timers["bar_anim"] > BAR_ANIM_INTERVAL:
        _timers["bar_anim"] = now
        if bar_current_5h < bar_target_5h:
            bar_current_5h += 1
            update_bar(bar_5h_bmp, bar_5h_pal, bar_current_5h)
        elif bar_current_5h > bar_target_5h:
            bar_current_5h -= 1
            update_bar(bar_5h_bmp, bar_5h_pal, bar_current_5h)
        if bar_current_7d < bar_target_7d:
            bar_current_7d += 1
            update_bar(bar_7d_bmp, bar_7d_pal, bar_current_7d)
        elif bar_current_7d > bar_target_7d:
            bar_current_7d -= 1
            update_bar(bar_7d_bmp, bar_7d_pal, bar_current_7d)


def tick_poll(now):
    """Poll server, handle transitions and failures."""
    global current_sessions
    s = _state
    if now - _timers["poll"] <= POLL_INTERVAL:
        return
    _timers["poll"] = now
    data = fetch_status()
    if data:
        s["poll_failures"] = 0
        transitions = update_display(data)
        if transitions and not s["is_flashing"] and not s["first_poll"]:
            if len(transitions) == 1:
                show_flash(transitions[0])
            else:
                show_flash(f"{len(transitions)} waiting")
            _timers["flash_start"] = now
            s["is_flashing"] = True
        s["first_poll"] = False
        if not data.get("sessions"):
            show_no_sessions()
        gc.collect()
    else:
        s["poll_failures"] += 1
        if s["poll_failures"] == 2:
            print("Rebuilding HTTP session...")
            init_http()
        elif s["poll_failures"] >= 5:
            current_sessions = []
            show_offline()
            # Hard-reset ESP32 to clear all socket state
            init_http(hard_reset=True)
            s["poll_failures"] = 0


def tick_scroll(now):
    """Phase-based synchronized scrolling for long session names."""
    global scroll_phase, scroll_timer
    if _state["is_flashing"] or not current_sessions:
        return
    visible = get_visible_sessions()
    max_chars = (WIDTH - 9) // FONT_CHAR_WIDTH

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

    if scroll_phase == "pause" and now - scroll_timer >= SCROLL_PAUSE:
        needs_scroll = any(
            len(scroll_names[i]) > max_chars for i in range(SESSION_SLOTS)
        )
        scroll_phase = "scroll" if needs_scroll else "done"
        if needs_scroll:
            scroll_timer = now

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

    elif scroll_phase == "hold" and now - scroll_timer >= SCROLL_END_PAUSE:
        for i in range(min(len(visible), SESSION_SLOTS)):
            scroll_offsets[i] = 0
            name = scroll_names[i]
            session_labels[i].text = name[:max_chars] if name else ""
        scroll_phase = "settle"
        scroll_timer = now

    elif scroll_phase == "settle" and now - scroll_timer >= SCROLL_END_PAUSE:
        scroll_phase = "done"


def tick_breathe(now):
    """Pulse separator and text when no sessions are active."""
    global breathe_step
    if current_sessions or now - _timers["breathe"] <= BREATHE_INTERVAL:
        return
    _timers["breathe"] = now
    breathe_step = (breathe_step + 1) % len(BREATHE_COLORS)
    sep_palette[0] = BREATHE_COLORS[breathe_step]
    session_labels[0].color = BREATHE_TEXT_COLORS[breathe_step]


def tick_pulse(now):
    """Pulse waiting/blocked status dots via palette mutation."""
    s = _state
    if s["is_flashing"] or now - _timers["pulse"] <= PULSE_SPEED:
        return
    _timers["pulse"] = now
    s["pulse_on"] = not s["pulse_on"]
    visible = get_visible_sessions()
    for i in range(min(len(visible), SESSION_SLOTS)):
        st = visible[i]["status"]
        if st == "blocked":
            session_dot_pals[i][0] = COLOR_RED_BRIGHT if s["pulse_on"] else COLOR_RED_DIM
        elif st == "waiting":
            session_dot_pals[i][0] = COLOR_AMBER if s["pulse_on"] else COLOR_AMBER_DIM


def tick_cycle(now):
    """Trigger slide animation when sessions exceed display slots."""
    global slide_phase
    if _state["is_flashing"] or slide_phase != 0:
        return
    if scroll_phase not in ("idle", "done"):
        return
    if now - _timers["cycle"] <= CYCLE_SPEED:
        return
    _timers["cycle"] = now
    if len(current_sessions) > SESSION_SLOTS:
        slide_phase = 1
        _timers["slide"] = now


def tick_slide(now):
    """Animate session group slide transition."""
    global slide_phase, display_offset, scroll_phase
    if slide_phase == 0 or now - _timers["slide"] <= SLIDE_INTERVAL:
        return
    _timers["slide"] = now
    if slide_phase == 1:
        session_group.y += SLIDE_STEP
        if session_group.y >= SESSION_ROW_HEIGHT * SESSION_SLOTS:
            display_offset += 1
            visible = get_visible_sessions()
            for i in range(SESSION_SLOTS):
                if i < len(visible):
                    s = visible[i]
                    st = s["status"]
                    session_labels[i].color = COLOR_WHITE if st != "working" else COLOR_WHITE_DIM
                    session_dots[i].hidden = False
                    if st == "blocked":
                        session_dot_pals[i][0] = COLOR_RED_BRIGHT
                    elif st == "waiting":
                        session_dot_pals[i][0] = COLOR_AMBER
                    else:
                        session_dot_pals[i][0] = COLOR_TEAL
            for i in range(SESSION_SLOTS):
                scroll_names[i] = ""
                scroll_offsets[i] = 0
            scroll_phase = "idle"
            slide_phase = 2
    elif slide_phase == 2:
        session_group.y -= SLIDE_STEP
        if session_group.y <= 0:
            session_group.y = 0
            slide_phase = 0


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    # WiFi (boot_group showing during connect with progress bar)
    boot_progress(10, "init...")
    if not connect_wifi():
        retry = 0
        while not connect_wifi():
            retry += 1
            boot_progress(0, f"retry {retry}...")
            time.sleep(10)

    boot_progress(90, "ready")
    time.sleep(2)  # let ESP32 settle + show "ready"
    init_http()
    boot_progress(100, "")

    # Switch from boot splash to real UI
    time.sleep(0.5)
    display.root_group = root

    # Enable hardware watchdog — resets chip if main loop freezes for >30s
    # (HTTP timeout is 10s, so 30s covers worst case with margin)
    wdt = microcontroller.watchdog
    wdt.timeout = 16
    wdt.mode = WatchDogMode.RESET

    while True:
        wdt.feed()
        now = time.monotonic()
        tick_flash(now)
        tick_bar_animation(now)
        tick_poll(now)
        tick_scroll(now)
        tick_breathe(now)
        tick_pulse(now)
        tick_cycle(now)
        tick_slide(now)
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Crash-recovery wrapper — restarts main() on any exception
# ---------------------------------------------------------------------------

while True:
    try:
        main()
    except KeyboardInterrupt:
        break
    except Exception as e:
        print(f"CRASH: {e}")
        # Disable watchdog during recovery pause
        try:
            microcontroller.watchdog.mode = None
        except Exception:
            pass
        time.sleep(5)
        supervisor.reload()
