"""Unit tests for cc-matrix-display host server -- pure/testable functions."""

import json
import os
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import host.server as server


class TestLoadConfig(unittest.TestCase):
    """Tests for load_config(): defaults < file < env vars."""

    def _clean_env(self, extras=None):
        """Return env dict with all CC_MATRIX_* vars removed, plus optional extras."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("CC_MATRIX_")}
        if extras:
            env.update(extras)
        return env

    def test_defaults_when_no_file(self):
        """Default values returned when config file doesn't exist."""
        with tempfile.TemporaryDirectory() as td:
            with patch.dict(os.environ, self._clean_env(), clear=True):
                cfg = server.load_config(os.path.join(td, "nope.json"))
        self.assertEqual(cfg["port"], 8321)
        self.assertEqual(cfg["secret"], "")
        self.assertEqual(cfg["bind"], "0.0.0.0")

    def test_file_overrides_defaults(self):
        """Values from config file override defaults."""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "config.json")
            Path(p).write_text(json.dumps({"port": 9999, "secret": "file-secret"}))
            with patch.dict(os.environ, self._clean_env(), clear=True):
                cfg = server.load_config(p)
        self.assertEqual(cfg["port"], 9999)
        self.assertEqual(cfg["secret"], "file-secret")
        self.assertEqual(cfg["bind"], "0.0.0.0")

    def test_env_overrides_file(self):
        """Environment variables override file values."""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "config.json")
            Path(p).write_text(json.dumps({"port": 9999, "secret": "file-secret"}))
            extras = {"CC_MATRIX_PORT": "1234", "CC_MATRIX_SECRET": "env-secret"}
            with patch.dict(os.environ, self._clean_env(extras), clear=True):
                cfg = server.load_config(p)
        self.assertEqual(cfg["port"], 1234)
        self.assertEqual(cfg["secret"], "env-secret")

    def test_port_env_non_numeric_raises(self):
        """Non-numeric CC_MATRIX_PORT raises ValueError."""
        with tempfile.TemporaryDirectory() as td:
            extras = {"CC_MATRIX_PORT": "not-a-number"}
            with patch.dict(os.environ, self._clean_env(extras), clear=True):
                with self.assertRaises(ValueError):
                    server.load_config(os.path.join(td, "nope.json"))


class TestCheckAuth(unittest.TestCase):
    """Tests for MatrixHandler._check_auth()."""

    def _make_handler(self, secret, auth_header):
        handler = MagicMock(spec=server.MatrixHandler)
        handler.server = MagicMock()
        handler.server.config = {"secret": secret}
        handler.headers = {"Authorization": auth_header} if auth_header else {}
        handler._check_auth = server.MatrixHandler._check_auth.__get__(handler)
        return handler

    def test_empty_secret_allows_all(self):
        """When secret is empty, all requests are allowed."""
        self.assertTrue(self._make_handler("", "")._check_auth())

    def test_valid_token_passes(self):
        """Correct Bearer token passes auth."""
        self.assertTrue(self._make_handler("my-secret", "Bearer my-secret")._check_auth())

    def test_invalid_token_fails(self):
        """Wrong Bearer token is rejected."""
        self.assertFalse(self._make_handler("my-secret", "Bearer wrong")._check_auth())

    def test_missing_header_fails(self):
        """Missing Authorization header is rejected when secret is set."""
        self.assertFalse(self._make_handler("my-secret", None)._check_auth())


class TestGetNamedSessions(unittest.TestCase):
    """Tests for get_named_sessions()."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_sessions_dir = server.SESSIONS_DIR
        self._orig_waiting = server.WAITING_PREFIX
        self._orig_pending = server.PENDING_PREFIX
        server.SESSIONS_DIR = Path(self.tmpdir)
        server.WAITING_PREFIX = os.path.join(self.tmpdir, "claude-waiting-")
        server.PENDING_PREFIX = os.path.join(self.tmpdir, "claude-pending-")

    def tearDown(self):
        server.SESSIONS_DIR = self._orig_sessions_dir
        server.WAITING_PREFIX = self._orig_waiting
        server.PENDING_PREFIX = self._orig_pending
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_session(self, filename, data):
        (Path(self.tmpdir) / filename).write_text(json.dumps(data))

    def test_empty_dir_returns_empty(self):
        """Empty sessions dir returns empty list."""
        self.assertEqual(server.get_named_sessions(), [])

    def test_unnamed_sessions_skipped(self):
        """Sessions without a 'name' field are excluded."""
        self._write_session("123.json", {"pid": 999, "sessionId": "abc"})
        with patch("os.kill"):
            self.assertEqual(server.get_named_sessions(), [])

    def test_dead_pid_filtered(self):
        """Sessions whose PID is not alive are excluded."""
        self._write_session("123.json", {"name": "test", "pid": 99999, "sessionId": "abc"})
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertEqual(server.get_named_sessions(), [])

    def test_working_status_no_flags(self):
        """Session with no waiting/pending flag files is 'working'."""
        self._write_session("1.json", {"name": "build", "pid": 42, "sessionId": "sess1"})
        with patch("os.kill"):
            result = server.get_named_sessions()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "build")
        self.assertEqual(result[0]["status"], "working")

    def test_waiting_status_from_flag(self):
        """Session with waiting flag file gets 'waiting' status."""
        self._write_session("1.json", {"name": "deploy", "pid": 42, "sessionId": "sess2"})
        Path(f"{server.WAITING_PREFIX}sess2").touch()
        with patch("os.kill"):
            result = server.get_named_sessions()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "waiting")

    def test_blocked_status_from_old_pending(self):
        """Pending flag older than PENDING_THRESHOLD yields 'blocked'."""
        self._write_session("1.json", {"name": "review", "pid": 42, "sessionId": "sess3"})
        pending = Path(f"{server.PENDING_PREFIX}sess3")
        pending.touch()
        old_time = time.time() - 20  # > PENDING_THRESHOLD(10), < PENDING_EXPIRY(60)
        os.utime(pending, (old_time, old_time))
        with patch("os.kill"):
            result = server.get_named_sessions()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "blocked")

    def test_sort_order_blocked_waiting_working(self):
        """Sessions sort: blocked first, then waiting, then working."""
        self._write_session("1.json", {"name": "aaa-working", "pid": 10, "sessionId": "s1"})
        self._write_session("2.json", {"name": "bbb-waiting", "pid": 20, "sessionId": "s2"})
        self._write_session("3.json", {"name": "ccc-blocked", "pid": 30, "sessionId": "s3"})

        Path(f"{server.WAITING_PREFIX}s2").touch()
        pending = Path(f"{server.PENDING_PREFIX}s3")
        pending.touch()
        old_time = time.time() - 20
        os.utime(pending, (old_time, old_time))

        with patch("os.kill"):
            result = server.get_named_sessions()

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["status"], "blocked")
        self.assertEqual(result[1]["status"], "waiting")
        self.assertEqual(result[2]["status"], "working")


class TestFetchUsage(unittest.TestCase):
    """Tests for fetch_usage(): response normalization."""

    def setUp(self):
        server._usage_cache["data"] = None
        server._usage_cache["ts"] = 0

    def test_response_shape(self):
        """API response is normalized to {five_hour: {pct, resets_at}, seven_day: {pct, resets_at}}."""
        fake_body = json.dumps({
            "five_hour": {"utilization": 42.7, "resets_at": "2026-04-01T12:00:00Z"},
            "seven_day": {"utilization": 85.3, "resets_at": "2026-04-07T00:00:00Z"},
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = fake_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("host.server.get_oauth_token", return_value="sk-ant-fake"):
            with patch("urllib.request.urlopen", return_value=mock_resp):
                result = server.fetch_usage()

        self.assertIn("five_hour", result)
        self.assertIn("seven_day", result)
        self.assertEqual(result["five_hour"]["pct"], 43)
        self.assertEqual(result["five_hour"]["resets_at"], "2026-04-01T12:00:00Z")
        self.assertEqual(result["seven_day"]["pct"], 85)
        self.assertEqual(result["seven_day"]["resets_at"], "2026-04-07T00:00:00Z")

    def test_no_token_returns_none(self):
        """When no OAuth token is available, returns None."""
        with patch("host.server.get_oauth_token", return_value=None):
            self.assertIsNone(server.fetch_usage())

    def test_cache_returns_stale_on_url_error(self):
        """On URLError, returns stale cached data."""
        cached = {
            "five_hour": {"pct": 10, "resets_at": "old"},
            "seven_day": {"pct": 20, "resets_at": "old"},
        }
        server._usage_cache["data"] = cached
        server._usage_cache["ts"] = 0

        with patch("host.server.get_oauth_token", return_value="sk-ant-fake"):
            with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("fail")):
                result = server.fetch_usage()

        self.assertEqual(result, cached)


if __name__ == "__main__":
    unittest.main()
