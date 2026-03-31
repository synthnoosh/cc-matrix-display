#!/usr/bin/env python3
"""cc-matrix-display host server.

Zero-dependency HTTP server that aggregates Claude Code session data and usage
stats into a single JSON endpoint for the Matrix Portal display.

Usage:
    python3 server.py                    # uses ~/.cc-matrix/config.json
    python3 server.py /path/to/config    # explicit config path
    CC_MATRIX_PORT=8321 python3 server.py
"""

import glob
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cc-matrix")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path.home() / ".cc-matrix" / "config.json"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
WAITING_PREFIX = "/tmp/claude-waiting-"
PENDING_PREFIX = "/tmp/claude-pending-"
PENDING_THRESHOLD = 10  # seconds before pending becomes "blocked"
USAGE_API = "https://api.anthropic.com/oauth/usage"
USAGE_CACHE_TTL = 60  # seconds


def load_config(path=None):
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    cfg = {"port": 8321, "secret": "", "bind": "0.0.0.0"}
    if p.exists():
        with open(p) as f:
            cfg.update(json.load(f))
    # Environment overrides
    cfg["port"] = int(os.environ.get("CC_MATRIX_PORT", cfg["port"]))
    cfg["secret"] = os.environ.get("CC_MATRIX_SECRET", cfg["secret"])
    cfg["bind"] = os.environ.get("CC_MATRIX_BIND", cfg["bind"])
    return cfg


# ---------------------------------------------------------------------------
# OAuth token retrieval (user-agnostic)
# ---------------------------------------------------------------------------

_token_cache = {"token": None, "ts": 0}


def get_oauth_token():
    """Extract Claude Code OAuth token. Cross-platform: reads the standard credentials file."""
    now = time.time()
    if _token_cache["token"] and now - _token_cache["ts"] < 300:
        return _token_cache["token"]

    token = (
        _try_macos_keychain()       # macOS: always up-to-date (fails gracefully on Linux)
        or _try_credentials_file()  # cross-platform: ~/.claude/.credentials.json
        or _try_env_var()           # last resort: explicit env var
    )
    if token:
        _token_cache["token"] = token
        _token_cache["ts"] = now
    return token


def _try_credentials_file():
    """Read from ~/.claude/.credentials.json (cross-platform, works on macOS + Linux)."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    try:
        with open(creds_path) as f:
            data = json.load(f)
        token = data.get("claudeAiOauth", {}).get("accessToken", "")
        if token and token.startswith("sk-ant-"):
            return token
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def _try_macos_keychain():
    """Fallback for macOS: read from Keychain."""
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode()
        m = re.search(r"sk-ant-oat01-[A-Za-z0-9_-]+", raw)
        if m:
            return m.group(0)
        # Try hex-decoded
        try:
            decoded = bytes.fromhex(raw.strip()).decode("utf-8", errors="ignore")
            m = re.search(r"sk-ant-oat01-[A-Za-z0-9_-]+", decoded)
            if m:
                return m.group(0)
        except ValueError:
            pass
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _try_env_var():
    """Last resort: explicit env var override."""
    return os.environ.get("CC_MATRIX_API_KEY")


# ---------------------------------------------------------------------------
# Usage fetcher (cached)
# ---------------------------------------------------------------------------

_usage_cache = {"data": None, "ts": 0}


def fetch_usage():
    """Fetch 5h/7d usage from Anthropic OAuth API. Cached for USAGE_CACHE_TTL."""
    now = time.time()
    if _usage_cache["data"] and now - _usage_cache["ts"] < USAGE_CACHE_TTL:
        return _usage_cache["data"]

    token = get_oauth_token()
    if not token:
        log.warning("No OAuth token available — usage will be empty")
        return None

    try:
        req = urllib.request.Request(
            USAGE_API,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "cc-matrix-display/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        result = {
            "five_hour": {
                "pct": round(data.get("five_hour", {}).get("utilization", 0)),
                "resets_at": data.get("five_hour", {}).get("resets_at", ""),
            },
            "seven_day": {
                "pct": round(data.get("seven_day", {}).get("utilization", 0)),
                "resets_at": data.get("seven_day", {}).get("resets_at", ""),
            },
        }
        _usage_cache["data"] = result
        _usage_cache["ts"] = now
        return result

    except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
        log.warning("Usage fetch failed: %s", e)
        return _usage_cache["data"]  # return stale if available


# ---------------------------------------------------------------------------
# Session enumeration (named only)
# ---------------------------------------------------------------------------


def get_named_sessions():
    """Return list of named, alive Claude Code sessions with waiting status."""
    sessions = []
    if not SESSIONS_DIR.exists():
        return sessions

    for path in SESSIONS_DIR.glob("*.json"):
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # Only include named sessions
        name = data.get("name")
        if not name:
            continue

        pid = data.get("pid")
        if not pid:
            continue

        # Check if process is alive
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            continue

        session_id = data.get("sessionId", "")
        waiting_flag = Path(f"{WAITING_PREFIX}{session_id}")
        pending_flag = Path(f"{PENDING_PREFIX}{session_id}")

        if waiting_flag.exists():
            status = "waiting"
        elif pending_flag.exists():
            try:
                age = time.time() - pending_flag.stat().st_mtime
                status = "blocked" if age > PENDING_THRESHOLD else "working"
            except OSError:
                status = "working"
        else:
            status = "working"

        sessions.append({"name": name, "status": status})

    # Sort: blocked first, then waiting, then working
    priority = {"blocked": 0, "waiting": 1, "working": 2}
    sessions.sort(key=lambda s: (priority.get(s["status"], 2), s["name"]))
    return sessions


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class MatrixHandler(BaseHTTPRequestHandler):
    server_version = "cc-matrix-display/1.0"

    def log_message(self, format, *args):
        log.debug(format, *args)

    def _check_auth(self):
        secret = self.server.config.get("secret", "")
        if not secret:
            return True
        auth = self.headers.get("Authorization", "")
        return auth == f"Bearer {secret}"

    def _json_response(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json_response(200, {"ok": True})
            return

        if self.path != "/status":
            self._json_response(404, {"error": "not found"})
            return

        if not self._check_auth():
            self._json_response(401, {"error": "unauthorized"})
            return

        usage = fetch_usage()
        sessions = get_named_sessions()

        payload = {
            "usage": usage or {
                "five_hour": {"pct": 0, "resets_at": ""},
                "seven_day": {"pct": 0, "resets_at": ""},
            },
            "sessions": sessions,
            "ts": int(time.time()),
        }
        self._json_response(200, payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_config(config_path)

    HTTPServer.allow_reuse_address = True
    server = HTTPServer((config["bind"], config["port"]), MatrixHandler)
    server.config = config

    log.info(
        "cc-matrix-display server starting on %s:%d",
        config["bind"],
        config["port"],
    )
    if config["secret"]:
        log.info("Auth enabled (Bearer token required)")
    else:
        log.warning("Auth DISABLED — set 'secret' in config for security")

    signal.signal(signal.SIGTERM, lambda *_: (server.shutdown(), sys.exit(0)))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
