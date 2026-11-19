import pytest


@pytest.fixture(autouse=True)
def _pending_dir(tmp_path, monkeypatch):
    """Keep website-form pending/delivered files out of the repo."""
    monkeypatch.setenv("AMY_PENDING_DIR", str(tmp_path / "pending"))


@pytest.fixture(autouse=True)
def _cache_dir(tmp_path, monkeypatch):
    import gmail_client
    monkeypatch.setenv("AMY_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(gmail_client, "_CONFIRMATION_CACHE", None)


@pytest.fixture(autouse=True)
def _no_flag_alert_threads(monkeypatch):
    """Queue flag alerts would send via Gmail/Zoho in a thread; tests that
    need them call queue_service.send_flag_alert / queue_flags directly."""
    import queue_service
    sent = []
    monkeypatch.setattr(queue_service, "_dispatch_alert", lambda s, ref, now: sent.append(ref))
    return sent
