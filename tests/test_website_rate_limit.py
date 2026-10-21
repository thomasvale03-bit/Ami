import os, threading
from unittest import mock
from googleapiclient.errors import HttpError
import gmail_client, posh_webhook as pw
from tests.test_website_form import payload


def _err(status=403):
    r = mock.MagicMock(); r.status = status; r.reason = "x"
    return HttpError(r, b'{"error":{"message":"Quota exceeded","errors":[{"reason":"rateLimitExceeded"}]}}')


def _intake(svc):
    i = mock.MagicMock(dry_run=False, intake_email="intake@example.com", _seen=set())
    i._lock = threading.Lock(); i.service.return_value = svc
    return i


def _svc(effects):
    s = mock.MagicMock(); s.users().messages().insert().execute.side_effect = effects; return s


def test_retries_then_succeeds():
    sleeps = []
    svc = _svc([_err(), _err(429), {"id": "x"}])
    with mock.patch.object(gmail_client, "send_once", return_value=True):
        assert pw.accept_website(_intake(svc), payload(source="playmaker", ref="pm-a"), _sleep=sleeps.append) == (200, "queued")
    assert sleeps == [1.0, 2.0]


def test_saves_when_still_limited_then_retry_delivers_once():
    svc = _svc([_err()] * 4)
    intake = _intake(svc)
    p = payload(source="playmaker", ref="pm-b", promoter="Thomas Vale")
    assert pw.accept_website(intake, p, _sleep=lambda d: None) == (200, "queued (pending)")
    assert len([f for f in os.listdir(pw.pending_dir()) if f.endswith(".json")]) == 1
    # resubmitting the same ref while pending does not duplicate
    assert pw.accept_website(intake, p, _sleep=lambda d: None) == (200, "queued (pending)")
    # still limited: stays pending
    svc.users().messages().insert().execute.side_effect = [_err()]
    assert pw.retry_pending_website(intake) == (0, 1)
    svc.users().messages().insert().execute.side_effect = None
    svc.users().messages().insert().execute.return_value = {"id": "ok"}
    with mock.patch.object(gmail_client, "send_once", return_value=True):
        assert pw.retry_pending_website(intake) == (1, 0)
        assert pw.retry_pending_website(intake) == (0, 0)
    assert not [f for f in os.listdir(pw.pending_dir()) if f.endswith(".json")]
    # a fresh process (empty _seen) still sees it as delivered by pm- ref
    assert pw.accept_website(_intake(svc), p) == (200, "duplicate")


def test_other_errors_still_raise():
    import pytest
    with pytest.raises(HttpError):
        pw.accept_website(_intake(_svc([_err(500)])), payload(ref="gg-c"), _sleep=lambda d: None)


def test_confirmations_cached_by_id():
    svc = mock.MagicMock()
    svc.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}]}
    svc.users().messages().get().execute.return_value = {"payload": {"headers": [{"name": "To", "value": "a@b.c"}]}, "internalDate": "0"}
    cache = {}
    with mock.patch.object(gmail_client, "get_plain_text_body", return_value=(None, "Hi Al,")) as g:
        gmail_client.recent_confirmations(svc, cache=cache)
        gmail_client.recent_confirmations(svc, cache=cache)
    assert g.call_count == 1 and "m1" in cache


def test_is_rate_limited():
    assert gmail_client.is_rate_limited(_err(403)) and gmail_client.is_rate_limited(_err(429))
    assert not gmail_client.is_rate_limited(_err(500)) and not gmail_client.is_rate_limited(ValueError())


def test_confirmation_cache_persists_across_restart(tmp_path):
    from datetime import date
    svc = mock.MagicMock()
    svc.users().messages().list().execute.return_value = {"messages": [{"id": "m1"}, {"id": "m2"}]}
    svc.users().messages().get().execute.side_effect = [
        {"payload": {"headers": [{"name": "To", "value": "a@b.c"}]}, "internalDate": "1792000000000"}, _err()]
    with mock.patch.object(gmail_client, "get_plain_text_body", return_value=(None, "Hi Al, Friday, October 23")):
        import pytest
        with pytest.raises(HttpError):
            gmail_client.recent_confirmations(svc)  # rate limited after m1; m1 still saved
    assert (tmp_path / "confirmation_cache.json").exists()
    gmail_client._CONFIRMATION_CACHE = None  # simulate restart
    svc.users().messages().get().execute.side_effect = None
    svc.users().messages().get().execute.return_value = {"payload": {"headers": []}, "internalDate": "0"}
    with mock.patch.object(gmail_client, "get_plain_text_body", return_value=(None, "")) as g:
        out = gmail_client.recent_confirmations(svc)
    assert g.call_count == 1  # only m2 fetched
    m1 = [o for o in out if o["id"] == "m1"][0]
    assert m1["to"] == "a@b.c" and isinstance(m1["last_night"], date)
