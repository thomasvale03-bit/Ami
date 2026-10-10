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
    return p.chromium.launch(headless=True, executable_path=os.environ["CHROMIUM_PATH"])


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


def _add_one(page, date_iso, name, male, female):
    """Add a single guest for one night via the dashboard's 'Add new' dialog.
    Steps finalised against the real Add-new form; raises on failure so the
    caller records it per guest."""
    # TODO(finalise from screenshot): navigate to `date_iso`, click "Add new",
    # fill name + male/female counts, save, confirm the row appears.
    raise NotImplementedError("Add-new flow pending the real dashboard form.")


def add_guests_for_night(date_iso, guests):
    """guests: list of {"name", "male", "female"}. Returns per-guest results
    [{"name", "ok", "error"}]. Best-effort: never raises."""
    if not auto_on():
        return [{"name": g["name"], "ok": False, "error": "speakeasy auto off"} for g in guests]
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
                    _add_one(page, date_iso, g["name"], g.get("male", 0), g.get("female", 0))
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
