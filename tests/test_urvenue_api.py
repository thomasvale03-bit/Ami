"""UrGuestlist API booking path: list, create, confirm, and fallback.

These tests never call the live API. HTTP is mocked. No real guest is created.
"""
import io
import json
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

import urvenue


NIGHT = date(2026, 11, 22)
GUEST = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "male_count": 2,
    "female_count": 3,
}
REF = "testref001"
ITEM = {
    "id": "401220209571",
    "internalname": "General List",
    "displayname": "Guest List",
    "genders": "FMU",
    "status": "7",
}


def _line():
    return urvenue.booking_line(GUEST, REF)[0]


def _chat(**overrides):
    row = {
        "status": "rsvped",
        "partyname": "Ada Lovelace",
        "original": _line(),
        "email": "ada@example.com",
        "comment": "",
        "message": "Ada Lovelace with guests (2M 3F)",
        "itemname": "Guest List",
        "itemid": ITEM["id"],
        "ownername": "Thomas Vale",
        "inviteid": "1",
        "expected": {"males": 2, "females": 3, "unspecified": 0, "guests": 5},
    }
    row.update(overrides)
    return row


def _uv(status="success", data=None, message="", reason=None, http=None):
    if http:
        return {"uv": {"success": 0, "data": {"message": message, "http_code": http}}}
    suc = {"status": status}
    if message:
        suc["message"] = message
    if reason:
        suc["reason"] = reason
    return {"uv": {"success": suc, "data": data}}


class FakeResp:
    def __init__(self, payload, status=200, headers=None):
        raw = json.dumps(payload).encode()
        self._raw = raw
        self.status = status
        self.headers = headers or {"X-Uv-Xsrf": "xsrf-test"}

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def open(self, req, timeout=None):
        url = req.full_url
        parsed = urlparse(url)
        path = parsed.path  # /v1/guestlist/dayview/json/
        method = req.get_method()
        body = req.data.decode() if req.data else ""
        form = {k: v[0] for k, v in parse_qs(body).items()} if body else {}
        # Never store password/apikey values
        safe_form = {k: ("<redacted>" if k.lower() in {"password", "apikey", "loginkey"} else v)
                     for k, v in form.items()}
        self.calls.append({"method": method, "path": path, "form_keys": sorted(form), "safe_form": safe_form})
        result = self.handler(method, path, form)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, tuple) and result[0] >= 400:
            status, payload = result
            raw = json.dumps(payload).encode()
            raise HTTPError(url, status, "error", hdrs := _Hdr({"Content-Type": "application/json"}), io.BytesIO(raw))
        return FakeResp(result)


class _Hdr(dict):
    def get(self, k, default=None):
        for key, val in self.items():
            if key.lower() == str(k).lower():
                return val
        return default


@pytest.fixture
def api_env(monkeypatch, tmp_path):
    monkeypatch.setenv("URVENUE_EMAIL", "info@example.com")
    monkeypatch.setenv("URVENUE_PASSWORD", "dummy-password-not-real")
    monkeypatch.delenv("URVENUE_API_DISABLE", raising=False)
    monkeypatch.delenv("URVENUE_API_KEY", raising=False)
    monkeypatch.setenv("AMY_WORKER_DIR", str(tmp_path / "wd"))


def _install(monkeypatch, opener):
    monkeypatch.setattr(urvenue.urllib.request, "build_opener", lambda *a, **k: opener)


def _script(login_ok=True, chats=None, items=None, saved=1, post=None, second_chats="same"):
    chats = [] if chats is None else chats
    items = [ITEM] if items is None else items
    state = {"dayviews": 0}

    def handler(method, path, form):
        if path.endswith("/users/login/json/"):
            if not login_ok:
                return _uv("fail", message="Unable to sign in", reason="invalidlogin")
            return _uv("success", {"userid": "401302498642", "csrf": "x"})
        if path.endswith("/guestlist/dayview/json/"):
            state["dayviews"] += 1
            if state["dayviews"] == 1:
                return _uv("success", {"chats": chats, "states": {}, "channels": {}})
            if second_chats == "same":
                rows = chats
            elif second_chats == "added":
                rows = chats + [_chat()]
            else:
                rows = second_chats
            return _uv("success", {"chats": rows, "states": {}, "channels": {}})
        if path.endswith("/guestlist/dayitem/json/"):
            return _uv("success", {"list": items, "users": [], "nationalities": []})
        if method == "POST" and path.endswith("/guestlist/mylist/json/"):
            if post is not None:
                return post
            return _uv("success", {"saved": saved, "errors": [], "submitted": 1})
        raise AssertionError(f"unexpected {method} {path}")

    return handler



def test_find_dayview_row_matches_name_and_ref():
    chats = [
        _chat(partyname="Other Guest", original="Other Guest 1M  a@b.com", email="a@b.com", message="Other Guest with guests (1M)"),
        _chat(partyname="Ada Lovelace", original=_line() + " leftover", email=""),
    ]
    assert urvenue.find_dayview_row(chats, "Ada Lovelace", "ada@example.com", REF)["partyname"] == "Ada Lovelace"
    assert urvenue.find_dayview_row(chats, "  ada   lovelace ", "", "")["partyname"] == "Ada Lovelace"
    assert urvenue.find_dayview_row(
        [_chat(partyname="X", original="X 1M", comment="Playmaker Entertainment AMY testref001", email="")],
        "Nobody", "", REF,
    )["comment"].endswith("testref001")
    assert urvenue.find_dayview_row(chats, "Missing Person", "nobody@example.com", "nope") is None


def test_find_dayview_row_ignores_shared_email_when_name_differs():
    """A shared email does not identify the row when the name differs."""
    chats = [
        _chat(partyname="Thomas Vale", original="Thomas Vale 3M 3F  thomas.vale03@gmail.com",
              email="thomas.vale03@gmail.com", message="Thomas Vale with guests (3M 3F)"),
    ]
    assert urvenue.find_dayview_row(chats, "Obed Vale", "thomas.vale03@gmail.com", "1a206aa51e70667c") is None
    # Same person, different spacing/case, still matches for post-create verification.
    assert urvenue.find_dayview_row(chats, "thomas  vale", "other@example.com", "")["partyname"] == "Thomas Vale"


def test_pick_list_item_prefers_general_list():
    items = [
        {"id": "1", "internalname": "VIP", "displayname": "VIP List"},
        {"id": "2", "internalname": "General List", "displayname": "Guest List"},
    ]
    assert urvenue.pick_list_item(items)["id"] == "2"
    assert urvenue.pick_list_item([]) is None


def test_api_books_and_verifies_without_posting_twice(api_env, monkeypatch):
    opener = FakeOpener(_script(chats=[], second_chats="added"))
    _install(monkeypatch, opener)
    result = urvenue.add_guest("XS Nightclub", NIGHT, GUEST, ref=REF)
    assert result["verified"] is True
    assert result["via"] == "api"
    assert result["list_item"] == "General List"
    assert "Ada Lovelace" in result["row"]
    methods_paths = [(c["method"], c["path"]) for c in opener.calls]
    assert methods_paths[0][0] == "POST" and methods_paths[0][1].endswith("/users/login/json/")
    posts = [c for c in opener.calls if c["method"] == "POST" and c["path"].endswith("/guestlist/mylist/json/")]
    assert len(posts) == 1
    # Dayview GET happened before the create (list read, then confirm read)
    gets = [c for c in opener.calls if c["method"] == "GET" and c["path"].endswith("/guestlist/dayview/json/")]
    assert len(gets) == 2
    assert methods_paths.index(("GET", gets[0]["path"])) < methods_paths.index(("POST", posts[0]["path"]))


def test_api_posts_even_when_already_on_the_list(api_env, monkeypatch):
    """Always POST guestlist/mylist, even if a matching name / ref / AMY
    comment is already on dayview."""
    opener = FakeOpener(_script(chats=[_chat()], second_chats=[_chat()]))
    _install(monkeypatch, opener)
    result = urvenue.add_guest("XS Nightclub", NIGHT, GUEST, ref=REF)
    assert result["verified"] is True
    assert result["via"] == "api"
    assert sum(1 for c in opener.calls if c["path"].endswith("/guestlist/mylist/json/")) == 1



def test_shared_email_different_name_is_not_a_duplicate(api_env, monkeypatch):
    existing = _chat(
        partyname="Thomas Vale",
        original="Thomas Vale 3M 3F  thomas.vale03@gmail.com",
        email="thomas.vale03@gmail.com",
        message="Thomas Vale with guests (3M 3F)",
        expected={"males": 3, "females": 3, "unspecified": 0, "guests": 6},
    )
    guest = {
        "first_name": "Obed",
        "last_name": "Vale",
        "email": "thomas.vale03@gmail.com",
        "male_count": 3,
        "female_count": 3,
    }
    opener = FakeOpener(_script(chats=[existing], second_chats=[existing, _chat(
        partyname="Obed Vale",
        original="Obed Vale thomas.vale03@gmail.com 3M 3F !Playmaker Entertainment AMY testref001",
        email="thomas.vale03@gmail.com",
        message="Obed Vale with guests (3M 3F)",
        expected={"males": 3, "females": 3, "unspecified": 0, "guests": 6},
    )]))
    _install(monkeypatch, opener)
    result = urvenue.add_guest("XS Nightclub", NIGHT, guest, ref=REF)
    assert result["verified"] is True
    assert result["via"] == "api"
    assert "Obed Vale" in result["row"]
    assert sum(1 for c in opener.calls if c["path"].endswith("/guestlist/mylist/json/")) == 1



def test_invalid_login_does_not_fall_back(api_env, monkeypatch):
    opener = FakeOpener(_script(login_ok=False))
    _install(monkeypatch, opener)
    called = []
    monkeypatch.setattr(urvenue, "add_guest_browser", lambda *a, **k: called.append(1) or {})
    with pytest.raises(urvenue.LoginUnavailable):
        urvenue.add_guest("XS Nightclub", NIGHT, GUEST, ref=REF)
    assert called == []


def test_network_error_before_write_falls_back(api_env, monkeypatch):
    def handler(method, path, form):
        if path.endswith("/users/login/json/"):
            raise URLError("timed out")
        raise AssertionError("should have fallen back before later calls")

    opener = FakeOpener(handler)
    _install(monkeypatch, opener)
    monkeypatch.setattr(urvenue, "add_guest_browser", lambda *a, **k: {"verified": True, "via": "browser"})
    result = urvenue.add_guest("Encore Beach Club", NIGHT, GUEST, ref=REF)
    assert result["via"] == "browser"


def test_no_items_is_not_open_without_browser(api_env, monkeypatch):
    opener = FakeOpener(_script(chats=[], items=[]))
    _install(monkeypatch, opener)
    called = []
    monkeypatch.setattr(urvenue, "add_guest_browser", lambda *a, **k: called.append(1) or {})
    with pytest.raises(urvenue.NotOpen):
        urvenue.add_guest("LIV Beach", NIGHT, GUEST, ref=REF)
    assert called == []
    assert not any(c["path"].endswith("/guestlist/mylist/json/") for c in opener.calls)


def test_missing_row_after_post_is_uncertain_and_not_retried(api_env, monkeypatch):
    opener = FakeOpener(_script(chats=[], second_chats=[]))
    _install(monkeypatch, opener)
    called = []
    monkeypatch.setattr(urvenue, "add_guest_browser", lambda *a, **k: called.append(1) or {})
    with pytest.raises(urvenue.SubmissionUncertain):
        urvenue.add_guest("XS Nightclub", NIGHT, GUEST, ref=REF)
    assert called == []
    assert sum(1 for c in opener.calls if c["path"].endswith("/guestlist/mylist/json/")) == 1


def test_post_http_error_is_uncertain_not_browser(api_env, monkeypatch):
    opener = FakeOpener(_script(chats=[], post=(500, _uv(http="500", message="server error"))))
    _install(monkeypatch, opener)
    called = []
    monkeypatch.setattr(urvenue, "add_guest_browser", lambda *a, **k: called.append(1) or {})
    with pytest.raises(urvenue.SubmissionUncertain):
        urvenue.add_guest("XS Nightclub", NIGHT, GUEST, ref=REF)
    assert called == []


def test_password_and_keys_never_appear_in_errors(api_env, monkeypatch):
    def handler(method, path, form):
        if path.endswith("/users/login/json/"):
            return _uv("fail", message="bad dummy-password-not-real and key", reason="invalidlogin")
        raise AssertionError(path)

    opener = FakeOpener(handler)
    _install(monkeypatch, opener)
    with pytest.raises(urvenue.LoginUnavailable) as exc:
        urvenue.add_guest("XS Nightclub", NIGHT, GUEST, ref=REF)
    msg = str(exc.value)
    assert "dummy-password-not-real" not in msg
    assert "<redacted>" in msg or "login failed" in msg.lower()


def test_api_disable_uses_browser_only(api_env, monkeypatch):
    monkeypatch.setenv("URVENUE_API_DISABLE", "true")
    opener = FakeOpener(lambda *a, **k: (_ for _ in ()).throw(AssertionError("API should be skipped")))
    _install(monkeypatch, opener)
    monkeypatch.setattr(urvenue, "add_guest_browser", lambda *a, **k: {"verified": True, "via": "browser"})
    result = urvenue.add_guest("XS Nightclub", NIGHT, GUEST, ref=REF)
    assert result["via"] == "browser"
    assert opener.calls == []


def test_rehearse_does_not_post(api_env, monkeypatch):
    opener = FakeOpener(_script(chats=[]))
    _install(monkeypatch, opener)
    result = urvenue.add_guest("XS Nightclub", NIGHT, GUEST, ref=REF, rehearse=True)
    assert result["rehearsal"] is True
    assert result["via"] == "api"
    assert not any(c["path"].endswith("/guestlist/mylist/json/") for c in opener.calls)
