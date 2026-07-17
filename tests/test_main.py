"""Tests for Claude Code session detection, PID management, and RPC logic."""

import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claudecode_discord_presence.main import (
    connect_rpc,
    find_latest_jsonl_mtime,
    get_claude_projects_dir,
    is_claude_running,
    is_session_active,
)


# --- get_claude_projects_dir ---


class TestGetClaudeProjectsDir:
    def test_returns_path(self):
        result = get_claude_projects_dir()
        assert isinstance(result, Path)
        assert result.parts[-2:] == (".claude", "projects")


# --- find_latest_jsonl_mtime ---


class TestFindLatestJsonlMtime:
    def test_no_directory(self, tmp_path: Path):
        nonexistent = tmp_path / "nonexistent"
        assert find_latest_jsonl_mtime(nonexistent) is None

    def test_empty_directory(self, tmp_path: Path):
        assert find_latest_jsonl_mtime(tmp_path) is None

    def test_no_jsonl_files(self, tmp_path: Path):
        (tmp_path / "readme.txt").write_text("hello")
        assert find_latest_jsonl_mtime(tmp_path) is None

    def test_single_jsonl(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text('{"msg": "hello"}\n')
        result = find_latest_jsonl_mtime(tmp_path)
        assert result is not None
        assert abs(result - time.time()) < 5

    def test_multiple_jsonl_returns_latest(self, tmp_path: Path):
        old = tmp_path / "old.jsonl"
        old.write_text('{"msg": "old"}\n')
        old_mtime = old.stat().st_mtime

        time.sleep(0.1)

        new = tmp_path / "new.jsonl"
        new.write_text('{"msg": "new"}\n')
        new_mtime = new.stat().st_mtime

        result = find_latest_jsonl_mtime(tmp_path)
        assert result == new_mtime
        assert result >= old_mtime

    def test_nested_jsonl(self, tmp_path: Path):
        subdir = tmp_path / "project-abc"
        subdir.mkdir()
        f = subdir / "session.jsonl"
        f.write_text('{"msg": "nested"}\n')

        result = find_latest_jsonl_mtime(tmp_path)
        assert result is not None

    def test_empty_jsonl_file(self, tmp_path: Path):
        """A 0-byte .jsonl file should still be detected by mtime."""
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        result = find_latest_jsonl_mtime(tmp_path)
        assert result is not None

    def test_deeply_nested_jsonl(self, tmp_path: Path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        f = deep / "session.jsonl"
        f.write_text('{"msg": "deep"}\n')
        result = find_latest_jsonl_mtime(tmp_path)
        assert result is not None

    def test_permission_error_returns_none(self, tmp_path: Path):
        """If rglob raises OSError, return None gracefully."""
        with patch.object(Path, "rglob", side_effect=OSError("permission denied")):
            assert find_latest_jsonl_mtime(tmp_path) is None


# --- is_session_active ---


class TestIsSessionActive:
    def test_no_files_returns_false(self, tmp_path: Path):
        assert is_session_active(tmp_path, 600) is False

    def test_recent_file_returns_true(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text('{"msg": "active"}\n')
        assert is_session_active(tmp_path, 600) is True

    def test_stale_file_returns_false(self, tmp_path: Path):
        f = tmp_path / "session.jsonl"
        f.write_text('{"msg": "stale"}\n')
        # Set mtime to 20 minutes ago to guarantee staleness
        old_time = time.time() - 1200
        os.utime(f, (old_time, old_time))
        assert is_session_active(tmp_path, 600) is False

    def test_nonexistent_dir_returns_false(self, tmp_path: Path):
        nonexistent = tmp_path / "nonexistent"
        assert is_session_active(nonexistent, 600) is False

    def test_negative_timeout_returns_false(self, tmp_path: Path):
        """Negative timeout should never match."""
        f = tmp_path / "session.jsonl"
        f.write_text('{"msg": "test"}\n')
        assert is_session_active(tmp_path, -1) is False

    def test_exact_boundary_timeout(self, tmp_path: Path):
        """File modified exactly at timeout boundary."""
        f = tmp_path / "session.jsonl"
        f.write_text('{"msg": "test"}\n')
        mtime = f.stat().st_mtime
        # timeout_sec=0 means (time.time() - mtime) < 0 is always False
        # for a file just written (mtime ~ now), so this should be False
        assert is_session_active(tmp_path, 0) is False


# --- is_claude_running ---


class TestIsClaudeRunning:
    def test_returns_bool(self):
        result = is_claude_running()
        assert isinstance(result, bool)

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_oserror_returns_false(self, mock_run, monkeypatch):
        """On Windows, an OSError from tasklist must yield False, not raise."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        mock_run.side_effect = OSError("command not found")
        assert is_claude_running() is False
        mock_run.assert_called_once()

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_empty_stdout(self, mock_run):
        """Empty tasklist output should return False."""
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        with patch("claudecode_discord_presence.main.sys") as mock_sys:
            mock_sys.platform = "win32"
            from claudecode_discord_presence import main as main_mod
            original = main_mod.CLAUDE_PROCESS_NAME
            main_mod.CLAUDE_PROCESS_NAME = "claude.exe"
            result = main_mod.is_claude_running()
            main_mod.CLAUDE_PROCESS_NAME = original
        assert result is False

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_info_message_no_match(self, mock_run):
        """tasklist 'INFO: No tasks' message should return False."""
        mock_run.return_value = MagicMock(
            stdout="INFO: No tasks are running which match the specified criteria.",
            returncode=0,
        )
        with patch("claudecode_discord_presence.main.sys") as mock_sys:
            mock_sys.platform = "win32"
            from claudecode_discord_presence import main as main_mod
            original = main_mod.CLAUDE_PROCESS_NAME
            main_mod.CLAUDE_PROCESS_NAME = "claude.exe"
            result = main_mod.is_claude_running()
            main_mod.CLAUDE_PROCESS_NAME = original
        assert result is False


# --- connect_rpc ---


class TestConnectRpc:
    @patch("claudecode_discord_presence.main.Presence")
    def test_successful_connection(self, mock_presence_cls):
        """Successful RPC connection should return a Presence instance."""
        mock_instance = MagicMock()
        mock_presence_cls.return_value = mock_instance
        result = connect_rpc("1234567890")
        assert result is mock_instance
        mock_instance.connect.assert_called_once()

    @patch("claudecode_discord_presence.main.Presence")
    def test_connection_refused(self, mock_presence_cls):
        """If Discord is not running, connect() raises and we return None."""
        mock_instance = MagicMock()
        mock_instance.connect.side_effect = ConnectionRefusedError("Discord not running")
        mock_presence_cls.return_value = mock_instance
        result = connect_rpc("1234567890")
        assert result is None

    @patch("claudecode_discord_presence.main.Presence")
    def test_connection_timeout(self, mock_presence_cls):
        """Timeout during connect should return None."""
        mock_instance = MagicMock()
        mock_instance.connect.side_effect = TimeoutError("connection timed out")
        mock_presence_cls.return_value = mock_instance
        result = connect_rpc("1234567890")
        assert result is None

    @patch("claudecode_discord_presence.main.Presence")
    def test_invalid_client_id(self, mock_presence_cls):
        """Invalid client ID causing an exception should return None."""
        mock_presence_cls.side_effect = Exception("Invalid client ID")
        result = connect_rpc("invalid")
        assert result is None

    @patch("claudecode_discord_presence.main.Presence")
    def test_unexpected_exception(self, mock_presence_cls):
        """Any unexpected exception should return None, not propagate."""
        mock_instance = MagicMock()
        mock_instance.connect.side_effect = RuntimeError("unexpected")
        mock_presence_cls.return_value = mock_instance
        result = connect_rpc("1234567890")
        assert result is None


# --- _reconcile_presence ---


class TestReconcilePresence:
    def test_activates_and_shows_presence(self, monkeypatch):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        monkeypatch.setattr(m, "connect_rpc", lambda cid: mock_rpc)
        rpc, active = m._reconcile_presence(True, False, None)
        assert rpc is mock_rpc
        assert active is True
        mock_rpc.update.assert_called_once()

    def test_activation_update_failure_drops_rpc(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        mock_rpc.update.side_effect = BrokenPipeError()
        rpc, active = m._reconcile_presence(True, False, mock_rpc)
        assert rpc is None
        assert active is False

    def test_idle_clears_presence(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        rpc, active = m._reconcile_presence(False, True, mock_rpc)
        assert rpc is mock_rpc
        assert active is False
        mock_rpc.clear.assert_called_once()

    def test_idle_clear_failure_drops_rpc(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        mock_rpc.clear.side_effect = OSError()
        rpc, active = m._reconcile_presence(False, True, mock_rpc)
        assert rpc is None
        assert active is False

    def test_continue_updates(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        rpc, active = m._reconcile_presence(True, True, mock_rpc)
        assert rpc is mock_rpc
        assert active is True
        mock_rpc.update.assert_called_once()

    def test_continue_update_failure_drops_rpc(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        mock_rpc.update.side_effect = ConnectionResetError()
        rpc, active = m._reconcile_presence(True, True, mock_rpc)
        assert rpc is None
        assert active is False


# --- _drop_rpc ---


class TestDropRpc:
    def test_none_is_safe(self):
        from claudecode_discord_presence.main import _drop_rpc
        assert _drop_rpc(None) is None

    def test_clears_and_closes(self):
        from claudecode_discord_presence.main import _drop_rpc
        rpc = MagicMock()
        assert _drop_rpc(rpc) is None
        rpc.clear.assert_called_once()
        rpc.close.assert_called_once()

    def test_close_called_even_if_clear_raises(self):
        from claudecode_discord_presence.main import _drop_rpc
        rpc = MagicMock()
        rpc.clear.side_effect = OSError("ipc")
        assert _drop_rpc(rpc) is None
        rpc.close.assert_called_once()

    def test_no_raise_if_close_raises(self):
        from claudecode_discord_presence.main import _drop_rpc
        rpc = MagicMock()
        rpc.close.side_effect = OSError("ipc")
        assert _drop_rpc(rpc) is None  # must not raise


# --- _run_daemon lifecycle ---


class TestRunDaemonLifecycle:
    def test_returns_when_lock_unavailable(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = False
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        # Must not raise and must not enter the loop.
        m._run_daemon()
        fake_lock.acquire.assert_called_once()
        fake_lock.release.assert_not_called()

    def test_stop_sentinel_exits_and_cleans_up(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        monkeypatch.setattr(m, "_stop_requested", lambda: True)  # stop on first check
        cleared = []
        monkeypatch.setattr(m, "_clear_own_stop_sentinel", lambda: cleared.append(True))
        m._run_daemon()
        fake_lock.release.assert_called_once()
        assert cleared == [True]

    def test_releases_lock_on_exception(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        monkeypatch.setattr(m, "_stop_requested", lambda: False)

        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(m, "is_claude_running", _boom)
        with pytest.raises(RuntimeError):
            m._run_daemon()
        fake_lock.release.assert_called_once()

    def test_logs_traceback_on_unexpected_exception(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: None)
        monkeypatch.setattr(m, "_stop_requested", lambda: False)
        monkeypatch.setattr(m, "_clear_own_stop_sentinel", lambda: None)
        mock_logger = MagicMock()
        monkeypatch.setattr(m, "logger", mock_logger)

        def _boom():
            raise RuntimeError("boom")

        monkeypatch.setattr(m, "is_claude_running", _boom)
        with pytest.raises(RuntimeError):
            m._run_daemon()
        # The traceback must be logged (stderr is DEVNULL for the detached daemon).
        mock_logger.exception.assert_called_once()
        fake_lock.release.assert_called_once()


class TestStopRequested:
    def test_no_sentinel(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m, "STOP_FILE", tmp_path / "stop")
        assert m._stop_requested() is False

    def test_sentinel_own_pid(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text(str(os.getpid()))
        monkeypatch.setattr(m, "STOP_FILE", f)
        assert m._stop_requested() is True

    def test_sentinel_other_pid_is_cleaned(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text("999999")
        monkeypatch.setattr(m, "STOP_FILE", f)
        assert m._stop_requested() is False
        assert not f.exists()

    def test_sentinel_garbage(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text("not-a-pid")
        monkeypatch.setattr(m, "STOP_FILE", f)
        assert m._stop_requested() is False


class TestSleepUntilPoll:
    def test_true_immediately_when_stop(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text(str(os.getpid()))
        monkeypatch.setattr(m, "STOP_FILE", f)
        slept = []
        monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
        assert m._sleep_until_poll() is True
        assert slept == []

    def test_sleeps_full_interval_without_stop(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m, "STOP_FILE", tmp_path / "absent")
        slept = []
        monkeypatch.setattr(m.time, "sleep", lambda s: slept.append(s))
        assert m._sleep_until_poll() is False
        expected = len(range(0, m.POLL_INTERVAL_SEC, m.STOP_POLL_SEC))
        assert len(slept) == expected


class TestClearOwnStopSentinel:
    def test_removes_own(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text(str(os.getpid()))
        monkeypatch.setattr(m, "STOP_FILE", f)
        m._clear_own_stop_sentinel()
        assert not f.exists()

    def test_keeps_other_pid(self, tmp_path, monkeypatch):
        from claudecode_discord_presence import main as m
        f = tmp_path / "stop"
        f.write_text("999999")
        monkeypatch.setattr(m, "STOP_FILE", f)
        m._clear_own_stop_sentinel()
        assert f.exists()
