from unittest.mock import MagicMock, patch

from claudecode_discord_presence import hook as hook_mod


def test_popen_failure_writes_to_stderr(monkeypatch, capsys):
    fake_lock = MagicMock()
    fake_lock.acquire.return_value = True
    monkeypatch.setattr(hook_mod, "InstanceLock", lambda path: fake_lock)

    def _boom(*a, **k):
        raise OSError("python missing")

    monkeypatch.setattr(hook_mod.subprocess, "Popen", _boom)
    hook_mod.main()  # must not raise
    assert "failed to launch" in capsys.readouterr().err.lower()


def test_skips_launch_when_lock_held(monkeypatch):
    fake_lock = MagicMock()
    fake_lock.acquire.return_value = False
    monkeypatch.setattr(hook_mod, "InstanceLock", lambda path: fake_lock)

    with patch.object(hook_mod.subprocess, "Popen") as popen:
        hook_mod.main()

    popen.assert_not_called()
    fake_lock.release.assert_not_called()


def test_launches_when_lock_free(monkeypatch):
    fake_lock = MagicMock()
    fake_lock.acquire.return_value = True
    monkeypatch.setattr(hook_mod, "InstanceLock", lambda path: fake_lock)

    with patch.object(hook_mod.subprocess, "Popen") as popen:
        hook_mod.main()

    fake_lock.acquire.assert_called_once()
    fake_lock.release.assert_called_once()
    popen.assert_called_once()
