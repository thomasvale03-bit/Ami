"""Thomas (Nov 21 2026): guest emails never promise skip-the-line / line
priority. The VIP table add-on says "Lock in your own table" instead. Covers
every rendered confirmation (TAO, LIV, XS/Wynn, mixed, Drai's, legacy text +
HTML, Scottsdale, Riot House, hold emails for PME and GuestlistGo) plus a scan
of every email template file on disk."""
import pathlib
import re

import pytest

import worker_emails as we
from templates import emails, html_emails

BAD = re.compile(r"skip(?:ping)?[\s-]+(?:the[\s-]+)?(?:line|queue)|line[\s-]+priority|priority[\s-]+(?:entry|line|access)"
                 r"|front[\s-]+of[\s-]+the[\s-]+line|jump[\s-]+the[\s-]+line|bypass[\s-]+the[\s-]+line", re.I)
ROOT = pathlib.Path(__file__).resolve().parents[1]


def clean(*bodies):
    for body in bodies:
        hit = BAD.search(body or "")
        assert not hit, f"line-priority wording found: {hit.group(0)!r}"


def req(f=3, m=3, start="2026-11-27", end="2026-11-28"):
    return {"first_name": "Mock", "last_name": "Guest", "email": "guest@example.invalid",
            "start_date": start, "end_date": end, "female_count": f, "male_count": m}


TAO = {"venue": "OMNIA Nightclub", "date": "2026-11-27", "order_id": "3c249411-8c37-4da7-97e4-b9a84def1754",
       "event": "Headliner", "event_time": "10:30 PM"}
MDC = {"venue": "Marquee Dayclub", "date": "2026-11-28", "order_id": "ffbcddfa-0000-4000-8000-000000000000",
       "event": "Race Week", "event_time": "11:00 AM"}
LIV = {"venue": "LIV Nightclub", "date": "2026-11-27", "order_id": None, "system": "urvenue",
       "event": "Dom Dolla", "event_time": "10:30pm", "list_item": "General List"}
LIVB = {**LIV, "venue": "LIV Beach", "date": "2026-11-28", "event_time": "11:00am"}
XS = {"venue": "XS Nightclub", "date": "2026-11-28", "order_id": "UV-34CE9F75", "system": "urvenue",
      "event": "Headliner", "event_time": "9:00pm", "list_item": "General List"}


def test_vip_partial_has_no_skip_the_line_and_keeps_the_table_offer():
    vip = (ROOT / "templates/email/partials/vip_block.html").read_text()
    clean(vip)
    assert "Lock in your own table with bottle service" in vip


@pytest.mark.parametrize("bookings", [[TAO], [MDC], [LIV], [LIVB], [XS], [TAO, MDC, XS], [LIV, LIVB, MDC]])
@pytest.mark.parametrize("party", [(1, 0), (3, 3), (1, 4)])
@pytest.mark.parametrize("update", [False, True])
def test_worker_guest_confirmations(bookings, party, update):
    p = we.guest_email(req(*party), bookings, update=update, pending=[("Hakkasan Nightclub", "2026-12-31")])
    clean(p["subject"], p["text"], p["html"])
    if sum(party) >= 4:
        assert "Lock in your own table" in p["html"]


def test_drais_confirmation():
    p = we.drais_guest_email(req(), [{"venue": we.DRAIS_VENUE, "date": "2026-11-27"}])
    clean(p["text"], p["html"])


@pytest.mark.parametrize("raw", [{"source": "playmaker"}, {"source": "guestlistgo"}])
def test_hold_emails_both_brands(raw):
    held = [("Hakkasan Nightclub", "2026-12-31", "unposted"), ("LIV Nightclub", "2026-12-31", "closed")]
    p = we.hold_email("r1", {**raw, "email": "guest@example.invalid"}, req(start="2026-12-31", end="2026-12-31"), held)
    clean(p["text"], p["html"])


def test_legacy_and_other_brand_confirmations():
    nights = [{"date": "2026-11-27", "venue": "JEWEL Nightclub"}, {"date": "2026-11-28", "venue": "Marquee Nightclub"}]
    regs = [{"venue": "TAO Nightclub", "date": "2026-11-27", "event": "", "female_count": 3, "male_count": 3,
             "confirmation_id": "X1"}]
    clean(*emails.guest_confirmation("Amy", nights),
          *emails.consolidated_confirmation("Amy", "a@b.c", "2026-11-27", regs),
          html_emails.concierge_confirmation_html("Amy", nights, 6),
          html_emails.consolidated_confirmation_html("Amy", regs),
          html_emails.render_confirmation("Amy", [("Fri, Nov 27", "OMNIA Nightclub", None)], 8))


def test_every_email_template_file_on_disk():
    files = [p for p in (ROOT / "templates").rglob("*") if p.suffix in {".html", ".txt", ".py", ".md"}]
    files += [ROOT / "worker_emails.py"]
    assert files
    for path in files:
        clean(path.read_text(errors="ignore"))


def test_guards_reject_line_priority_wording():
    for phrase in ("Skip the line tonight", "skip-the-line entry", "priority entry", "front of the line"):
        assert we.LINE_PRIORITY_RE.search(phrase)
    assert not we.LINE_PRIORITY_RE.search("Lock in your own table with bottle service")
