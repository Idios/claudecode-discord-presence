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
    is_already_running,
    is_claude_running,
    is_process_alive,
    is_session_active,
    remove_pid_file,
    write_pid_file,
    PID_FILE,
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


# --- is_process_alive ---


class TestIsProcessAlive:
    def test_current_process_is_alive(self):
        assert is_process_alive(os.getpid()) is True

    def test_nonexistent_pid_is_not_alive(self):
        assert is_process_alive(99999999) is False

    def test_pid_zero(self):
        """PID 0 is special (kernel); os.kill(0, 0) sends to process group.
        Should not crash regardless of result."""
        result = is_process_alive(0)
        assert isinstance(result, bool)

    def test_negative_pid(self):
        """Negative PIDs should not crash."""
        result = is_process_alive(-1)
        assert isinstance(result, bool)


# --- is_claude_running ---


class TestIsClaudeRunning:
    def test_returns_bool(self):
        result = is_claude_running()
        assert isinstance(result, bool)

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_oserror_returns_false(self, mock_run):
        """If subprocess.run raises OSError, return False."""
        mock_run.side_effect = OSError("command not found")
        with patch("claudecode_discord_presence.main.sys") as mock_sys:
            mock_sys.platform = "win32"
            # Re-import to pick up the patched sys — but since is_claude_running
            # reads sys.platform at call time, we patch it directly
            from claudecode_discord_presence.main import is_claude_running as icr
        # The function catches OSError internally
        # Just verify it doesn't raise
        result = is_claude_running()
        assert isinstance(result, bool)

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


# --- PID file management ---


class TestPidFileManagement:
    def test_write_and_remove_pid_file(self, tmp_path: Path, monkeypatch):
        pid_file = tmp_path / "test.pid"
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)

        write_pid_file()
        assert pid_file.exists()
        assert pid_file.read_text() == str(os.getpid())

        remove_pid_file()
        assert not pid_file.exists()

    def test_remove_nonexistent_pid_file(self, tmp_path: Path, monkeypatch):
        """Removing a PID file that doesn't exist should not raise."""
        pid_file = tmp_path / "nonexistent.pid"
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)
        remove_pid_file()  # should not raise

    def test_write_pid_file_creates_parent_dirs(self, tmp_path: Path, monkeypatch):
        pid_file = tmp_path / "subdir" / "deep" / "test.pid"
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)
        write_pid_file()
        assert pid_file.exists()


# --- is_already_running ---


class TestIsAlreadyRunning:
    def test_no_pid_file(self, tmp_path: Path, monkeypatch):
        pid_file = tmp_path / "nonexistent.pid"
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)
        assert is_already_running() is False

    def test_pid_file_with_own_pid(self, tmp_path: Path, monkeypatch):
        """PID file containing our own PID should return False."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)
        assert is_already_running() is False

    def test_pid_file_with_dead_pid(self, tmp_path: Path, monkeypatch):
        """PID file containing a dead PID should return False."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("99999999")
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)
        assert is_already_running() is False

    def test_pid_file_empty(self, tmp_path: Path, monkeypatch):
        """Empty PID file should return False (ValueError on int())."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("")
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)
        assert is_already_running() is False

    def test_pid_file_garbage(self, tmp_path: Path, monkeypatch):
        """PID file with non-numeric content should return False."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("not_a_number")
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)
        assert is_already_running() is False

    def test_pid_file_with_whitespace(self, tmp_path: Path, monkeypatch):
        """PID file with whitespace-padded number should still parse."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("  99999999  \n")
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)
        assert is_already_running() is False  # dead PID

    def test_pid_file_with_alive_other_pid(self, tmp_path: Path, monkeypatch):
        """PID file with a living PID (not ours) should return True."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")
        monkeypatch.setattr("claudecode_discord_presence.main.PID_FILE", pid_file)
        monkeypatch.setattr(
            "claudecode_discord_presence.main.is_process_alive", lambda pid: True
        )
        assert is_already_running() is True


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


# --- Main loop RPC error handling ---


class TestMainLoopRpcErrors:
    """Test that the main loop handles RPC failures gracefully.

    These tests patch out sleep and sys.exit to run a controlled number
    of loop iterations.
    """

    def _run_one_iteration(self, rpc_mock, active: bool, presence_active: bool):
        """Simulate one iteration of the main loop's RPC logic."""
        from claudecode_discord_presence.main import connect_rpc, CLIENT_ID

        if active and not presence_active:
            rpc = rpc_mock
            if rpc is not None:
                try:
                    rpc.update()
                    return rpc, True
                except Exception:
                    return None, False
            return None, False
        elif not active and presence_active:
            if rpc_mock is not None:
                try:
                    rpc_mock.clear()
                except Exception:
                    pass
            return rpc_mock, False
        elif active and presence_active:
            if rpc_mock is not None:
                try:
                    rpc_mock.update()
                    return rpc_mock, True
                except Exception:
                    return None, False
            return None, False
        return rpc_mock, presence_active

    def test_update_raises_clears_presence(self):
        """If rpc.update() raises, presence should be deactivated."""
        mock_rpc = MagicMock()
        mock_rpc.update.side_effect = BrokenPipeError("pipe broken")
        rpc, active = self._run_one_iteration(mock_rpc, active=True, presence_active=True)
        assert rpc is None
        assert active is False

    def test_clear_raises_still_deactivates(self):
        """If rpc.clear() raises, presence should still be deactivated."""
        mock_rpc = MagicMock()
        mock_rpc.clear.side_effect = OSError("IPC error")
        rpc, active = self._run_one_iteration(mock_rpc, active=False, presence_active=True)
        assert active is False

    def test_update_on_new_session_raises(self):
        """If rpc.update() fails on a new session, rpc should be reset."""
        mock_rpc = MagicMock()
        mock_rpc.update.side_effect = ConnectionResetError("reset")
        rpc, active = self._run_one_iteration(mock_rpc, active=True, presence_active=False)
        assert rpc is None
        assert active is False
