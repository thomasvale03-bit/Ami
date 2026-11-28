"""Scottsdale door lists: read EVERY Gmail page (Riot House gets ~200 webhook emails / 14 days)."""
from datetime import date, datetime
from unittest import mock

import gmail_client
import main

SAT = datetime(2026, 11, 14, 19, 43, tzinfo=main.PHOENIX)
CUTOFF_MS = int(SAT.timestamp() * 1000)


def body(name, order, event="Riot House | Guest List", when="2026-11-15T05:00:00.000Z", ticket="Female Admission"):
    return (f"NEW POSH CLIENT\nCLIENT INFORMATION\nName: {name}\nEmail: {name.lower().replace(' ', '.')}@example.invalid\n"
            f"EVENT INFORMATION\nEvent: {event}\nEvent Date: {when}\nTicket: {ticket}\n"
            f"ORDER INFORMATION\nOrder Number: {order}\nSOURCE: POSH WEBHOOK\n")


class FakeGmail:
    """Minimal Gmail API: list() honours maxResults/pageToken; get() returns internalDate."""

    def __init__(self, msgs, page_cap=100):
        self.msgs, self.page_cap, self.list_calls = msgs, page_cap, []  # msgs: venue -> [(id, subject, body, ms)]

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, userId, q, maxResults=100, pageToken=None):
        self.list_calls.append((q, maxResults, pageToken))
        venue = "Riot House" if "Riot House" in q else "Almost Famous"
        items = self.msgs.get(venue, [])
        start = int(pageToken or 0)
        size = min(maxResults, self.page_cap)
        page = items[start:start + size]
        resp = {"messages": [{"id": m[0]} for m in page]}
        if start + size < len(items):
            resp["nextPageToken"] = str(start + size)
        return mock.Mock(execute=mock.Mock(return_value=resp))


def run(svc, fn):
    by_id = {m[0]: m for ms in svc.msgs.values() for m in ms}
    with mock.patch.object(gmail_client, "get_plain_text_body",
                           side_effect=lambda s, mid: ({"internalDate": str(by_id[mid][3]), "id": mid}, by_id[mid][2])), \
         mock.patch.object(gmail_client, "get_subject", side_effect=lambda msg: by_id[msg["id"]][1]):
        return fn()


def riot_mail(n_orders, dup_every=2, late=0):
    """Newest first. n_orders unique orders; every `dup_every`th order also has a 2nd (SENT) copy."""
    out = []
    for i in range(n_orders):
        o = 40900000 + i
        subj = f"NEW POSH SIGNUP (webhook) #{o} Riot House"
        ms = CUTOFF_MS - (i + 1) * 60_000
        out.append((f"r{i}", subj, body(f"Guest {i}", o), ms))
        if i % dup_every == 0:
            out.append((f"r{i}-sent", subj, body(f"Guest {i}", o), ms))
    for i in range(late):  # arrived after the 7:43 PM send
        o = 40990000 + i
        out.insert(0, (f"late{i}", f"NEW POSH SIGNUP (webhook) #{o} Riot House", body(f"Late {i}", o), CUTOFF_MS + 60_000))
    return out


def test_all_pages_read_and_deduped_by_order():
    svc = FakeGmail({"Riot House": riot_mail(150)})  # 225 emails > one 100-result page
    stats = {}
    orders = run(svc, lambda: main.collect_scottsdale_door_orders(svc, "Riot House", date(2026, 11, 14), cutoff=SAT, stats=stats))
    assert stats["emails_read"] == 225
    assert stats["pages"] == 3
    assert len(orders) == 150 and len({o["order_number"] for o in orders}) == 150
    assert "40900149" in {o["order_number"] for o in orders}  # oldest order, on the last page
    assert [c[2] for c in svc.list_calls] == [None, "100", "200"]


def test_signups_after_send_are_never_added():
    svc = FakeGmail({"Riot House": riot_mail(3, late=2)})
    stats = {}
    orders = run(svc, lambda: main.collect_scottsdale_door_orders(svc, "Riot House", date(2026, 11, 14), cutoff=SAT, stats=stats))
    assert len(orders) == 3 and stats["after_cutoff"] == 2
    assert not any(o["name"].startswith("Late") for o in orders)


def test_safety_cap_stops_and_logs(caplog):
    svc = FakeGmail({"Riot House": riot_mail(30, dup_every=1000)})
    stats = {}
    with mock.patch.object(main, "DOOR_LIST_MAX_MESSAGES", 10):
        orders = run(svc, lambda: main.collect_scottsdale_door_orders(svc, "Riot House", date(2026, 11, 14), cutoff=SAT, stats=stats))
    assert stats["emails_read"] == 10 and stats["cap_hit"] is True and len(orders) == 9  # r0 + its SENT copy + r1..r8
    assert "safety cap" in caplog.text


def test_live_send_uses_every_page_and_never_resends():
    svc = FakeGmail({"Riot House": riot_mail(201)})
    sent = []
    send = mock.patch.object(main, "_send_branded_or_gmail",
                             side_effect=lambda s, mid, to, subj, txt, html, **kw: sent.append((to, subj, txt)) or True)
    with mock.patch.object(gmail_client, "already_sent", return_value=False), send:
        assert run(svc, lambda: main.send_scottsdale_door_lists(svc, "live", now=SAT)) == 2
    riot = sent[0][2]
    assert riot.startswith("Riot House – Sat 11/14\n")
    assert "Total: 201 parties – 201 girls / 0 guys" in riot
    # All unique orders from every Gmail page are in the body, deduped (no order #s).
    assert riot.count("Guest 200") == 1 and riot.count("Guest 0") == 1
    assert "#40900200" not in riot and "#40900000" not in riot
    # Already sent tonight: no second send, no search, even with new signups.
    svc2 = FakeGmail({"Riot House": riot_mail(205)})
    sent.clear()
    with mock.patch.object(gmail_client, "already_sent", return_value=True), send:
        assert run(svc2, lambda: main.send_scottsdale_door_lists(svc2, "live", now=SAT)) == 0
    assert sent == [] and svc2.list_calls == []


def test_preview_sends_no_email_and_counts():
    af = [(f"a{i}", f"NEW POSH SIGNUP (webhook) #{41000000 + i} Almost Famous",
           body(f"Af {i}", 41000000 + i, event="Almost Famous", ticket="Male Admission"), CUTOFF_MS - 1000 * (i + 1))
          for i in range(5)]
    svc = FakeGmail({"Riot House": riot_mail(4), "Almost Famous": af})
    with mock.patch.object(main, "_send_branded_or_gmail", side_effect=AssertionError("must not send")), \
         mock.patch.object(gmail_client, "send_once", side_effect=AssertionError("must not send")):
        out = run(svc, lambda: main.scottsdale_door_list_preview(svc, date(2026, 11, 14)))
    rh, a = out["venues"]["Riot House"], out["venues"]["Almost Famous"]
    assert rh["unique_orders"] == 4 and rh["emails_read"] == 6 and rh["women"] == 4
    assert a["unique_orders"] == 5 and a["men"] == 5 and a["women"] == 0


def test_preview_route_is_authenticated(monkeypatch, tmp_path):
    import queue_service as q
    monkeypatch.setenv('AMY_QUEUE_ENABLED', 'true')
    monkeypatch.setenv('AMY_QUEUE_API_TOKEN', 't' * 32)
    monkeypatch.setenv('AMY_QUEUE_DIR', str(tmp_path / 'queue'))
    path = '/queue/door-list-preview?night=2026-11-14'
    assert q.handle('GET', path, {}, None)[0] == 401
    with mock.patch.object(gmail_client, "get_service", return_value="svc"), \
         mock.patch.object(main, "scottsdale_door_list_preview", return_value={"ok": 1}) as prev:
        code, body_ = q.handle('GET', path, {'Authorization': 'Bearer ' + 't' * 32}, None)
    assert code == 200 and body_ == {"ok": 1}
    assert prev.call_args[0][1] == date(2026, 11, 14)
