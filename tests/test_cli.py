import os

from claudecode_discord_presence import main as m
from claudecode_discord_presence.single_instance import InstanceLock


class TestParseArgs:
    def test_no_args(self):
        a = m._parse_args([])
        assert a.status is False and a.stop is False

    def test_status(self):
        assert m._parse_args(["--status"]).status is True

    def test_stop(self):
        assert m._parse_args(["--stop"]).stop is True


class TestCmdStatus:
    def test_not_running(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(m, "PID_FILE", tmp_path / "p.pid")
        monkeypatch.setattr(m, "LOG_FILE", tmp_path / "l.log")
        m._cmd_status()
        assert "not running" in capsys.readouterr().out

    def test_running(self, tmp_path, monkeypatch, capsys):
        pid_file = tmp_path / "p.pid"
        monkeypatch.setattr(m, "PID_FILE", pid_file)
        monkeypatch.setattr(m, "LOG_FILE", tmp_path / "l.log")
        held = InstanceLock(pid_file)
        assert held.acquire()
        try:
            m._cmd_status()
        finally:
            held.release()
        assert "running (pid" in capsys.readouterr().out


class TestCmdStop:
    def test_not_running_writes_no_sentinel(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(m, "PID_FILE", tmp_path / "p.pid")
        stop = tmp_path / "s.stop"
        monkeypatch.setattr(m, "STOP_FILE", stop)
        m._cmd_stop()
        assert "not running" in capsys.readouterr().out
        assert not stop.exists()

    def test_running_writes_pid_sentinel(self, tmp_path, monkeypatch, capsys):
        pid_file = tmp_path / "p.pid"
        stop = tmp_path / "s.stop"
        monkeypatch.setattr(m, "PID_FILE", pid_file)
        monkeypatch.setattr(m, "STOP_FILE", stop)
        held = InstanceLock(pid_file)
        assert held.acquire()
        try:
            m._cmd_stop()
        finally:
            held.release()
        assert stop.exists()
        assert stop.read_text().strip() == str(os.getpid())
