"""Tests for Claude Code session detection, PID management, and RPC logic."""

import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claudecode_discord_presence.main import (
    Tool,
    connect_rpc,
    find_latest_jsonl_mtime,
    get_claude_projects_dir,
    is_claude_running,
    is_session_active,
    resolve_active_tool,
    resolve_tools,
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

    def test_exact_boundary_timeout(self, tmp_path: Path, monkeypatch):
        """At the exact boundary (age == timeout_sec), the session is NOT active.

        The clock and the file mtime are both pinned so the result cannot flip
        on sub-millisecond skew between time.time() and the filesystem (the old
        Windows flake).
        """
        from claudecode_discord_presence import main as m
        f = tmp_path / "session.jsonl"
        f.write_text("{}")
        os.utime(f, (1000.0, 1000.0))
        monkeypatch.setattr(m.time, "time", lambda: 1100.0)  # age == 100s exactly
        assert m.is_session_active(tmp_path, 100) is False  # age not < timeout
        assert m.is_session_active(tmp_path, 101) is True   # age < timeout


# --- is_claude_running ---


class TestIsClaudeRunning:
    def test_returns_bool(self):
        result = is_claude_running()
        assert isinstance(result, bool)

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_oserror_returns_false(self, mock_run, monkeypatch):
        """An OSError from tasklist yields False, not a raise."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        # CREATE_NO_WINDOW is a Windows-only subprocess attribute; provide it so
        # the forced win32 branch is runnable on POSIX CI runners.
        monkeypatch.setattr(
            "claudecode_discord_presence.main.subprocess.CREATE_NO_WINDOW", 0,
            raising=False,
        )
        mock_run.side_effect = OSError("command not found")
        assert is_claude_running() is False
        mock_run.assert_called_once()

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_empty_stdout(self, mock_run, monkeypatch):
        """Empty tasklist output returns False."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        # CREATE_NO_WINDOW is a Windows-only subprocess attribute; provide it so
        # the forced win32 branch is runnable on POSIX CI runners.
        monkeypatch.setattr(
            "claudecode_discord_presence.main.subprocess.CREATE_NO_WINDOW", 0,
            raising=False,
        )
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "claude.exe")
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        assert is_claude_running() is False

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_info_message_no_match(self, mock_run, monkeypatch):
        """The 'INFO: No tasks' message must not count as a match."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        # CREATE_NO_WINDOW is a Windows-only subprocess attribute; provide it so
        # the forced win32 branch is runnable on POSIX CI runners.
        monkeypatch.setattr(
            "claudecode_discord_presence.main.subprocess.CREATE_NO_WINDOW", 0,
            raising=False,
        )
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "claude.exe")
        mock_run.return_value = MagicMock(
            stdout="INFO: No tasks are running which match the specified criteria.",
            returncode=0,
        )
        assert is_claude_running() is False

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_matches_process_row(self, mock_run, monkeypatch):
        """A real tasklist row starting with the image name returns True."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        # CREATE_NO_WINDOW is a Windows-only subprocess attribute; provide it so
        # the forced win32 branch is runnable on POSIX CI runners.
        monkeypatch.setattr(
            "claudecode_discord_presence.main.subprocess.CREATE_NO_WINDOW", 0,
            raising=False,
        )
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "claude.exe")
        mock_run.return_value = MagicMock(
            stdout="claude.exe                    1234 Console                1     50,000 K",
            returncode=0,
        )
        assert is_claude_running() is True

    @patch("claudecode_discord_presence.main.subprocess.run")
    def test_tasklist_substring_is_not_a_match(self, mock_run, monkeypatch):
        """A line merely CONTAINING the name (not starting with it) is not a match."""
        monkeypatch.setattr("claudecode_discord_presence.main.sys.platform", "win32")
        # CREATE_NO_WINDOW is a Windows-only subprocess attribute; provide it so
        # the forced win32 branch is runnable on POSIX CI runners.
        monkeypatch.setattr(
            "claudecode_discord_presence.main.subprocess.CREATE_NO_WINDOW", 0,
            raising=False,
        )
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "claude.exe")
        mock_run.return_value = MagicMock(
            stdout="some-wrapper-for-claude.exe    9999 Console                1     10,000 K",
            returncode=0,
        )
        assert is_claude_running() is False


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


def _tool(key="claude", client_id="123", sessions_dir=None):
    return Tool(key, key, client_id, f"{key}.exe", sessions_dir)


class TestReconcilePresence:
    def test_activates_and_shows_presence(self, monkeypatch):
        from claudecode_discord_presence import main as m
        tool = _tool()
        mock_rpc = MagicMock()
        monkeypatch.setattr(m, "connect_rpc", lambda cid: mock_rpc)
        rpc, cid, active = m._reconcile_presence(tool, False, None, None)
        assert rpc is mock_rpc
        assert cid == "123"
        assert active is True
        mock_rpc.update.assert_called_once()

    def test_activation_update_failure_drops_rpc(self):
        from claudecode_discord_presence import main as m
        tool = _tool()
        mock_rpc = MagicMock()
        mock_rpc.update.side_effect = BrokenPipeError()
        rpc, cid, active = m._reconcile_presence(tool, False, mock_rpc, "123")
        assert rpc is None
        assert cid is None
        assert active is False

    def test_switching_tool_reconnects_with_new_client_id(self, monkeypatch):
        from claudecode_discord_presence import main as m
        old_rpc = MagicMock()
        new_rpc = MagicMock()
        monkeypatch.setattr(m, "connect_rpc", lambda cid: new_rpc)
        rpc, cid, active = m._reconcile_presence(
            _tool(client_id="999"), True, old_rpc, "123"
        )
        assert rpc is new_rpc
        assert cid == "999"
        assert active is True
        old_rpc.clear.assert_called_once()
        old_rpc.close.assert_called_once()
        new_rpc.update.assert_called_once()

    def test_same_tool_does_not_reconnect(self, monkeypatch):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        monkeypatch.setattr(m, "connect_rpc", MagicMock())
        rpc, cid, active = m._reconcile_presence(
            _tool(client_id="123"), True, mock_rpc, "123"
        )
        assert rpc is mock_rpc
        assert cid == "123"
        assert active is True
        mock_rpc.update.assert_called_once()
        m.connect_rpc.assert_not_called()

    def test_idle_clears_presence(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        rpc, cid, active = m._reconcile_presence(None, True, mock_rpc, "123")
        assert rpc is mock_rpc
        assert cid == "123"
        assert active is False
        mock_rpc.clear.assert_called_once()

    def test_idle_clear_failure_drops_rpc(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        mock_rpc.clear.side_effect = OSError()
        rpc, cid, active = m._reconcile_presence(None, True, mock_rpc, "123")
        assert rpc is None
        assert cid is None
        assert active is False

    def test_continue_update_failure_drops_rpc(self):
        from claudecode_discord_presence import main as m
        mock_rpc = MagicMock()
        mock_rpc.update.side_effect = ConnectionResetError()
        rpc, cid, active = m._reconcile_presence(_tool(), True, mock_rpc, "123")
        assert rpc is None
        assert cid is None
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

        def _boom(_name):
            raise RuntimeError("boom")

        monkeypatch.setattr(m, "is_process_running", _boom)
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

        def _boom(_name):
            raise RuntimeError("boom")

        monkeypatch.setattr(m, "is_process_running", _boom)
        with pytest.raises(RuntimeError):
            m._run_daemon()
        # The traceback must be logged (stderr is DEVNULL for the detached daemon).
        mock_logger.exception.assert_called_once()
        fake_lock.release.assert_called_once()

    def test_exits_after_consecutive_absences(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        monkeypatch.setattr(m, "_stop_requested", lambda: False)
        monkeypatch.setattr(m, "_sleep_until_poll", lambda: False)
        monkeypatch.setattr(m, "_clear_own_stop_sentinel", lambda: None)
        monkeypatch.setattr(m, "resolve_tools", lambda: [_tool()])
        monkeypatch.setattr(m, "_reconcile_presence", lambda a, p, r, c: (r, c, p))
        monkeypatch.setattr(m, "EXIT_CONFIRM_COUNT", 2)
        # Absent on every poll: must exit after exactly 2 checks.
        gone = MagicMock(side_effect=[False, False])
        monkeypatch.setattr(m, "is_process_running", gone)
        m._run_daemon()
        assert gone.call_count == 2
        fake_lock.release.assert_called_once()

    def test_transient_absence_does_not_exit(self, monkeypatch):
        from claudecode_discord_presence import main as m
        fake_lock = MagicMock()
        fake_lock.acquire.return_value = True
        monkeypatch.setattr(m, "InstanceLock", lambda path: fake_lock)
        monkeypatch.setattr(m, "configure_logging", lambda: m.logger)
        monkeypatch.setattr(m, "_stop_requested", lambda: False)
        monkeypatch.setattr(m, "_sleep_until_poll", lambda: False)
        monkeypatch.setattr(m, "_clear_own_stop_sentinel", lambda: None)
        monkeypatch.setattr(m, "resolve_tools", lambda: [_tool()])
        monkeypatch.setattr(m, "_reconcile_presence", lambda a, p, r, c: (r, c, p))
        monkeypatch.setattr(m, "EXIT_CONFIRM_COUNT", 2)
        # False, then True (resets), then two consecutive False -> exits on the 4th check.
        seq = MagicMock(side_effect=[False, True, False, False])
        monkeypatch.setattr(m, "is_process_running", seq)
        m._run_daemon()
        assert seq.call_count == 4
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


class TestEnvInt:
    def test_unset_returns_default(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.delenv("CCDP_TESTVAL", raising=False)
        assert m._env_int("CCDP_TESTVAL", 42) == 42

    def test_valid_int(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setenv("CCDP_TESTVAL", "7")
        assert m._env_int("CCDP_TESTVAL", 42) == 7

    def test_non_integer_returns_default(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setenv("CCDP_TESTVAL", "not-a-number")
        assert m._env_int("CCDP_TESTVAL", 42) == 42

    def test_non_positive_returns_default(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setenv("CCDP_TESTVAL", "0")
        assert m._env_int("CCDP_TESTVAL", 42) == 42
        monkeypatch.setenv("CCDP_TESTVAL", "-3")
        assert m._env_int("CCDP_TESTVAL", 42) == 42


class TestClaudeProcessName:
    def test_default_by_platform(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.delenv("CCDP_CLAUDE_PROCESS_NAME", raising=False)
        expected = "claude.exe" if sys.platform == "win32" else "claude"
        assert m._claude_process_name() == expected

    def test_env_override(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setenv("CCDP_CLAUDE_PROCESS_NAME", "custom-proc")
        assert m._claude_process_name() == "custom-proc"


class TestResolveTools:
    def _clear(self, monkeypatch):
        for name in (
            "CCDP_CLAUDE_CLIENT_ID",
            "CCDP_CLAUDE_SESSIONS_DIR",
            "CCDP_CODEX_CLIENT_ID",
            "CCDP_CODEX_PROCESS_NAME",
            "CCDP_CODEX_SESSIONS_DIR",
            "CCDP_ZED_CLIENT_ID",
            "CCDP_ZED_PROCESS_NAME",
            "CCDP_ZED_SESSIONS_DIR",
        ):
            monkeypatch.delenv(name, raising=False)

    def test_default_is_all_three_tools(self, monkeypatch):
        self._clear(monkeypatch)
        tools = resolve_tools()
        assert [t.key for t in tools] == ["claude", "codex", "zed"]
        # All share the built-in client ID by default.
        assert tools[0].client_id == tools[1].client_id == tools[2].client_id
        assert tools[0].client_id
        assert tools[0].sessions_dir == get_claude_projects_dir()
        assert tools[1].sessions_dir == Path.home() / ".codex" / "sessions"
        assert tools[2].sessions_dir is None  # Zed is process-only by default

    def test_codex_label_and_client_id(self, monkeypatch):
        self._clear(monkeypatch)
        tools = resolve_tools()
        codex = tools[1]
        assert codex.label == "Codex"

    def test_codex_client_id_override(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("CCDP_CODEX_CLIENT_ID", "999")
        assert resolve_tools()[1].client_id == "999"

    def test_zed_client_id_override(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("CCDP_ZED_CLIENT_ID", "888")
        assert resolve_tools()[2].client_id == "888"

    def test_codex_process_name_override(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("CCDP_CODEX_PROCESS_NAME", "codex-cli.exe")
        assert resolve_tools()[1].process_name == "codex-cli.exe"

    def test_codex_sessions_dir_override(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("CCDP_CODEX_SESSIONS_DIR", "/tmp/codex-sessions")
        assert resolve_tools()[1].sessions_dir == Path("/tmp/codex-sessions")

    def test_empty_sessions_dir_means_process_only(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("CCDP_CODEX_SESSIONS_DIR", "")
        assert resolve_tools()[1].sessions_dir is None

    def test_claude_client_id_override(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("CCDP_CLAUDE_CLIENT_ID", "777")
        assert resolve_tools()[0].client_id == "777"


class TestResolveActiveTool:
    def test_empty_running_returns_none(self):
        assert resolve_active_tool([], 600) is None

    def test_process_only_tool_is_active(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m.time, "time", lambda: 100.0)
        zed = Tool("zed", "Zed", "888", "Zed.exe", None)
        assert resolve_active_tool([zed], 600) is zed

    def test_recent_session_tool_is_active(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m.time, "time", lambda: 100.0)
        claude = Tool("claude", "Claude", "123", "claude.exe", Path("/x"))
        monkeypatch.setattr(m, "find_latest_jsonl_mtime", lambda d: 95.0)
        assert resolve_active_tool([claude], 600) is claude

    def test_stale_session_tool_is_not_active(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m.time, "time", lambda: 1000.0)
        claude = Tool("claude", "Claude", "123", "claude.exe", Path("/x"))
        monkeypatch.setattr(m, "find_latest_jsonl_mtime", lambda d: 100.0)
        assert resolve_active_tool([claude], 600) is None

    def test_most_recent_wins(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m.time, "time", lambda: 100.0)
        claude = Tool("claude", "Claude", "123", "claude.exe", Path("/c"))
        codex = Tool("codex", "Codex", "999", "codex.exe", Path("/x"))
        monkeypatch.setattr(
            m, "find_latest_jsonl_mtime", lambda d: 90.0 if d == Path("/c") else 95.0
        )
        assert resolve_active_tool([claude, codex], 600) is codex

    def test_missing_session_files_skips_tool(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m.time, "time", lambda: 100.0)
        claude = Tool("claude", "Claude", "123", "claude.exe", Path("/c"))
        monkeypatch.setattr(m, "find_latest_jsonl_mtime", lambda d: None)
        assert resolve_active_tool([claude], 600) is None

    def test_recent_file_based_beats_process_only(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m.time, "time", lambda: 100.0)
        claude = Tool("claude", "Claude", "123", "claude.exe", Path("/c"))
        zed = Tool("zed", "Zed", "888", "Zed.exe", None)
        monkeypatch.setattr(m, "find_latest_jsonl_mtime", lambda d: 95.0)
        assert resolve_active_tool([zed, claude], 600) is claude

    def test_stale_file_based_falls_back_to_process_only(self, monkeypatch):
        from claudecode_discord_presence import main as m
        monkeypatch.setattr(m.time, "time", lambda: 1000.0)
        claude = Tool("claude", "Claude", "123", "claude.exe", Path("/c"))
        zed = Tool("zed", "Zed", "888", "Zed.exe", None)
        monkeypatch.setattr(m, "find_latest_jsonl_mtime", lambda d: 100.0)
        assert resolve_active_tool([claude, zed], 600) is zed
