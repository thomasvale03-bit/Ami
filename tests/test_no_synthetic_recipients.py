"""Nov 21 2026 bounce: pm-web-hannahbob-xs-1122-2026-11-22@playmakerentertainment.com
was BCC'd because send_once passed its Message-ID positionally into
build_message's (newly inserted) bcc slot. Synthetic ids are never recipients."""
import base64
import email
from datetime import date

import pytest

import gmail_client


def parse(raw):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw["raw"]))


class FakeService:
    def __init__(self):
        self.sent = []

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kw):
        return self

    def send(self, userId, body):
        self.sent.append(body)
        return self

    def execute(self):
        return {"messages": []}


MID = "pm-web-hannahbob-xs-1122-2026-11-22@playmakerentertainment.com"


def test_send_once_message_id_is_a_header_not_a_recipient(monkeypatch):
    monkeypatch.setattr(gmail_client, "already_sent", lambda service, q: False)
    svc = FakeService()
    assert gmail_client.send_once(svc, MID, "team@playmakerentertainment.com", "S", "b",
                                  sender="info@playmakerentertainment.com")
    msg = parse(svc.sent[0])
    assert msg["Message-ID"] == f"<{MID}>"
    assert msg["Bcc"] is None and msg["Cc"] is None
    assert msg["To"] == "team@playmakerentertainment.com"
    assert MID not in str(msg["To"])


def test_send_once_keeps_real_cc(monkeypatch):
    monkeypatch.setattr(gmail_client, "already_sent", lambda service, q: False)
    svc = FakeService()
    gmail_client.send_once(svc, MID, "guest@example.com", "S", "b", sender="info@x.com",
                           cc="team@playmakerentertainment.com")
    msg = parse(svc.sent[0])
    assert msg["To"] == "guest@example.com" and msg["Cc"] == "team@playmakerentertainment.com"


def test_build_message_strips_synthetic_ids_but_not_real_addresses():
    raw = gmail_client.build_message("hannah@example.com", "S", "b", "info@x.com",
                                     cc=f"{MID}, team@playmakerentertainment.com",
                                     bcc="gg-web-abc-2026-11-22@guestlistgo.me", message_id=MID)
    msg = parse(raw)
    assert msg["To"] == "hannah@example.com"
    assert msg["Cc"] == "team@playmakerentertainment.com" and msg["Bcc"] is None
    # a real guest whose address merely contains "web" is untouched
    msg = parse(gmail_client.build_message("pm-webster@example.com", "S", "b", "info@x.com",
                                           bcc="web-pm@example.com"))
    assert msg["To"] == "pm-webster@example.com" and msg["Bcc"] == "web-pm@example.com"


def test_build_message_refuses_synthetic_to_and_positional_bcc():
    with pytest.raises(ValueError):
        gmail_client.build_message(MID, "S", "b", "info@x.com")
    with pytest.raises(TypeError):
        gmail_client.build_message("a@x.com", "S", "b", "info@x.com", None, MID)


def test_website_team_notice_has_no_synthetic_recipient(monkeypatch):
    import posh_webhook
    monkeypatch.setattr(gmail_client, "already_sent", lambda service, q: False)
    monkeypatch.setattr(posh_webhook, "to_website_text", lambda clean: "body")
    svc = FakeService()
    clean = {"ref": "hannahbob-xs-1122", "email": "hannah@example.com", "night": date(2026, 11, 22),
             "source": "playmaker"}
    assert posh_webhook.notify_team_website(svc, clean, "New request")
    msg = parse(svc.sent[0])
    rcpts = " ".join(str(msg[h] or "") for h in ("To", "Cc", "Bcc"))
    assert "pm-web-" not in rcpts and msg["To"] == "team@playmakerentertainment.com"
    assert msg["Message-ID"] == "<pm-web-hannahbob-xs-1122-2026-11-22@playmakerentertainment.com>"
