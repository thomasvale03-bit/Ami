import pytest


@pytest.fixture(autouse=True)
def _pending_dir(tmp_path, monkeypatch):
    """Keep website-form pending/delivered files out of the repo."""
    monkeypatch.setenv("AMY_PENDING_DIR", str(tmp_path / "pending"))
