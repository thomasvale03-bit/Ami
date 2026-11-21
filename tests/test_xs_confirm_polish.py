"""XS preview fixes (Thomas, Nov 21 2026): no UV- refs in the guest subject,
TAO pass steps labelled on mixed trips, urvenue start time on the night card."""
import worker_emails as we

REQ = {"first_name": "Mock", "last_name": "Guest", "email": "guest@example.invalid", "start_date": "2026-11-22",
       "end_date": "2026-11-22", "female_count": 2, "male_count": 0}
XS = {"venue": "XS Nightclub", "date": "2026-11-22", "order_id": "UV-34CE9F75", "system": "urvenue",
      "event": "Headliner", "event_time": "9:00pm", "list_item": "General List",
      "party": {"female_count": 2, "male_count": 0}}
MDC = {"venue": "Marquee Dayclub", "date": "2026-11-22", "order_id": "3c249411-8c37-4da7-97e4-b9a84def1754",
       "event": "Poolside Sundays", "event_time": "11:00 AM", "party": {"female_count": 2, "male_count": 0}}


def test_subject_drops_uv_refs_keeps_tao_refs():
    s = we.guest_email(REQ, [MDC, XS], update=True)["subject"]
    assert "UV-" not in s and "Order 3c249411" in s and "XS Nightclub Nov 22" in s
    only = we.guest_email(REQ, [XS], update=True)["subject"]
    assert "UV-" not in only and only.startswith("Updated: ") and "Order" not in only
    tao = we.guest_email(REQ, [MDC], update=True)["subject"]
    assert tao.endswith("(Order 3c249411)")  # unchanged for TAO-only
    assert "Order" not in we.guest_email(REQ, [MDC, XS])["subject"]  # first send: no tail


def test_update_subjects_stay_unique_per_urvenue_night():
    other = {**XS, "date": "2026-11-23", "order_id": "UV-AAAAAAAA"}
    a = we.guest_email(REQ, [MDC, XS], update=True)["subject"]
    b = we.guest_email(REQ, [MDC, XS, other], update=True)["subject"]
    assert a != b


def test_mixed_trip_labels_tao_steps():
    html = we.guest_email(REQ, [MDC, XS])["html"]
    passes = html[html.index("<!-- How to get passes -->"):html.index("<!-- Before you go -->")]
    assert "For Marquee Dayclub" in passes
    assert passes.index("For Marquee Dayclub") < passes.index("Wynn Resorts app reservation")
    two = {**MDC, "venue": "OMNIA Nightclub", "date": "2026-11-21"}
    assert "For OMNIA Nightclub and Marquee Dayclub" in we.guest_email(REQ, [two, MDC, XS])["html"]


def test_tao_only_and_urvenue_only_have_no_label():
    assert "For Marquee Dayclub" not in we.guest_email(REQ, [MDC])["html"]
    html = we.guest_email(REQ, [XS])["html"]
    assert "How to get your passes" not in html and ">For " not in html


def test_urvenue_start_time_on_card_and_text():
    m = we.guest_email(REQ, [MDC, XS])
    assert "Headliner \u00b7 9:00pm" in m["html"]
    assert "(Headliner, 9:00pm — on the guest list)" in m["text"]
    assert "Poolside Sundays \u00b7" not in m["html"]  # TAO card unchanged
    no_time = we.guest_email(REQ, [{**XS, "event_time": None}])
    assert "Headliner \u00b7" not in no_time["html"] and "(Headliner — on the guest list)" in no_time["text"]
