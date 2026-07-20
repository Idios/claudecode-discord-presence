from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def test_ci_workflow_exists():
    assert CI.is_file()


def test_ci_covers_three_os_and_runs_pytest():
    text = CI.read_text(encoding="utf-8")
    for os_name in ("windows-latest", "ubuntu-latest", "macos-latest"):
        assert os_name in text, f"CI matrix missing {os_name}"
    assert "pytest" in text
    assert "3.10" in text
