import os
from pathlib import Path

from claudecode_discord_presence.single_instance import InstanceLock


def test_acquire_returns_true_when_uncontended(tmp_path):
    lock = InstanceLock(tmp_path / "x.pid")
    assert lock.acquire() is True
    lock.release()


def test_second_lock_on_same_file_fails(tmp_path):
    path = tmp_path / "x.pid"
    first = InstanceLock(path)
    assert first.acquire() is True
    second = InstanceLock(path)
    assert second.acquire() is False
    first.release()


def test_release_allows_reacquire(tmp_path):
    path = tmp_path / "x.pid"
    first = InstanceLock(path)
    assert first.acquire() is True
    first.release()
    second = InstanceLock(path)
    assert second.acquire() is True
    second.release()


def test_acquire_writes_pid(tmp_path):
    path = tmp_path / "x.pid"
    lock = InstanceLock(path)
    assert lock.acquire() is True
    assert path.read_text().strip() == str(os.getpid())
    lock.release()


def test_release_removes_pid_file(tmp_path):
    path = tmp_path / "x.pid"
    lock = InstanceLock(path)
    lock.acquire()
    lock.release()
    assert not path.exists()


def test_acquire_creates_parent_dirs(tmp_path):
    path = tmp_path / "sub" / "deep" / "x.pid"
    lock = InstanceLock(path)
    assert lock.acquire() is True
    assert path.exists()
    lock.release()


def test_release_without_acquire_is_safe(tmp_path):
    lock = InstanceLock(tmp_path / "x.pid")
    lock.release()  # must not raise


def test_stop_file_default_path():
    from claudecode_discord_presence.single_instance import STOP_FILE
    assert STOP_FILE.name == "claudecode-discord-presence.stop"
    assert STOP_FILE.parent.name == ".claude"
