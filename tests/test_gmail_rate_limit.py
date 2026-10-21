import pytest
from unittest.mock import MagicMock
from googleapiclient.errors import HttpError
import gmail_client

def _err(status):
    r = MagicMock(); r.status = status; r.reason = "x"
    return HttpError(r, b'{"error":{"message":"Quota exceeded","errors":[{"reason":"rateLimitExceeded"}]}}')

def _svc(effects):
    s = MagicMock(); s.users().getProfile().execute.side_effect = effects; return s

def test_retries_then_succeeds():
    sleeps = []
    s = _svc([_err(403), _err(429), {"emailAddress": "a@b.c"}])
    assert gmail_client.get_account_email(s, _sleep=sleeps.append) == "a@b.c"
    assert len(sleeps) == 2

def test_gives_up_after_attempts():
    with pytest.raises(HttpError):
        gmail_client.get_account_email(_svc([_err(403)] * 3), attempts=3, _sleep=lambda d: None)

def test_non_rate_limit_raises_immediately():
    sleeps = []
    with pytest.raises(HttpError):
        gmail_client.get_account_email(_svc([_err(401)]), _sleep=sleeps.append)
    assert sleeps == []
