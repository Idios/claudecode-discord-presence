import importlib.metadata

from claudecode_discord_presence import __version__


def test_metadata_version_matches_dunder():
    """Installed package metadata must match __init__.__version__ (single source)."""
    assert importlib.metadata.version("claudecode-discord-presence") == __version__
