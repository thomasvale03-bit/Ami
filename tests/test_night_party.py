"""Per-night party size for a multi-night request (optional override)."""
import pytest
import request_plan
import worker_emails


def test_parse_party_by_night_line():
    assert request_plan.parse_night_parties(
        "Party by night: 2026-11-21 1F 0M; 2026-11-22 2F 0M; 2026-11-23 2F"
    ) == {"2026-11-21": (1, 0), "2026-11-22": (2, 0), "2026-11-23": (2, 0)}


def test_party_for_night_uses_note_then_default():
    raw = {
        "female_count": 2,
        "male_count": 0,
        "comments": "No phone given. Saturday LAVO is 1F only. "
                    "Party by night: 2026-11-21 1F 0M; 2026-11-22 2F 0M; 2026-11-23 2F 0M",
    }
    assert request_plan.party_for_night(raw, "2026-11-21") == (1, 0)
    assert request_plan.party_for_night(raw, "2026-11-22") == (2, 0)
    assert request_plan.party_for_night(raw, "2026-11-23") == (2, 0)
    assert request_plan.party_for_night({"female_count": 2, "male_count": 1}, "2026-11-21") == (2, 1)


def test_itinerary_entry_women_men():
    raw = {
        "female_count": 2,
        "male_count": 0,
        "itinerary": [
            {"date": "2026-11-21", "venues": ["LAVO Party Brunch"], "women": 1, "men": 0},
            {"date": "2026-11-22", "venues": ["TAO Nightclub"], "women": 2, "men": 0},
        ],
    }
    assert request_plan.night_parties(raw) == {"2026-11-21": (1, 0), "2026-11-22": (2, 0)}


def test_team_email_night_lines_include_party_only_when_planned():
    request = {"first_name": "Ouns", "last_name": "Lateef", "email": "a@example.invalid",
               "female_count": 2, "male_count": 0, "start_date": "2026-11-21", "end_date": "2026-11-22",
               "promoter": "Thomas Vale"}
    bookings = [
        {"venue": "LAVO Party Brunch", "date": "2026-11-21", "order_id": "AAA"},
        {"venue": "TAO Nightclub", "date": "2026-11-22", "order_id": "BBB"},
    ]
    plain = worker_emails.team_email("r1", {}, request, bookings)
    assert "(1F 0M)" not in plain["text"]
    assert "Party: 2 female guests (free)" in plain["text"]
    raw = {"comments": "Party by night: 2026-11-21 1F 0M; 2026-11-22 2F 0M"}
    planned = worker_emails.team_email("r1", raw, request, bookings)
    assert "LAVO Party Brunch (1F 0M)" in planned["text"]
    assert "TAO Nightclub (2F 0M)" in planned["text"]


def test_drais_team_wording_unchanged_without_plan():
    request = {"first_name": "Ouns", "last_name": "Lateef", "email": "a@example.invalid",
               "female_count": 2, "male_count": 0, "start_date": "2026-11-21", "end_date": "2026-11-22"}
    bookings = [{"venue": "Drai's After Hours", "date": "2026-11-21"},
                {"venue": "Drai's After Hours", "date": "2026-11-22"}]
    plain = worker_emails.drais_team_email("r1", {}, request, bookings)
    assert "Party: 2 women, 0 men" in plain["text"]
    assert "(confirmation email sent, no Speakeasy signup)" in plain["text"]
    assert "(1F 0M)" not in plain["text"]
    raw = {"comments": "Party by night: 2026-11-21 1F 0M; 2026-11-22 2F 0M"}
    planned = worker_emails.drais_team_email("r1", raw, request, bookings)
    assert "Party: 2 women, 0 men" in planned["text"]
    assert "Sat 2026-11-21 (1F 0M) (confirmation email sent, no Speakeasy signup)" in planned["text"]
    assert "Sun 2026-11-22 (2F 0M) (confirmation email sent, no Speakeasy signup)" in planned["text"]
    assert planned["subject"].endswith("(2F 0M)")
