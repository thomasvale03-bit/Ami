"""Thomas (Nov 21 2026): every guest confirmation states that guest list
spots are subject to capacity. Covers automatic worker emails (TAO, LIV,
XS/Wynn, mixed, Drai's, Updated, partial with nights still to come,
hold notices that confirm already-booked nights, GuestlistGo-branded
holds) plus legacy text/HTML, Scottsdale, Riot House, and Almost Famous.
"""
import pathlib

import pytest

import worker_emails as we
from templates import emails, html_emails
from templates.emails import CAPACITY_LINE

ROOT = pathlib.Path(__file__).resolve().parents[1]
assert CAPACITY_LINE == "All guest list spots are subject to capacity."


def has_line(*bodies):
    for body in bodies:
        assert CAPACITY_LINE in (body or ""), f"missing {CAPACITY_LINE!r} in: {(body or '')[:240]!r}"


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


@pytest.mark.parametrize("bookings", [[TAO], [MDC], [LIV], [LIVB], [XS], [TAO, MDC, XS], [LIV, LIVB, MDC]])
@pytest.mark.parametrize("update", [False, True])
def test_worker_guest_confirmations(bookings, update):
    p = we.guest_email(req(), bookings, update=update, pending=[("Hakkasan Nightclub", "2026-12-31")])
    has_line(p["text"], p["html"])
    # One clear line, not a skip-the-line promise
    assert "skip" not in p["text"].lower() and "skip" not in p["html"].lower()


def test_drais_confirmation():
    p = we.drais_guest_email(req(), [{"venue": we.DRAIS_VENUE, "date": "2026-11-27"}])
    has_line(p["text"], p["html"])


def test_hold_email_includes_line_when_nights_already_booked():
    held = [("Hakkasan Nightclub", "2026-12-31", "unposted")]
    booked = we.hold_email("r1", {"source": "playmaker", "email": "guest@example.invalid"},
                           req(start="2026-12-31", end="2026-12-31"), held, booked=True)
    has_line(booked["text"], booked["html"])
    gg = we.hold_email("r2", {"source": "guestlistgo", "email": "guest@example.invalid"},
                       req(start="2026-12-31", end="2026-12-31"), held, booked=True)
    has_line(gg["text"], gg["html"])
    assert "playmaker" not in gg["text"].lower() and "playmaker" not in gg["html"].lower()


def test_hold_email_without_booked_nights_is_not_a_confirmation():
    held = [("Hakkasan Nightclub", "2026-12-31", "unposted")]
    p = we.hold_email("r1", {"source": "playmaker", "email": "guest@example.invalid"},
                      req(start="2026-12-31", end="2026-12-31"), held, booked=False)
    assert CAPACITY_LINE not in p["text"]
    assert CAPACITY_LINE not in p["html"]


def test_legacy_and_scottsdale_confirmations():
    nights = [{"date": "2026-11-27", "venue": "JEWEL Nightclub"}, {"date": "2026-11-28", "venue": "Marquee Nightclub"}]
    regs = [{"venue": "TAO Nightclub", "date": "2026-11-27", "event": "", "female_count": 3, "male_count": 3,
             "confirmation_id": "X1"}]
    has_line(emails.guest_confirmation("Amy", nights)[1],
             emails.consolidated_confirmation("Amy", "a@b.c", "2026-11-27", regs)[1],
             emails.guest_signup_links_email("Amy", [{"date": "2026-11-27", "venue": "OMNIA Nightclub",
                                                      "url": "https://example.invalid/list"}],
                                    "https://example.invalid/promoter")[1],
             html_emails.concierge_confirmation_html("Amy", nights, 6),
             html_emails.consolidated_confirmation_html("Amy", regs),
             html_emails.render_confirmation("Amy", [("Fri, Nov 27", "OMNIA Nightclub", None)], 8),
             html_emails.render_almost_famous("Amy", "Saturday, November 28"),
             html_emails.render_almost_famous_text("Amy", "Saturday, November 28"),
             html_emails.render_riot_house("Amy", ["Fri, Nov 27"]),
             html_emails.render_riot_house_text("Amy", ["Fri, Nov 27"]),
             html_emails.render_scottsdale_venue("Amy", "El Hefe", ["Fri, Nov 27"]),
             html_emails.render_scottsdale_venue_text("Amy", "El Hefe", ["Fri, Nov 27"]))
    _subj, text, html = html_emails.scottsdale_confirmation_email("Amy", "Almost Famous", ["2026-11-28"])
    has_line(text, html)
    _subj, text, html = html_emails.scottsdale_confirmation_email("Amy", "Riot House", ["2026-11-27"])
    has_line(text, html)


def test_capacity_helper_matches_shared_wording():
    assert CAPACITY_LINE in html_emails.capacity_note_html()
    assert "skip" not in html_emails.capacity_note_html().lower()


def test_html_templates_on_disk_include_the_line():
    for name in ("guest_confirmation.html", "almost_famous.html"):
        body = (ROOT / "templates/email" / name).read_text()
        assert CAPACITY_LINE in body
