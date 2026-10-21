import base64, email
from unittest import mock
import posh_webhook as pw
from tests.test_branded_send import FakeGmail, PAYLOAD


def _from(g):
    return email.message_from_bytes(base64.urlsafe_b64decode(g.sent[-1]["raw"]))["From"]


def setup_function():
    pw._branded_times.clear(); pw._zoho_sent.clear()


def test_guestlistgo_from_goes_via_gmail_even_with_zoho():
    g = FakeGmail()
    with mock.patch.object(pw, "zoho_configured", return_value=True), mock.patch.object(pw, "_zoho_send") as z:
        code, res = pw.send_branded(g, dict(PAYLOAD, subject="gg1", **{"from": "Confirmations@GuestlistGo.me"}))
    assert code == 200 and res["via"] == "gmail" and res["from"] == "confirmations@guestlistgo.me"
    z.assert_not_called()
    assert _from(g) == "GuestlistGo <confirmations@guestlistgo.me>"


def test_other_from_rejected():
    g = FakeGmail()
    assert pw.send_branded(g, dict(PAYLOAD, subject="gg2", **{"from": "info@evil.com"})) == (400, "from not allowed")
    assert g.sent == []


def test_playmaker_unchanged():
    g = FakeGmail()
    with mock.patch.object(pw, "zoho_configured", return_value=False):
        code, res = pw.send_branded(g, dict(PAYLOAD, subject="pm1"))
    assert code == 200 and _from(g).startswith("Playmaker Entertainment <")
