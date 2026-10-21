"""Auto-add Drai's After Hours guests to Playmaker's own SpeakeasyGo guest list.

This logs into Playmaker's OWN SpeakeasyGo manager account (the operator's
credentials, supplied via environment variables — never committed) and adds
guests through the dashboard's built-in "Add new" flow, the same way a person
would. There is no bot-check on the SpeakeasyGo manager; this is ordinary
automation of an authorized account using the product's own feature.

Everything here is gated behind SPEAKEASY_AUTO=true AND credentials being
present, so it stays dormant until it's configured and tested. If anything
fails (login, a field moves, a CAPTCHA ever appears), it logs and returns a
failure for that guest — it never raises into Amy's main loop, and the guest's
door-text confirmation still goes out regardless.

Config (set in Railway, not in code):
    SPEAKEASY_AUTO       = "true" to enable
    SPEAKEASY_EMAIL      = manager login email
    SPEAKEASY_PASSWORD   = manager login password
    SPEAKEASY_LOGIN_URL  = the manager login page URL
    SPEAKEASY_MANAGER_URL= the guest-list dashboard URL (optional; defaults below)
"""
import logging
import os

log = logging.getLogger("amy.speakeasy")

LOGIN_URL = os.environ.get("SPEAKEASY_LOGIN_URL", "https://manager.speakeasygo.com/login").strip()
MANAGER_URL = os.environ.get("SPEAKEASY_MANAGER_URL", "https://manager.speakeasygo.com/guest-list").strip()
EMAIL = os.environ.get("SPEAKEASY_EMAIL", "").strip()
PASSWORD = os.environ.get("SPEAKEASY_PASSWORD", "")


def auto_on():
    """True only when enabled AND fully configured — otherwise Amy does nothing
    here and falls back to the door-text confirmation alone."""
    enabled = os.environ.get("SPEAKEASY_AUTO", "").strip().lower() in ("1", "true", "yes")
    return enabled and bool(EMAIL and PASSWORD and LOGIN_URL)


def _browser(p):
    # CHROMIUM_PATH is set in dev; on Railway's Playwright image it's unset and
    # Playwright finds its own Chromium (pass None, same as tao_portal).
    return p.chromium.launch(headless=True, executable_path=os.environ.get("CHROMIUM_PATH") or None)


def _login(page):
    """Log into the SpeakeasyGo manager with the operator's own credentials.
    Selectors are finalised against the real login page."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    # Real login form: #email, #password, "Sign In with Email" (no CAPTCHA).
    page.fill("#email", EMAIL)
    page.fill("#password", PASSWORD)
    page.click("button:has-text('Sign In with Email')")
    page.wait_for_load_state("networkidle", timeout=30000)
    page.wait_for_timeout(1500)


def _parse_shown_date(text, year):
    """Parse the date-nav label like 'Fri, Oct 9' into a date, using `year`."""
    import datetime, re
    m = re.search(r"([A-Za-z]{3,})\s+(\d{1,2})", text or "")
    if not m:
        return None
    mon, day = m.group(1)[:3], int(m.group(2))
    months = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,
              "Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
    if mon not in months:
        return None
    try:
        return datetime.date(year, months[mon], day)
    except ValueError:
        return None


def _goto_date(page, target):
    """Step the date-nav arrows until the shown date is `target` (a date).
    Best-effort; the modal subtitle is the real safety check before saving."""
    import datetime
    date_btn = page.get_by_text(__import__("re").compile(r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun),"), exact=False).first
    for _ in range(120):
        shown = _parse_shown_date(date_btn.inner_text(), target.year)
        if shown == target:
            return True
        arrow = "following-sibling::button[1]" if (shown is None or shown < target) else "preceding-sibling::button[1]"
        try:
            date_btn.locator(f"xpath={arrow}").click(timeout=4000)
        except Exception:
            # fall back to the two nav buttons flanking the date label
            btns = page.locator("header button, nav button, button")
            (btns.nth(1) if (shown is None or shown < target) else btns.nth(0)).click()
        page.wait_for_timeout(500)
    return False


def _set_count(modal, label, n):
    """Set the Men/Women stepper to n: type into its number box if it's an
    input, otherwise click its '+' n times."""
    if n <= 0:
        return
    box = modal.locator(f"xpath=.//*[normalize-space(text())='{label}']/ancestor::div[1]").first
    inp = box.locator("input")
    if inp.count() > 0:
        inp.first.fill(str(n))
        return
    plus = box.get_by_role("button").last  # layout is [− value +]; '+' is last
    for _ in range(n):
        plus.click()
        modal.page.wait_for_timeout(120)


def _add_one(page, date_iso, first, last, male, female):
    """Add one guest for `date_iso` via the 'Add new' dialog. Verifies the
    dialog's own date matches before saving, so a guest is never added to the
    wrong night. Raises on any failure so the caller records it per guest."""
    import datetime
    target = datetime.date.fromisoformat(date_iso)
    page.goto(MANAGER_URL, wait_until="networkidle")
    page.wait_for_timeout(1500)
    _goto_date(page, target)
    page.get_by_role("button", name="Add new").click()
    modal = page.get_by_role("dialog")
    modal.wait_for(timeout=15000)
    # Safety: the modal says "Create a guest for Friday, October 9, 2026."
    subtitle = modal.inner_text()
    shown = None
    import re as _re
    m = _re.search(r"for\s+[A-Za-z]+,\s+([A-Za-z]+\s+\d{1,2},\s+\d{4})", subtitle)
    if m:
        try:
            shown = datetime.datetime.strptime(m.group(1), "%B %d, %Y").date()
        except ValueError:
            shown = None
    if shown != target:
        try: modal.get_by_role("button", name="Cancel").click()
        except Exception: pass
        raise RuntimeError(f"dialog date {shown} != target {target}; not adding")
    modal.get_by_placeholder("Enter first name").fill(first)
    modal.get_by_placeholder("Enter last name").fill(last or first)
    _set_count(modal, "Men", int(male or 0))
    _set_count(modal, "Women", int(female or 0))
    modal.get_by_role("button", name="Save Guest List").click()
    page.wait_for_timeout(2000)


def add_guests_for_night(date_iso, guests, force=False):
    """guests: list of {"name", "male", "female"}. Returns per-guest results
    [{"name", "ok", "error"}]. Best-effort: never raises. force=True runs even
    when SPEAKEASY_AUTO is off (used by the one-shot test), as long as creds
    are set."""
    if not force and not auto_on():
        return [{"name": g["name"], "ok": False, "error": "speakeasy auto off"} for g in guests]
    if not (EMAIL and PASSWORD):
        return [{"name": g["name"], "ok": False, "error": "speakeasy creds not set"} for g in guests]
    from playwright.sync_api import sync_playwright
    results = []
    try:
        with sync_playwright() as p:
            b = _browser(p)
            page = b.new_context(locale="en-US").new_page()
            page.set_default_timeout(20000)
            _login(page)
            for g in guests:
                try:
                    first = g.get("first") or (g.get("name", "").split(" ", 1) + [""])[0]
                    last = g.get("last") or (g.get("name", "").split(" ", 1) + [""])[1]
                    _add_one(page, date_iso, first.strip(), last.strip(),
                             g.get("male", 0), g.get("female", 0))
                    results.append({"name": g["name"], "ok": True, "error": None})
                    log.info("SpeakeasyGo: added %s for %s", g["name"], date_iso)
                except Exception as exc:  # noqa: BLE001 - one guest failing must not sink the batch
                    results.append({"name": g["name"], "ok": False, "error": str(exc)})
                    log.warning("SpeakeasyGo: failed to add %s for %s: %s", g["name"], date_iso, exc)
            b.close()
    except Exception as exc:  # noqa: BLE001 - login/browser failure: fall back to door text
        log.warning("SpeakeasyGo auto-add unavailable (%s); door text still sent", exc)
        return [{"name": g["name"], "ok": False, "error": str(exc)} for g in guests]
    return results


def test_add_guest(date_iso, guest, shot_path="/tmp/speakeasy_test.png"):
    """Run one add with creds (regardless of SPEAKEASY_AUTO) and always capture a
    screenshot so a failure can be seen. Returns {ok, error, screenshot}."""
    if not (EMAIL and PASSWORD):
        return {"ok": False, "error": "speakeasy creds not set", "screenshot": None}
    from playwright.sync_api import sync_playwright
    out = {"ok": False, "error": None, "screenshot": None}
    try:
        with sync_playwright() as p:
            b = _browser(p)
            page = b.new_context(locale="en-US").new_page()
            page.set_default_timeout(20000)
            try:
                first = guest.get("first") or (guest.get("name", "").split(" ", 1) + [""])[0]
                last = guest.get("last") or (guest.get("name", "").split(" ", 1) + [""])[1]
                _add_one(page, date_iso, first.strip(), last.strip(),
                         guest.get("male", 0), guest.get("female", 0))
                out["ok"] = True
            except Exception as exc:  # noqa: BLE001
                out["error"] = str(exc)
            finally:
                try:
                    page.screenshot(path=shot_path, full_page=True)
                    out["screenshot"] = shot_path
                except Exception:
                    pass
                b.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{out['error'] or ''} / {exc}".strip(" /")
    return out


def _test_add(first, last, male, female, date_iso):
    """One-guest live test. Runs with creds from the environment regardless of
    SPEAKEASY_AUTO, so you can verify before enabling the real flow. Check your
    SpeakeasyGo dashboard for the guest afterward.

        python speakeasy.py test First Last 2 1 2026-10-17
    """
    if not (EMAIL and PASSWORD):
        print("Set SPEAKEASY_EMAIL and SPEAKEASY_PASSWORD first."); return
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = _browser(p)
        page = b.new_context(locale="en-US").new_page(); page.set_default_timeout(20000)
        try:
            _login(page)
            _add_one(page, date_iso, first, last, int(male), int(female))
            print(f"OK: added {first} {last} ({male}M/{female}F) for {date_iso} — check the dashboard.")
        except Exception as exc:  # noqa: BLE001
            print("FAILED:", exc)
            try:
                page.screenshot(path="/tmp/speakeasy_fail.png")
                print("screenshot: /tmp/speakeasy_fail.png")
            except Exception:
                pass
        finally:
            b.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 7 and sys.argv[1] == "test":
        _test_add(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])
    else:
        print(__doc__)
        print("\nUsage: python speakeasy.py test <First> <Last> <Men> <Women> <YYYY-MM-DD>")
