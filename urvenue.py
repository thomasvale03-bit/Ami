"""Guest-list sign-ups on Playmaker's urvenue.me (UrGuestlist) account for
LIV, LIV Beach, XS, Encore Beach Club and Encore Beach Club At Night
(Thomas, Nov 21 2026).

Flow:
  API first (https://api.urvenue.me, UrGuestlist system id 4): sign in with
  URVENUE_EMAIL / URVENUE_PASSWORD, GET guestlist/dayview, POST
  guestlist/mylist to add the party, then re-read dayview to confirm.
  Cross-list dedupe (AlreadyListed when a name / ref / AMY comment already
  appears on the dayview or browser list) is off unless AMY_DEDUPE or
  AMY_WORKER_DUPLICATE_CHECK is true (Thomas, Nov 22 2026: always add).
  URVENUE_API_DISABLE=true skips the API and uses the browser. On an API
  error before a write, fall back to the browser. SubmissionUncertain is
  never retried in the browser (no blind resubmit). AlreadyListed, when
  dedupe is on, is also not retried in the browser.

  Browser (same one the TAO worker uses, see tao_portal._browser_page):
  1. open the venue's month calendar on guestlist.urvenue.me; sign in on
     urvenue.me first when the saved session is gone (email step, then the
     password step) with URVENUE_EMAIL / URVENUE_PASSWORD from the worker env;
  2. check the venue switcher shows the requested venue (never another one);
  3. find the night on the calendar: no event tile = list not posted yet;
  4. open the event, require the "<event> - Open" status;
  5. search the event's list for the guest (AlreadyListed only when dedupe is on);
  6. "+ Add Guest" -> General List, owner Thomas Vale, one textarea line
     `Name email 2M 3F !internal comment` -> "Add Guests";
  7. re-read the list and require the guest's row.

Everything before the final click raises an UrvenueError subclass (nothing
submitted). After the click, a missing row raises SubmissionUncertain (never
resubmitted). The password is only ever typed into the password field: never
logged, printed or put in an exception message.
"""
import hashlib
import json
import logging
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from datetime import date
from pathlib import Path

log = logging.getLogger("amy.urvenue")

LOGIN_URL = os.environ.get("URVENUE_LOGIN_URL", "https://urvenue.me/").strip()
ACCOUNTS_URL = "https://urvenue.me/accounts"
GUESTLIST_HOST = "guestlist.urvenue.me"
CALENDAR_URL = "https://guestlist.urvenue.me/2/{acct}/{vid}/guestlist/dayview"
DEFAULT_EMAIL = "info@playmakerentertainment.com"
LIST_ITEM = os.environ.get("URVENUE_LIST_ITEM", "General List")
OWNER = os.environ.get("URVENUE_OWNER", "Thomas Vale")

# UrGuestlist operations API. The published SPA at guestlist.urvenue.me calls
# https://api.urvenue.me/v1/{path}/json/ with systemid 4. Login uses the
# public frontend appApiKey (overridable) plus URVENUE_EMAIL/URVENUE_PASSWORD.
# URVENUE_API_KEY is the user key; it is not required once a session exists.
API_BASE = os.environ.get("URVENUE_API_BASE", "https://api.urvenue.me").rstrip("/")
API_SYSTEM_ID = os.environ.get("URVENUE_API_SYSTEM_ID", "4").strip() or "4"
# Public client key from the UrGuestlist SPA bundle (not a user secret).
DEFAULT_APP_API_KEY = "AZVOPVDPTLAKEWPDFKDMQAVV"

# canonical venue -> urvenue account id, venue id and the venue-switcher label
# that proves the right venue is open (ids read from urvenue Nov 21 2026; the
# LIV Nightclub id 1121561 was found through the LIV account's venue switcher,
# whose label for it is "LIV Las Vegas"). A venue with no id is opened from
# SWITCH_FROM through the switcher and the id it lands on is cached.
VENUES = {
    "LIV Beach": {"acct": "2386", "vid": "1214881", "label": r"\bLIV\s+Beach\b", "avoid": None},
    "LIV Nightclub": {"acct": "2386", "vid": "1121561", "label": r"\bLIV\b(?:\s+(?:Las\s+Vegas|Nightclub))?",
                      "avoid": r"beach|day"},
    "XS Nightclub": {"acct": "79", "vid": "1115", "label": r"\bXS\b", "avoid": None},
    "Encore Beach Club": {"acct": "79", "vid": "1103", "label": r"\bEncore\s+Beach\s+Club\b|\bEBC\b",
                          "avoid": r"night"},
    "Encore Beach Club At Night": {"acct": "79", "vid": "1163",
                                   "label": r"\bEncore\s+Beach\s+Club\s+(?:At\s+)?Night\b|\bEBC\s+(?:At\s+)?Night\b",
                                   "avoid": None},
}
# Seed used to open the LIV account before switching to LIV Nightclub.
SWITCH_FROM = {"LIV Nightclub": "LIV Beach"}

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
MON3 = [m[:3] for m in MONTHS]
WAIT_MS = int(os.environ.get("URVENUE_WAIT_MS", "2500"))


class UrvenueError(Exception):
    """Stopped before anything was submitted."""


class LoginUnavailable(UrvenueError):
    """URVENUE_PASSWORD is not set and there is no saved session."""


class NotPosted(UrvenueError):
    """No event on that night's calendar: the list is not posted yet."""


class NotOpen(UrvenueError):
    def __init__(self, status, event=""):
        super().__init__(f"event {event!r} status is {status!r}, not Open")
        self.status, self.event = status, event


class AlreadyListed(UrvenueError):
    def __init__(self, row):
        super().__init__("guest is already on this list")
        self.row = row


class SubmissionUncertain(Exception):
    """'Add Guests' was clicked but the row could not be confirmed."""


# --- pure helpers (unit-tested) -----------------------------------------------

def credentials():
    email = os.environ.get("URVENUE_EMAIL", "").strip() or DEFAULT_EMAIL
    return email, os.environ.get("URVENUE_PASSWORD", "")


def has_password():
    return bool(os.environ.get("URVENUE_PASSWORD", ""))


def _cache_path():
    return Path(os.environ.get("AMY_WORKER_DIR", "/data/worker")) / "urvenue_venues.json"


def _load_cache():
    try:
        return json.loads(_cache_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def remember_venue_id(venue, acct, vid):
    data = _load_cache()
    data[venue] = f"{acct}/{vid}"
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, path)
    except OSError:
        log.warning("could not cache urvenue venue id for %s", venue)


def venue_ids(venue):
    """(acct, vid or None). URVENUE_VENUE_IDS='{"LIV Nightclub": "2386/123"}'
    overrides; then ids discovered earlier; then the built-in map."""
    spec = VENUES[venue]
    try:
        override = json.loads(os.environ.get("URVENUE_VENUE_IDS", "") or "{}").get(venue)
    except ValueError:
        override = None
    found = override or _load_cache().get(venue)
    if found and re.fullmatch(r"\d+/\d+", str(found)):
        acct, vid = str(found).split("/")
        return acct, vid
    return spec["acct"], spec["vid"]


def calendar_url(acct, vid):
    return CALENDAR_URL.format(acct=acct, vid=vid)


def ids_from_url(url):
    m = re.search(re.escape(GUESTLIST_HOST) + r"(?::\d+)?/\d+/(\d+)/(\d+)/", url or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def label_matches(venue, text):
    spec = VENUES[venue]
    text = (text or "").strip()
    if not text or not re.search(spec["label"], text, re.I):
        return False
    return not (spec.get("avoid") and re.search(spec["avoid"], text, re.I))


def yymmdd(d):
    return d.strftime("%y%m%d")


def confirmation_ref(ref, venue, night):
    """Stable reference for a urvenue sign-up (urvenue shows no order number)."""
    digest = hashlib.sha256(f"{ref}|{venue}|{night}".encode()).hexdigest()[:8].upper()
    return f"UV-{digest}"


def clean_name(first, last):
    name = f"{first or ''} {last or ''}"
    name = re.sub(r"[^A-Za-z\u00C0-\u024F' .-]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .-'")
    return name


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def booking_line(guest, ref):
    """One textarea line: `Name email 2M 3F !comment` (urvenue's own format;
    the comment must come last). No phone number, no guest message."""
    name = clean_name(guest.get("first_name"), guest.get("last_name"))
    if not name:
        raise UrvenueError("guest name is empty after cleaning")
    email = (guest.get("email") or "").strip()
    if not EMAIL_RE.match(email):
        raise UrvenueError("guest email is not a plain address")
    male, female = int(guest.get("male_count") or 0), int(guest.get("female_count") or 0)
    if male < 0 or female < 0 or male + female < 1:
        raise UrvenueError("party counts invalid")
    counts = " ".join(x for x in ((f"{male}M" if male else ""), (f"{female}F" if female else "")) if x)
    comment = re.sub(r"[^A-Za-z0-9 :._-]", "", f"Playmaker Entertainment AMY {ref}")[:80]
    return f"{name} {email} {counts} !{comment}", name


def parse_label_year(mon, header_month, header_year):
    """Calendar cells say 'Nov 30' / 'Dec 1'; the year comes from the month
    header ('November 2026') adjusted across a year boundary."""
    idx = MON3.index(mon) + 1
    diff = idx - header_month
    if diff > 6:
        return header_year - 1
    if diff < -6:
        return header_year + 1
    return header_year


def parse_event_page(text):
    """{'event', 'status', 'event_time', 'date'} from the event page text:
    '<Event> - Open' above 'Operation Hours', 'From 11:30am to 6:00pm', and the
    'Guest Lists Nov 21, 2026' header."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    out = {"event": "", "status": "", "event_time": "", "date": None}
    for i, ln in enumerate(lines):
        if ln.lower().startswith("operation hours"):
            j = i - 1
            while j >= 0 and lines[j] in ("-",):
                j -= 1
            title = lines[j] if j >= 0 else ""
            m = re.match(r"^(.*\S)\s*-\s*([A-Za-z][A-Za-z ]*)$", title)
            if m:
                out["event"], out["status"] = m.group(1).strip(), m.group(2).strip()
            elif j >= 1 and re.fullmatch(r"-\s*[A-Za-z][A-Za-z ]*", title):
                out["event"], out["status"] = lines[j - 1], title.lstrip("- ").strip()
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            t = re.search(r"from\s+(\d{1,2}(?::\d{2})?\s*[ap]m)", nxt, re.I)
            if t:
                out["event_time"] = t.group(1).replace(" ", "").lower()
            break
    m = re.search(r"\b(" + "|".join(MON3) + r")[a-z]*\.?\s+(\d{1,2}),\s+(\d{4})", text or "")
    if m:
        try:
            out["date"] = date(int(m.group(3)), MON3.index(m.group(1)) + 1, int(m.group(2)))
        except ValueError:
            pass
    return out


def find_row(text, name, email=""):
    """The list line naming this guest (case-insensitive), or None."""
    name_l, email_l = name.lower(), (email or "").lower()
    for ln in (text or "").splitlines():
        low = ln.lower()
        if (name_l and name_l in low) or (email_l and email_l in low):
            return ln.strip()[:300]
    return None


def cell_rect(labels, target):
    """Rectangle of the calendar cell whose date label is `target`. Labels sit
    at the top-right corner of each cell."""
    rights = sorted({round(l["r"]) for l in labels})
    tops = sorted({round(l["y"]) for l in labels})
    dx = [b - a for a, b in zip(rights, rights[1:]) if b - a > 20]
    dy = [b - a for a, b in zip(tops, tops[1:]) if b - a > 20]
    colw = statistics.median(dx) if dx else 115
    rowh = statistics.median(dy) if dy else 85
    return {"left": target["r"] - colw + 2, "right": target["r"] + 1,
            "top": target["y"] - 3, "bottom": target["y"] - 3 + rowh - 2}


# --- browser side ----------------------------------------------------------------

LABELS_JS = r"""() => {
  const re = /^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})$/;
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length) continue;
    const t = (el.innerText || el.textContent || '').trim();
    const m = t.match(re);
    if (!m) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    out.push({t: t, mon: m[1], day: +m[2], x: r.left, y: r.top, r: r.right, b: r.bottom});
  }
  return out;
}"""

HEADER_JS = r"""() => {
  const re = /^(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})$/;
  for (const el of document.querySelectorAll('body *')) {
    const t = (el.innerText || '').trim();
    if (!re.test(t)) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    return {t: t, x: r.left, y: r.top, r: r.right, b: r.bottom};
  }
  return null;
}"""

# Small clickable things on the month header's row, right half: prev / next.
ARROWS_JS = r"""(hdr) => {
  const out = [];
  const W = window.innerWidth, H = window.innerHeight;
  for (const el of document.querySelectorAll('button, a, [role=button], i, span, svg')) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.width > 70 || r.height > 60) continue;
    if (r.left < W * 0.6 || r.right > W || r.bottom > H || r.top < 0) continue;  // on screen only
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    const cy = r.top + r.height / 2;
    if (cy < hdr.y - 25 || cy > hdr.b + 25) continue;
    if ((el.innerText || '').trim().length > 2) continue;
    const cls = String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || '');
    out.push({x: r.left + r.width / 2, y: cy, w: r.width, tag: el.tagName, cls: cls});
  }
  return out;
}"""


def pick_arrows(arrows):
    """(prev, next) from the arrow candidates: icon classes first (fa-angle-left
    / -right), else leftmost / rightmost."""
    left = [a for a in arrows if re.search(r"(angle|chevron|arrow|caret)-left|\bprev", a.get("cls") or "", re.I)]
    right = [a for a in arrows if re.search(r"(angle|chevron|arrow|caret)-right", a.get("cls") or "", re.I)]
    if left and right:
        return left[0], right[0]
    ordered = sorted(arrows, key=lambda a: a["x"])
    if len(ordered) < 2:
        return None, None
    return ordered[0], ordered[-1]

# Event tiles inside a calendar cell: probe a few points; a hit that is not the
# empty cell (it holds an image, a background image or some text other than
# the date label) and sits inside the cell is an event.
CELL_EVENTS_JS = r"""(a) => {
  const c = a.cell, label = a.label;
  const seen = new Set(), out = [];
  const inside = (r) => r.left >= c.left - 4 && r.right <= c.right + 4 && r.top >= c.top - 4 && r.bottom <= c.bottom + 4;
  const xs = [0.35, 0.5, 0.65], ys = [0.4, 0.6, 0.8];
  for (const fx of xs) for (const fy of ys) {
    const px = c.left + (c.right - c.left) * fx, py = c.top + (c.bottom - c.top) * fy;
    let el = document.elementFromPoint(px, py);
    let hit = null;
    for (let k = 0; el && k < 6; k++, el = el.parentElement) {
      const r = el.getBoundingClientRect();
      if (!inside(r)) break;
      const txt = (el.innerText || '').replace(label, '').trim();
      const bg = getComputedStyle(el).backgroundImage;
      if (el.tagName === 'IMG' || el.querySelector('img') || (bg && bg !== 'none') || txt.length > 1) hit = el;
    }
    if (hit && !seen.has(hit)) {
      seen.add(hit);
      const r = hit.getBoundingClientRect();
      out.push({x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height,
                text: (hit.innerText || '').trim().slice(0, 120)});
    }
  }
  // nested hits (tile and its image) are one event
  return out.filter((e, i) => !out.some((o, j) => j !== i && o.w >= e.w && o.h >= e.h &&
      Math.abs(o.x - e.x) < o.w / 2 && Math.abs(o.y - e.y) < o.h / 2 && (o.w > e.w || o.h > e.h)));
}"""

# Text of the venue switcher (top bar, right side), e.g. "LIV Beach".
SWITCHER_JS = r"""() => {
  const W = window.innerWidth, out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length) continue;
    const t = (el.innerText || el.textContent || '').trim();
    if (!t || t.length > 60) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0 || r.top > 60 || r.left < W * 0.6) continue;
    out.push({t: t, x: r.left + r.width / 2, y: r.top + r.height / 2});
  }
  return out;
}"""

# Visible leaf texts anywhere (venue switcher options).
LEAVES_JS = r"""() => {
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length) continue;
    const t = (el.innerText || el.textContent || '').trim();
    if (!t || t.length > 60) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    out.push({t: t, x: r.left + r.width / 2, y: r.top + r.height / 2});
  }
  return out;
}"""


def _wait(page, ms=None):
    page.wait_for_timeout(ms if ms is not None else WAIT_MS)


def _goto(page, url):
    import tao_portal
    tao_portal._goto_promoter(page, url)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001 - load state is only a hint
        pass
    _wait(page)


def _body(page):
    try:
        return page.locator("body").inner_text(timeout=10_000)
    except Exception:  # noqa: BLE001
        return ""


def _visible(locator):
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


def _first_visible(page, selectors):
    for sel in selectors:
        loc = page.locator(sel)
        try:
            for i in range(min(loc.count(), 5)):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:  # noqa: BLE001
            continue
    return None


EMAIL_INPUTS = ("input[type=email]", "input[name*=email i]", "input[id*=email i]",
                "input[name*=user i]", "input[autocomplete=username]")
PASSWORD_INPUTS = ("input[type=password]",)
SUBMIT_BUTTONS = ("button:has-text('Next')", "button:has-text('Continue')", "button:has-text('Log In')",
                  "button:has-text('Login')", "button:has-text('Sign In')", "button[type=submit]",
                  "input[type=submit]")


def on_guestlist(page):
    if GUESTLIST_HOST not in (page.url or ""):
        return False
    if _first_visible(page, PASSWORD_INPUTS):
        return False
    text = _body(page)
    return bool(re.search(r"(" + "|".join(MONTHS) + r")\s+\d{4}", text) or "Add Guest" in text)


def login(page):
    """urvenue.me two-step sign-in. Raises LoginUnavailable / UrvenueError."""
    email, password = credentials()
    if not password:
        raise LoginUnavailable("urvenue session expired and URVENUE_PASSWORD is not set in the worker env")
    if not _first_visible(page, EMAIL_INPUTS + PASSWORD_INPUTS):
        _goto(page, LOGIN_URL)
    if "/dashboard" in (page.url or "") or "/accounts" in (page.url or ""):
        return  # already signed in on urvenue.me
    box = _first_visible(page, EMAIL_INPUTS)
    if box is None and not _first_visible(page, PASSWORD_INPUTS):
        box = _first_visible(page, ("input[type=text]",))
    if box is not None:
        box.fill(email)
        if not _first_visible(page, PASSWORD_INPUTS):
            btn = _first_visible(page, SUBMIT_BUTTONS)
            if btn is None:
                box.press("Enter")
            else:
                btn.click()
            try:
                page.wait_for_selector("input[type=password]", state="visible", timeout=20_000)
            except Exception:  # noqa: BLE001
                raise UrvenueError("urvenue login: password step did not appear")
    pw = _first_visible(page, PASSWORD_INPUTS)
    if pw is None:
        raise UrvenueError("urvenue login: no password field")
    pw.fill(password)
    btn = _first_visible(page, SUBMIT_BUTTONS)
    if btn is None:
        pw.press("Enter")
    else:
        btn.click()
    deadline = time.time() + 40
    while time.time() < deadline:
        _wait(page, 1500)
        url = page.url or ""
        if "/dashboard" in url or "/accounts" in url or GUESTLIST_HOST in url:
            if not _first_visible(page, PASSWORD_INPUTS):
                log.info("urvenue login ok")
                return
        text = _body(page).lower()
        if re.search(r"verification code|two[- ]factor|one[- ]time|captcha|verify it'?s you", text):
            raise UrvenueError("urvenue login asks for a verification step; a person must sign in once")
        if re.search(r"invalid|incorrect|wrong password|not match", text):
            raise UrvenueError("urvenue login rejected the saved credentials")
    raise UrvenueError("urvenue login did not finish")


def open_calendar(page, venue):
    """Open `venue`'s month calendar, signing in if needed, and prove the venue
    switcher shows that venue. Returns (acct, vid)."""
    acct, vid = venue_ids(venue)
    seed = None
    if not vid:
        seed = SWITCH_FROM.get(venue)
        if not seed:
            raise UrvenueError(f"no urvenue venue id for {venue}")
        acct, vid = venue_ids(seed)
    url = calendar_url(acct, vid)
    _goto(page, url)
    if not on_guestlist(page):
        login(page)
        _goto(page, url)
    if not on_guestlist(page):
        # Some sessions only reach the guest-list app from the accounts page.
        _goto(page, ACCOUNTS_URL)
        link = page.locator(f"a[href*='/{acct}/']")
        if _visible(link):
            link.first.click()
            _wait(page)
        _goto(page, url)
    if not on_guestlist(page):
        raise UrvenueError(f"could not open the urvenue guest-list calendar for {venue}")
    ensure_venue(page, venue)
    got = ids_from_url(page.url)
    if got[0] and got[1] and seed:
        remember_venue_id(venue, *got)
    return got if got[0] else (acct, vid)


def switcher_text(page):
    try:
        items = page.evaluate(SWITCHER_JS)
    except Exception:  # noqa: BLE001
        return "", None
    items = [i for i in items if not re.fullmatch(r"[\W\d]*", i["t"])]
    if not items:
        return "", None
    return " ".join(i["t"] for i in items), items[-1]


def ensure_venue(page, venue):
    """The switcher must show `venue`; otherwise pick it from the switcher."""
    text, item = switcher_text(page)
    if label_matches(venue, text):
        return
    if item is None:
        raise UrvenueError(f"urvenue venue switcher not found (wanted {venue})")
    before = {(o["t"], round(o["x"]), round(o["y"])) for o in page.evaluate(LEAVES_JS)}
    page.mouse.click(item["x"], item["y"])
    _wait(page, 1500)
    # Only entries that appeared when the switcher opened (never the sidebar's
    # account name, which can also say "LIV Las Vegas").
    options = [o for o in page.evaluate(LEAVES_JS)
               if (o["t"], round(o["x"]), round(o["y"])) not in before and label_matches(venue, o["t"])]
    if not options:
        page.keyboard.press("Escape")
        raise UrvenueError(f"urvenue venue switcher shows {text!r}; {venue} is not in its list")
    page.mouse.click(options[0]["x"], options[0]["y"])
    _wait(page)
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:  # noqa: BLE001
        pass
    text, _ = switcher_text(page)
    if not label_matches(venue, text):
        raise UrvenueError(f"urvenue venue switcher shows {text!r}, expected {venue}")


def _header(page):
    hdr = page.evaluate(HEADER_JS)
    if not hdr:
        raise UrvenueError("urvenue calendar month header not found")
    name, year = hdr["t"].split()
    return hdr, MONTHS.index(name) + 1, int(year)


def _labels(page):
    hdr, hm, hy = _header(page)
    out = []
    for lab in page.evaluate(LABELS_JS):
        try:
            lab["date"] = date(parse_label_year(lab["mon"], hm, hy), MON3.index(lab["mon"]) + 1, lab["day"])
        except ValueError:
            continue
        out.append(lab)
    return hdr, out


def _settle_calendar(page):
    """Event tiles load after the grid (seen Nov 21 2026: a freshly moved view
    showed only part of its events for a few seconds)."""
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # noqa: BLE001
        pass
    _wait(page)


SCROLL_TO_JS = r"""(a) => {
  let best = null, bd = 1e12;
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length) continue;
    if ((el.innerText || el.textContent || '').trim() !== a.t) continue;
    const r = el.getBoundingClientRect();
    const d = Math.abs(r.left - a.x) + Math.abs(r.top - a.y);
    if (d < bd) { bd = d; best = el; }
  }
  if (best) best.scrollIntoView({block: 'center', inline: 'nearest'});
  return !!best;
}"""

TOP_JS = r"""() => {
  window.scrollTo(0, 0);
  for (const el of document.querySelectorAll('body *')) { if (el.scrollTop > 0) el.scrollTop = 0; }
}"""


def _cell_events(page, night):
    """Events in `night`'s cell. The cell is scrolled into view first:
    elementFromPoint only sees what is on screen (rows 4-5 of the month grid
    are below the fold in a 720px window)."""
    hdr, labels = _labels(page)
    target = next((lab for lab in labels if lab["date"] == night), None)
    if target is None:
        return hdr, labels, None, None
    cell = cell_rect(labels, target)
    height = page.evaluate("() => window.innerHeight")
    if cell["top"] < 0 or cell["bottom"] > height:
        page.evaluate(SCROLL_TO_JS, {"t": target["t"], "x": target["x"], "y": target["y"]})
        _wait(page, 600)
        _, labels = _labels(page)
        target = next((lab for lab in labels if lab["date"] == night), None)
        if target is None:
            return hdr, labels, None, None
    events = page.evaluate(CELL_EVENTS_JS, {"cell": cell_rect(labels, target), "label": target["t"]})
    return hdr, labels, target, events


def _move(page, hdr, labels, forward):
    page.evaluate(TOP_JS)
    _wait(page, 400)
    hdr, labels = _labels(page)
    prev, nxt = pick_arrows(page.evaluate(ARROWS_JS, hdr))
    if prev is None:
        raise UrvenueError("urvenue calendar prev/next arrows not found")
    arrow = nxt if forward else prev
    before = [lab["t"] for lab in labels]
    page.mouse.click(arrow["x"], arrow["y"])
    _wait(page, 1500)
    _settle_calendar(page)
    if [lab["t"] for lab in _labels(page)[1]] == before:
        raise UrvenueError("urvenue calendar did not move")


def find_event_tile(page, night):
    """Move the calendar to `night` and return its event tile (or raise NotPosted)."""
    _settle_calendar(page)
    own_month_tried = False
    for _ in range(16):
        hdr, labels, target, events = _cell_events(page, night)
        if not labels:
            raise UrvenueError("urvenue calendar has no date cells")
        if target is not None:
            header_month = MONTHS.index(hdr["t"].split()[0]) + 1
            if not events and night.month != header_month and not own_month_tried:
                own_month_tried = True
                # Prefer the view of the night's own month: days outside the
                # header month may not carry their events.
                _move(page, hdr, labels, forward=(night.year, night.month) > (int(hdr["t"].split()[1]), header_month))
                continue
            for _poll in range(3):
                if events:
                    break
                _wait(page, 2000)
                hdr, labels, target, events = _cell_events(page, night)
            if not events:
                raise NotPosted(f"no event on {night.isoformat()}")
            if len(events) > 1:
                raise UrvenueError(f"{len(events)} events on {night.isoformat()}; which list is unclear")
            return events[0]
        dates = [lab["date"] for lab in labels]
        _move(page, hdr, labels, forward=night > max(dates))
    raise UrvenueError(f"could not reach {night.isoformat()} on the urvenue calendar")


def open_event(page, venue, night):
    tile = find_event_tile(page, night)
    page.mouse.click(tile["x"], tile["y"])
    want = yymmdd(night)
    deadline = time.time() + 30
    info = {}
    while time.time() < deadline:
        _wait(page, 1500)
        text = _body(page)
        if "Add Guest" in text and "Operation Hours" in text:
            info = parse_event_page(text)
            break
    if not info:
        raise UrvenueError(f"urvenue event page for {night.isoformat()} did not open")
    in_url = re.search(r"/dayview/(\d{6})", page.url or "")
    if in_url and in_url.group(1) != want:
        raise UrvenueError(f"urvenue opened {in_url.group(1)}, not {want}")
    if info.get("date") and info["date"] != night:
        raise UrvenueError(f"urvenue event page shows {info['date']}, not {night}")
    if not in_url and not info.get("date"):
        raise UrvenueError("urvenue event page date could not be read")
    ensure_venue(page, venue)
    status = (info.get("status") or "").lower()
    if status != "open":
        raise NotOpen(info.get("status") or "unknown", info.get("event") or tile.get("text") or "")
    info["tile"] = tile.get("text")
    return info


SEARCH_INPUTS = ("input[placeholder*='search' i]", "input[type=search]")


def list_text(page, query):
    box = _first_visible(page, SEARCH_INPUTS)
    if box is not None:
        box.fill(query)
        _wait(page, 2000)
    text = _body(page)
    if box is not None:
        box.fill("")
        _wait(page, 800)
    return text


def _modal(page):
    for sel in (".modal.show", ".modal.in", ".modal[style*='block']", "[role=dialog]", ".modal-dialog", ".modal"):
        loc = page.locator(sel).filter(has_text="Add Guests")
        try:
            for i in range(loc.count() - 1, -1, -1):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:  # noqa: BLE001
            continue
    return None


SELECTED_JS = r"""(m) => Array.from(m.querySelectorAll('select')).map(s => ({
  value: s.value, text: (s.options[s.selectedIndex] || {}).text || '',
  options: Array.from(s.options).map(o => o.text.trim())}))"""


def _choose_item(page, modal):
    selects = modal.evaluate(SELECTED_JS)
    for i, sel in enumerate(selects):
        if LIST_ITEM in sel["options"]:
            modal.locator("select").nth(i).select_option(label=LIST_ITEM)
            _wait(page, 600)
            return
    if _choose_ng_item(page, modal):
        return
    opener = modal.get_by_text(re.compile(r"Select Item", re.I))
    if not _visible(opener):
        raise UrvenueError("urvenue 'Select Item' dropdown not found")
    try:
        opener.first.click(timeout=5000)
    except Exception:  # noqa: BLE001 - something sits on top: force it
        opener.first.click(force=True, timeout=5000)
    _wait(page, 800)
    if not _pick_option(page, page.get_by_text(LIST_ITEM, exact=True)):
        raise UrvenueError(f"urvenue list item {LIST_ITEM!r} not offered")
    _wait(page, 600)


NG_VALUE_JS = r"""(n) => Array.from(n.querySelectorAll('.ng-value-label, .ng-value'))
  .map(e => e.textContent.trim()).filter(Boolean)"""


def _pick_option(page, option):
    try:
        for i in range(option.count() - 1, -1, -1):
            if option.nth(i).is_visible():
                try:
                    option.nth(i).click(timeout=5000)
                except Exception:  # noqa: BLE001
                    option.nth(i).click(force=True, timeout=5000)
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _choose_ng_item(page, modal):
    """Angular ng-select (the live site): its search <input> overlays the
    '- Select Item -' placeholder and intercepts clicks, so open it through
    .ng-select-container (force if needed), pick General List from the
    dropdown panel and read the chosen value back. False if there is none."""
    ngs = modal.locator(".ng-select")
    if not ngs.count():
        return False
    target = None
    for i in range(ngs.count()):
        n = ngs.nth(i)
        values = n.evaluate(NG_VALUE_JS)
        if LIST_ITEM in values:
            return True                      # already chosen
        if re.search(r"Select Item", n.inner_text() or "", re.I):
            target = n
            break
    if target is None:
        return False
    container = target.locator(".ng-select-container").first
    opened = False
    for how in ({}, {"force": True}):
        try:
            container.click(timeout=5000, **how)
            opened = True
            break
        except Exception:  # noqa: BLE001
            continue
    if not opened:
        try:
            target.locator("input").first.click(force=True, timeout=5000)
        except Exception:  # noqa: BLE001
            raise UrvenueError("urvenue 'Select Item' dropdown would not open") from None
    _wait(page, 600)
    options = page.locator(".ng-dropdown-panel .ng-option").filter(
        has_text=re.compile(rf"^\s*{re.escape(LIST_ITEM)}\s*$"))
    if not options.count():
        try:   # long lists: type to filter
            target.locator("input").first.fill(LIST_ITEM, timeout=3000)
            _wait(page, 600)
        except Exception:  # noqa: BLE001
            pass
    if not _pick_option(page, options):
        raise UrvenueError(f"urvenue list item {LIST_ITEM!r} not offered")
    _wait(page, 600)
    values = target.evaluate(NG_VALUE_JS)
    if LIST_ITEM not in values:
        raise UrvenueError(f"urvenue list item {LIST_ITEM!r} not selected (shows {values[:3]})")
    return True


NO_ITEMS_RE = re.compile(r"no items available", re.I)


def _no_items_popup(page):
    """SweetAlert 'There are no items available' after '+ Add Guest': dismiss
    it and say so. Nothing was opened or submitted."""
    try:
        pop = page.locator(".swal2-popup, .swal2-modal, [role=dialog]").filter(has_text=NO_ITEMS_RE)
        if not pop.count() or not pop.first.is_visible():
            return False
        try:
            pop.first.locator(".swal2-confirm, button").filter(has_text=re.compile(r"^\s*OK\s*$", re.I)).first.click(timeout=3000)
        except Exception:  # noqa: BLE001
            page.keyboard.press("Escape")
        return True
    except Exception:  # noqa: BLE001
        return False


def _check_modal(modal):
    """List item chosen and owner preset, read back from the form."""
    selects = modal.evaluate(SELECTED_JS)
    text = modal.inner_text()
    chosen = [s["text"].strip() for s in selects]
    native_item = any(LIST_ITEM in s["options"] for s in selects)
    native_owner = any(OWNER in s["options"] for s in selects)
    if native_item:
        if LIST_ITEM not in chosen:
            raise UrvenueError(f"urvenue list item {LIST_ITEM!r} not selected")
    elif LIST_ITEM not in text or re.search(r"-\s*Select Item\s*-", text):
        raise UrvenueError(f"urvenue list item {LIST_ITEM!r} not selected")
    if native_owner:
        if OWNER not in chosen:
            raise UrvenueError(f"urvenue owner is not {OWNER}")
    elif OWNER not in text:
        raise UrvenueError(f"urvenue owner is not {OWNER}")


def _shot(page, path):
    if not path:
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=path, full_page=True)
        return path
    except Exception:  # noqa: BLE001
        return None



def app_api_key():
    return os.environ.get("URVENUE_APP_API_KEY", "").strip() or DEFAULT_APP_API_KEY


def api_enabled():
    """API-first unless the operator turned it off. Tests disable it so a dummy
    password cannot hit the live API."""
    flag = os.environ.get("URVENUE_API_DISABLE", "").strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return False
    return has_password()


def _flag_on(name):
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def dedupe_enabled():
    """Cross-job / already-on-list checks. Off unless AMY_DEDUPE or
    AMY_WORKER_DUPLICATE_CHECK is true (Thomas, Nov 22 2026). AMY_DEDUPE
    wins when both are set. Per-job ledger idempotency and
    SubmissionUncertain are separate and always stay on."""
    for name in ("AMY_DEDUPE", "AMY_WORKER_DUPLICATE_CHECK"):
        val = _flag_on(name)
        if val is not None:
            return val
    return False


def _secret_fragments():
    frags = [v for v in (
        os.environ.get("URVENUE_PASSWORD", ""),
        os.environ.get("URVENUE_API_KEY", ""),
        os.environ.get("URVENUE_APP_API_KEY", ""),
        DEFAULT_APP_API_KEY,
        (credentials()[0] or ""),
    ) if v]
    return frags


def _redact(text):
    out = "" if text is None else str(text)
    for frag in _secret_fragments():
        if frag:
            out = out.replace(frag, "<redacted>")
    return out


def _uv_parts(payload):
    """(status, reason, message, data) from an api.urvenue.me JSON body."""
    if not isinstance(payload, dict):
        return None, None, None, None
    uv = payload.get("uv") if isinstance(payload.get("uv"), dict) else {}
    suc, data = uv.get("success"), uv.get("data")
    if isinstance(suc, dict):
        return suc.get("status"), suc.get("reason"), suc.get("message"), data
    return suc, None, None, data


def _norm_person_name(value):
    """Lowercased name with collapsed whitespace, for case/space-insensitive match."""
    return " ".join((value or "").split()).lower()


def find_dayview_row(chats, name, email="", ref=""):
    """The dayview chat for this guest, or None.

    A row is a duplicate only when the guest name matches (case- and
    whitespace-insensitive, on partyname or in original/comment/message)
    or when our job/UV ref or AMY booking comment is on the row. A shared
    email is not enough: a different person can use the same address.
    The email argument is accepted for callers but is not used as a key.
    """
    name_n = _norm_person_name(name)
    ref_l = (ref or "").strip().lower()
    rows = [c for c in (chats or []) if isinstance(c, dict)]

    def text_blob(chat):
        # Name/ref only — never the email field.
        return " ".join(_norm_person_name(chat.get(k)) for k in (
            "partyname", "original", "comment", "message"))

    if name_n:
        for chat in rows:
            if _norm_person_name(chat.get("partyname")) == name_n:
                return chat
        for chat in rows:
            if name_n in text_blob(chat):
                return chat
    if ref_l:
        for chat in rows:
            if ref_l in text_blob(chat):
                return chat
    return None


def dayview_row_text(chat):
    if not isinstance(chat, dict):
        return ""
    expected = chat.get("expected") if isinstance(chat.get("expected"), dict) else {}
    males = expected.get("males", "")
    females = expected.get("females", "")
    return " ".join(str(x) for x in (
        chat.get("status"), chat.get("partyname"), chat.get("itemname"),
        chat.get("ownername"), f"{males}M" if males not in ("", None) else "",
        f"{females}F" if females not in ("", None) else "",
        chat.get("original"),
    ) if x not in ("", None)).strip()[:300]


def pick_list_item(items, prefer=None):
    """Choose the guest-list item to book. Prefer URVENUE_LIST_ITEM (General List)."""
    prefer = (prefer or LIST_ITEM or "").strip().lower()
    rows = [it for it in (items or []) if isinstance(it, dict) and it.get("id")]
    if not rows:
        return None
    for it in rows:
        internal = str(it.get("internalname") or "").strip().lower()
        display = str(it.get("displayname") or "").strip().lower()
        if prefer and (internal == prefer or display == prefer):
            return it
    for it in rows:
        blob = f"{it.get('internalname') or ''} {it.get('displayname') or ''}".lower()
        if "guest" in blob and "list" in blob:
            return it
    return rows[0]


class _ApiClient:
    """Logged-in session against api.urvenue.me. Never logs secrets."""

    def __init__(self):
        self.cookies = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.xsrf = None
        self.userid = None

    def login(self):
        email, password = credentials()
        if not password:
            raise LoginUnavailable("URVENUE_PASSWORD is not set")
        payload = self.post("users/login", {
            "apikey": app_api_key(),
            "email": email,
            "password": password,
        }, base_params=False)
        status, reason, message, data = _uv_parts(payload)
        if status != "success":
            raise LoginUnavailable(_redact(f"urvenue API login failed ({reason or message or status})"))
        if isinstance(data, dict) and data.get("userid"):
            self.userid = str(data["userid"])
        if not self.userid:
            raise LoginUnavailable("urvenue API login did not return a user id")
        return self

    def _common(self, venue, extra=None):
        acct, vid = venue_ids(venue)
        params = {
            "systemid": API_SYSTEM_ID,
            "sourcecode": "internal",
            "sourceloc": acct or "-",
            "appaccountid": acct or "",
            "appuserid": self.userid or "",
            "appvenueid": vid or "",
            "appecozoneid": "0",
            "appprotocol": "https",
            "apphost": GUESTLIST_HOST,
            "appport": "",
            "apppath": "",
            "appsearch": "",
        }
        if extra:
            params.update({k: v for k, v in extra.items() if v is not None})
        return params

    def request(self, method, path, *, params=None, form=None):
        url = f"{API_BASE}/v1/{path}/json/"
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        headers = {
            "User-Agent": "AMY-booking-worker/urvenue-api",
            "Accept": "application/json",
            "Origin": f"https://{GUESTLIST_HOST}",
            "Referer": f"https://{GUESTLIST_HOST}/",
        }
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.xsrf:
            headers["X-Uvapp-Xsrf"] = self.xsrf
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=40) as resp:
                raw = resp.read()
                if resp.headers.get("X-Uv-Xsrf"):
                    self.xsrf = resp.headers.get("X-Uv-Xsrf")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            if exc.headers and exc.headers.get("X-Uv-Xsrf"):
                self.xsrf = exc.headers.get("X-Uv-Xsrf")
            try:
                payload = json.loads(raw.decode("utf-8", "replace") or "null")
            except ValueError:
                payload = None
            _, _, message, data = _uv_parts(payload) if payload else (None, None, None, None)
            msg = message or (data.get("message") if isinstance(data, dict) else None) or exc.reason
            raise UrvenueError(_redact(f"urvenue API {method} {path} HTTP {exc.code}: {msg}"))
        except urllib.error.URLError as exc:
            raise UrvenueError(_redact(f"urvenue API {method} {path} failed: {type(exc.reason).__name__ if not isinstance(exc.reason, str) else 'network error'}"))
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "null")
        except ValueError as exc:
            raise UrvenueError(f"urvenue API {method} {path} returned non-JSON") from exc
        return payload

    def get(self, path, params):
        return self.request("GET", path, params=params)

    def post(self, path, form, base_params=True, venue=None):
        body = dict(form)
        if base_params:
            if venue is None:
                raise UrvenueError("urvenue API POST requires a venue")
            merged = self._common(venue)
            merged.update(body)
            body = merged
        else:
            body.setdefault("sourcecode", "internal")
            body.setdefault("sourceloc", "-")
            body.setdefault("systemid", API_SYSTEM_ID)
            body.setdefault("appprotocol", "https")
            body.setdefault("apphost", GUESTLIST_HOST)
        return self.request("POST", path, form=body)


def add_guest_api(venue, night, guest, *, ref, rehearse=False, screenshot_path=None, before_submit=None):
    """Add the party through the UrGuestlist API. Raises the same errors as the
    browser path. Never logs the password or API keys."""
    line, name = booking_line(guest, ref)
    email = (guest.get("email") or "").strip()
    acct, vid = venue_ids(venue)
    if not vid:
        raise UrvenueError(f"no urvenue venue id for {venue}")
    caldate = yymmdd(night)
    client = _ApiClient()
    client.login()
    common = client._common(venue, {"venueid": vid, "caldate": caldate, "accountid": acct})
    view = client.get("guestlist/dayview", common)
    status, _, message, data = _uv_parts(view)
    if status != "success" or not isinstance(data, dict):
        raise UrvenueError(_redact(f"urvenue dayview failed ({message or status})"))
    chats = data.get("chats") if isinstance(data.get("chats"), list) else []
    existing = find_dayview_row(chats, name, email, ref)
    if existing and dedupe_enabled():
        raise AlreadyListed(dayview_row_text(existing))
    items_payload = client.get("guestlist/dayitem", {**common, "ownerid": client.userid, "status": "7"})
    istatus, _, imessage, idata = _uv_parts(items_payload)
    if istatus != "success":
        raise UrvenueError(_redact(f"urvenue dayitem failed ({imessage or istatus})"))
    items = idata.get("list") if isinstance(idata, dict) else None
    item = pick_list_item(items)
    if not item:
        raise NotOpen("no guest list items", "")
    if rehearse:
        return {"rehearsal": True, "screenshot": None, "event": None, "page_url": calendar_url(acct, vid) + f"/{caldate}/0",
                "line_fields": ["name", "email", "male", "female", "comment"], "via": "api",
                "list_item": item.get("internalname") or item.get("displayname")}
    if before_submit:
        before_submit()
    try:
        result = client.post("guestlist/mylist", {
            "ownerid": client.userid,
            "venueid": vid,
            "itemid": str(item["id"]),
            "caldate": caldate,
            "guests": line,
            "inviteid": "",
        }, venue=venue)
    except UrvenueError as exc:
        # The POST may or may not have reached urvenue. Do not also try the
        # browser: a second write could double-book.
        raise SubmissionUncertain(_redact(str(exc))) from exc
    rstatus, _, rmessage, rdata = _uv_parts(result)
    saved = None
    if isinstance(rdata, dict):
        saved = rdata.get("saved")
    if rstatus != "success":
        raise SubmissionUncertain(_redact(f"urvenue API add failed ({rmessage or rstatus})"))
    try:
        saved_n = int(saved) if saved is not None and str(saved) != "" else None
    except (TypeError, ValueError):
        saved_n = None
    if saved_n == 0:
        errors = rdata.get("errors") if isinstance(rdata, dict) else None
        raise SubmissionUncertain(_redact(f"urvenue API reported no saved guests ({errors or rmessage or 'saved=0'})"))
    try:
        again = client.get("guestlist/dayview", common)
        _, _, _, adata = _uv_parts(again)
        chats = adata.get("chats") if isinstance(adata, dict) and isinstance(adata.get("chats"), list) else []
        row_chat = find_dayview_row(chats, name, email, ref)
        row = dayview_row_text(row_chat) if row_chat else None
    except Exception as exc:  # noqa: BLE001 - after the write: never resubmit
        raise SubmissionUncertain(f"could not re-read the list after API add ({type(exc).__name__})")
    if not row:
        raise SubmissionUncertain("posted the guest via API once but the row is not on dayview")
    return {"verified": True, "confirmation_id": confirmation_ref(ref, venue, night.isoformat()),
            "row": row, "screenshot": None, "event": None, "event_time": None,
            "page_url": calendar_url(acct, vid) + f"/{caldate}/0",
            "list_item": item.get("internalname") or LIST_ITEM, "owner": OWNER, "via": "api"}


def add_guest(venue, night, guest, *, ref, rehearse=False, screenshot_path=None, before_submit=None):
    """Sign `guest` up on `venue`'s urvenue list for `night` (a date).
    Tries the UrGuestlist API first, then the browser. rehearse=True stops
    before saving. SubmissionUncertain is never retried on the other path
    (no blind resubmit). AlreadyListed (only raised when dedupe is on) is
    also not retried on the other path."""
    if venue not in VENUES:
        raise UrvenueError(f"{venue} is not a urvenue venue")
    booking_line(guest, ref)  # validate early, before either path
    if api_enabled():
        try:
            return add_guest_api(venue, night, guest, ref=ref, rehearse=rehearse,
                                 screenshot_path=screenshot_path, before_submit=before_submit)
        except (AlreadyListed, SubmissionUncertain, NotPosted, NotOpen, LoginUnavailable):
            raise
        except UrvenueError as exc:
            log.warning("urvenue API path failed (%s); falling back to browser", _redact(str(exc)))
        except Exception as exc:  # noqa: BLE001 - unexpected pre-write failure
            log.warning("urvenue API path failed (%s); falling back to browser", type(exc).__name__)
    return add_guest_browser(venue, night, guest, ref=ref, rehearse=rehearse,
                             screenshot_path=screenshot_path, before_submit=before_submit)


def add_guest_browser(venue, night, guest, *, ref, rehearse=False, screenshot_path=None, before_submit=None):
    """Sign `guest` up on `venue`'s urvenue list for `night` (a date).
    rehearse=True stops with the form filled and closes it (nothing saved)."""
    line, name = booking_line(guest, ref)
    email = (guest.get("email") or "").strip()
    from playwright.sync_api import sync_playwright
    import tao_portal
    with sync_playwright() as p:
        closable, page = tao_portal._browser_page(p)
        page.set_default_timeout(20_000)
        try:
            open_calendar(page, venue)
            info = open_event(page, venue, night)
            existing = find_row(list_text(page, name), name, email)
            if existing and dedupe_enabled():
                raise AlreadyListed(existing)
            add = page.get_by_text(re.compile(r"^\s*\+?\s*Add Guest\s*$"))
            btn = None
            for i in range(add.count()):
                if add.nth(i).is_visible():
                    btn = add.nth(i)
                    break
            if btn is None:
                raise UrvenueError("urvenue '+ Add Guest' button not found")
            btn.click()
            _wait(page, 1500)
            modal = _modal(page)
            if modal is None:
                if _no_items_popup(page):
                    # "Oops.. There are no items available" (e.g. LIV NYE, Nov 21
                    # 2026): this night has no guest list to add to = closed.
                    raise NotOpen("no guest list items", info.get("event") or "")
                raise UrvenueError("urvenue 'Add Guests' form did not open")
            _choose_item(page, modal)
            box = modal.locator("textarea")
            if not _visible(box):
                raise UrvenueError("urvenue 'Add Guests' text box not found")
            box.first.fill(line)
            _wait(page, 500)
            if box.first.input_value().strip() != line:
                raise UrvenueError("urvenue text box did not keep the booking line")
            _check_modal(modal)
            submit = modal.get_by_role("button", name=re.compile(r"^\s*Add Guests?\s*$", re.I))
            if submit.count() != 1 or not submit.first.is_visible():
                raise UrvenueError("urvenue 'Add Guests' button not found (or not unique)")
            if rehearse:
                shot = _shot(page, screenshot_path)
                close = modal.locator("button.close, [aria-label=Close], .close")
                if _visible(close):
                    close.first.click()
                else:
                    page.keyboard.press("Escape")
                return {"rehearsal": True, "screenshot": shot, "event": info.get("event"),
                        "page_url": page.url, "line_fields": ["name", "email", "male", "female", "comment"]}
            if before_submit:
                before_submit()
            submit.first.click()
            try:
                _wait(page, 3000)
                for _ in range(10):
                    if _modal(page) is None:
                        break
                    _wait(page, 1000)
                still_open = _modal(page) is not None
                row = find_row(list_text(page, name), name, email)
                if not row:
                    _goto(page, page.url)
                    row = find_row(list_text(page, name), name, email)
                shot = _shot(page, screenshot_path)
            except Exception as exc:  # noqa: BLE001 - after the click: never resubmit
                raise SubmissionUncertain(f"could not read the list after Add Guests ({type(exc).__name__})")
            if not row:
                raise SubmissionUncertain("clicked Add Guests once but the guest's row is not visible"
                                          + (" (form still open)" if still_open else ""))
            return {"verified": True, "confirmation_id": confirmation_ref(ref, venue, night.isoformat()),
                    "row": row, "screenshot": shot, "event": info.get("event"),
                    "event_time": info.get("event_time"), "page_url": page.url,
                    "list_item": LIST_ITEM, "owner": OWNER}
        finally:
            try:
                closable.close()
            except Exception:  # noqa: BLE001
                pass


def probe(venue, night=None):
    """Read-only check: sign in, open the venue calendar and (with a night) the
    event page. Never clicks '+ Add Guest'. No guest data, no secrets in the
    result."""
    from playwright.sync_api import sync_playwright
    import tao_portal
    out = {"venue": venue, "night": night.isoformat() if night else None}
    with sync_playwright() as p:
        closable, page = tao_portal._browser_page(p)
        page.set_default_timeout(20_000)
        try:
            out["ids"] = list(open_calendar(page, venue))
            out["switcher"] = switcher_text(page)[0]
            hdr, labels = _labels(page)
            out["calendar"] = {"header": hdr["t"], "first": str(labels[0]["date"]) if labels else None,
                               "last": str(labels[-1]["date"]) if labels else None}
            if night:
                try:
                    info = open_event(page, venue, night)
                    out["event"] = {k: (str(v) if isinstance(v, date) else v) for k, v in info.items()}
                    out["add_guest_button"] = _visible(page.get_by_text(re.compile(r"^\s*\+?\s*Add Guest\s*$")))
                except NotPosted:
                    out["event"] = "not posted"
                except NotOpen as exc:
                    out["event"] = f"not open: {exc.status}"
        except UrvenueError as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                closable.close()
            except Exception:  # noqa: BLE001
                pass
    return out
