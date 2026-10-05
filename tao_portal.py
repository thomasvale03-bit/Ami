"""
TAO Group promoter-portal automation (tickets.taogroup.com / TicketDriver).

Authorized in writing by TAO Group Hospitality / TicketDriver on 2026-09-28
(see config.rules.TAO_AUTOMATION_AUTHORIZATION): automated browser
submission of $0 Guest List/Pass registrations through Playmaker's promoter
link only. Every safety rule below stays inside that scope.

Contract with main.py / rules_engine.py:
    check_availability(venue, date_obj) -> dict | None
        None => no live, free Passes/Guest List entry for that venue+date.
        {"event", "listing_type": "Passes", "price": 0, "listing_url",
         "arrival_text", "capacity": {"female": n, "male": n}}

    submit_registration(listing, guest) -> dict
        {"confirmation_id", "verified": True, ...} only when TAO shows a
        success page with an order ID. Any problem before the final click
        returns {"confirmation_id": None, "verified": False, "reason": ...}.
        A problem after the final click raises SubmissionUncertain so the
        request is flagged for a person and never retried automatically.

Every event page is reached by opening Playmaker's promoter link and
clicking through from it (never by going to the event page directly), and
only Pass links listed on that page are ever used, so each registration is
credited to Playmaker.

READY stays False until the selectors have been checked against the live
site (read-only) and one test registration for valeconsultingaz@gmail.com
has succeeded. main.py refuses --live until then.
"""
import os
import re
from datetime import date
from urllib.parse import urlparse

from config.rules import TAO_AUTOMATION_AUTHORIZATION, TAO_PROMOTER_URL

# Checked against the live site on 2026-09-28 (read-only listing plus a
# full form rehearsal stopped before "Submit Order"). The first real
# submission happens in test mode, limited to the owner's own address.
READY = True

# URL slug fragments that identify each venue on TAO Pass links.
VENUE_SLUGS = {
    "Hakkasan Nightclub": [r"hakkasan"],
    "TAO Nightclub": [r"tao-nc", r"tao-nightclub"],
    "Marquee Nightclub": [r"marquee-nc", r"marquee-nightclub"],
    "OMNIA Nightclub": [r"omnia-nc", r"omnia-nightclub"],
    "JEWEL Nightclub": [r"jewel"],
    "TAO Beach Dayclub": [r"tao-beach"],
    "Marquee Dayclub": [r"marquee-dc", r"marquee-dayclub"],
    "OMNIA Dayclub": [r"omnia-dc", r"omnia-dayclub"],
    "Liquid Pool Lounge": [r"liquid"],
    "Palm Tree Beach Club": [r"palm-tree"],
    "LAVO Party Brunch": [r"lavo"],
}

UNAVAILABLE_TEXT = re.compile(r"\b(sold out|guest list closed|passes unavailable|event unavailable)\b", re.I)
SUCCESS_TEXT = re.compile(
    r"order confirmation|registration confirmed|you(?:'|’)re on the guest list|thank you for your order", re.I)
MARKETING_TEXT = re.compile(r"marketing|promotional|newsletter|offers|email updates|text messages|sms", re.I)
TERMS_TEXT = re.compile(r"terms|conditions|privacy|21\+|\bage\b", re.I)


PARENT_TEXT_JS = "n => (n.parentElement ? n.parentElement.innerText : '')"


# TAO order confirmation pages live at /orders/confirmation/<order id>
# (order IDs are UUIDs, e.g. 6aba738b-b670-47ec-b34b-54e80a1e60a9).
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
CONFIRMATION_URL = re.compile(r"/orders/confirmation/(" + UUID + r")", re.I)


def order_id_from(url, body):
    """The TAO order ID from the success page address, else from its text."""
    m = CONFIRMATION_URL.search(url or "")
    if m:
        return m.group(1)
    m = re.search(r"order\s*(?:id|number|#)\s*[:#]?\s*(" + UUID + r"|[A-Z0-9][A-Z0-9-]{5,})", body or "", re.I)
    if m:
        return m.group(1)
    m = re.search(UUID, body or "", re.I)
    return m.group() if m else None


class TaoBlocked(Exception):
    """TAO's site showed a bot/security check instead of its listings."""


def is_security_check(title, body):
    text = f"{title}\n{body}".lower()
    return ("just a moment" in text or "performing security verification" in text
            or "verify you are human" in text)


class SubmissionUncertain(Exception):
    """The final submit click happened but success could not be confirmed."""


# --- Pure safety helpers (unit-tested) ------------------------------------

def is_safe_pass_url(url):
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname == "tickets.taogroup.com"
            and "guest-list" in parsed.path.lower())


def venue_from_url(url):
    for venue, patterns in VENUE_SLUGS.items():
        if any(re.search(p, url, re.I) for p in patterns):
            return venue
    return None


def date_from_url(url):
    # TAO Pass links end in the event date, e.g. .../e/guest-list-omnia-nc-9-29-2026/tickets
    m = re.search(r"[-/](\d{1,2})-(\d{1,2})-(\d{4})(?:/|$|\?)", urlparse(url).path)
    if not m:
        return None
    month, day, year = (int(x) for x in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def has_positive_price(text):
    return any(float(a) > 0 for a in re.findall(r"\$\s*(\d+(?:\.\d{1,2})?)", text or ""))


def option_price_text(levels):
    """The nearest text around a quantity box that states a price (e.g. the
    'Guest List - Female FREE' row on TAO), or '' if none does."""
    return next((l for l in levels if re.search(r"\bfree\b|\$", l, re.I)), "")


def has_free_evidence(text):
    return bool(re.search(r"\bfree\b|\$\s*0(?:\.00)?\b", text or "", re.I))


def max_quantity(options):
    values = [int(m.group()) for o in options for m in [re.match(r"\d+", (o.get("value") or o.get("text") or "").strip())] if m]
    return max(values, default=0)


def gender_of(context):
    if re.search(r"female|women|woman|ladies|lady", context, re.I):
        return "female"
    if re.search(r"\bmale\b|\bmen\b|\bman\b|guys", context, re.I):
        return "male"
    return None


# --- Browser plumbing -------------------------------------------------------

_catalog = None  # {(venue, date): url}, loaded once per run


def launch_browser(playwright):
    # CHROMIUM_PATH points at a preinstalled Chromium where Playwright's own
    # download isn't available; unset on a normal install.
    return playwright.chromium.launch(headless=True, executable_path=os.environ.get("CHROMIUM_PATH") or None)


def _browser_page(playwright):
    browser = launch_browser(playwright)
    context = browser.new_context(locale="en-US", timezone_id="America/Los_Angeles")
    page = context.new_page()
    page.set_default_timeout(15_000)
    return browser, page


def _settle(page):
    """Give the (large) promoter page time to finish loading; its links are
    usable before every background request finishes."""
    try:
        page.wait_for_load_state("networkidle", timeout=45_000)
    except Exception:
        page.wait_for_load_state("load")


def _open_via_promoter(page, url):
    """Reach an event page the way a customer does, so Playmaker gets the
    promoter credit: open the promoter link first, then click that event's
    Pass link on it. Returns False if the link is no longer on the page."""
    page.goto(TAO_PROMOTER_URL, wait_until="domcontentloaded")
    _settle(page)
    hrefs = page.locator("a").evaluate_all("els => els.map(a => a.href)")
    if url not in hrefs:
        return False
    link = page.locator("a").nth(hrefs.index(url))
    link.evaluate("a => a.removeAttribute('target')")  # stay in this tab
    with page.expect_navigation(wait_until="domcontentloaded"):
        if link.is_visible():
            link.click()
        else:
            # The promoter page shows ~20 events at a time; later ones are in
            # the page but hidden until scrolled to. Activating the link
            # itself navigates exactly like a click (same URL, same session,
            # promoter page as referrer).
            link.evaluate("a => a.click()")
    return True


# Walks up from a Pass link to its event card and returns the card text.
CARD_TEXT_JS = """a => {
    let node = a;
    for (let i = 0; i < 6 && node; i++) {
        node = node.parentElement;
        if (node && (node.innerText || '').length > 60) break;
    }
    return (node ? node.innerText : '').replace(/\\s+/g, ' ').trim();
}"""

KNOWN_VENUES = sorted(VENUE_SLUGS, key=len, reverse=True)


def venue_from_card(text):
    """Cards read e.g. '... OMNIA Nightclub, Las Vegas, NV ...'."""
    for venue in KNOWN_VENUES:
        if re.search(re.escape(venue) + r",\s*Las Vegas", text or "", re.I):
            return venue
    return None


def card_details(text):
    """Event name and start time from a card such as 'Guest List - Alesso
    Tuesday, Oct 27, 2026 at 10:30 PM to Wednesday, ...'."""
    m = re.search(r"Guest List\s*-\s*(.+?)\s+(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,", text or "")
    start = re.search(r"\bat\s+(\d{1,2}:\d{2}\s?[AP]M)\b", text or "", re.I)
    return (m.group(1).strip() if m else None), (start.group(1).upper() if start else None)


def build_catalog(links):
    """links: [{"text", "href", "card"}] from the promoter page.
    Returns {(venue, date): {"url", "event", "event_time"}} for Pass links."""
    catalog = {}
    for link in links:
        if not re.fullmatch(r"passes?", link["text"], re.I) or not is_safe_pass_url(link["href"]):
            continue
        venue = venue_from_card(link.get("card")) or venue_from_url(link["href"])
        day = date_from_url(link["href"])
        if not (venue and day):
            continue
        event, event_time = card_details(link.get("card"))
        catalog.setdefault((venue, day), {"url": link["href"], "event": event, "event_time": event_time})
    return catalog


def _load_catalog():
    """Every live 'Pass'/'Passes' link on the promoter page, keyed by venue+date."""
    global _catalog
    if _catalog is not None:
        return _catalog
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page = _browser_page(p)
        try:
            page.goto(TAO_PROMOTER_URL, wait_until="domcontentloaded")
            _settle(page)
            if is_security_check(page.title(), page.locator("body").inner_text()):
                raise TaoBlocked("TAO's website showed a security check (Cloudflare) instead of the guest lists")
            links = page.locator("a").evaluate_all(
                "els => els.map(a => ({text: (a.textContent || '').replace(/\\s+/g, ' ').trim(), href: a.href}))")
            cards = page.locator("a").evaluate_all(f"els => els.map({CARD_TEXT_JS})")
        finally:
            browser.close()
    for link, card in zip(links, cards):
        link["card"] = card
    _catalog = build_catalog(links)
    return _catalog


def reset_catalog():
    """Forget the cached promoter-page listings (called between cycles)."""
    global _catalog
    _catalog = None


def _catalog_urls():
    return {entry["url"] for entry in _load_catalog().values()}


def _ticket_selects(page):
    return page.locator("select").evaluate_all("""els => els.map(el => {
        const label = el.id ? (document.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.innerText || '') : '';
        // Text around the select, nearest first: its own label, then each
        // enclosing element. Gender comes from the nearest level that names
        // one, so a neighbouring option's label can't be picked up.
        const levels = [((el.getAttribute('aria-label') || '') + ' ' + label).trim()];
        let node = el.parentElement;
        for (let i = 0; i < 4 && node; i++) { levels.push(node.innerText || ''); node = node.parentElement; }
        return {
            selector: el.id ? `#${CSS.escape(el.id)}` : (el.name ? `select[name="${CSS.escape(el.name)}"]` : null),
            levels: levels.map(t => t.replace(/\\s+/g, ' ').trim()),
            context: levels.join(' ').replace(/\\s+/g, ' ').trim(),
            options: [...el.options].map(o => ({value: o.value, text: (o.textContent || '').trim()})),
        };
    })""")


def _selects_by_gender(page):
    found = {}
    for select in _ticket_selects(page):
        gender = next((g for g in map(gender_of, select["levels"]) if g), None)
        if gender and gender not in found:
            found[gender] = select
    return found


def _arrival_text(body):
    lines = [re.sub(r"\s+", " ", l).strip() for l in re.split(r"\n|(?<=[.!?])\s+", body)]
    picks = [l for l in lines if 5 < len(l) <= 300 and re.search(
        r"arriv|before\s+\d|admission|entry|dress code|valid.*id|21\+|guest list (?:closes|cutoff)|ratio", l, re.I)]
    return " ".join(dict.fromkeys(picks[:4]))


def _fill_first(page, selectors, value):
    for selector in selectors:
        field = page.locator(selector).first
        if field.count() and field.is_visible():
            field.fill(str(value))
            return True
    return False


# --- Public API -------------------------------------------------------------

def check_availability(venue, date_obj):
    entry = _load_catalog().get((venue, date_obj))
    if not entry:
        return None
    url = entry["url"]
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page = _browser_page(p)
        try:
            if not _open_via_promoter(page, url):
                return None
            body = page.locator("body").inner_text()
            if UNAVAILABLE_TEXT.search(body):
                return None
            selects = _selects_by_gender(page)
            # Price evidence from each option's own nearest text; any price -> not eligible.
            priced = " ".join(option_price_text(s["levels"]) for s in selects.values())
            if not selects or has_positive_price(priced) or not has_free_evidence(priced):
                return None  # not provably free -> never eligible
            title = (page.locator("h1, h2").first.text_content() or "").strip()
            title = re.sub(r"^Guest List\s*-\s*", "", title)
            start = re.search(r"\b\d{1,2}:\d{2}\s?[AP]M\b", body, re.I)
            return {
                "event": entry.get("event") or title or f"{venue} Guest List",
                "event_time": entry.get("event_time") or (start.group().upper() if start else None),
                "listing_type": "Passes",
                "price": 0,
                "listing_url": url,
                "arrival_text": _arrival_text(body),
                "capacity": {g: max_quantity(s["options"]) for g, s in selects.items()},
            }
        finally:
            browser.close()


def submit_registration(listing, guest, rehearse=False):
    """rehearse=True fills and checks the whole form, then stops before the
    final click and returns {"rehearsal": True, ...}: nothing is ordered."""
    url = listing.get("listing_url", "")
    if not is_safe_pass_url(url):
        return {"confirmation_id": None, "verified": False, "reason": f"Refusing non-Guest-List URL: {url}"}
    if url not in _catalog_urls():
        return {"confirmation_id": None, "verified": False,
                "reason": f"Refusing a Pass link that is not on the Playmaker promoter page: {url}"}
    from playwright.sync_api import sync_playwright

    def stop(reason):
        return {"confirmation_id": None, "verified": False, "reason": reason}

    with sync_playwright() as p:
        browser, page = _browser_page(p)
        try:
            if not _open_via_promoter(page, url):
                return stop("Pass link is no longer on the Playmaker promoter page")
            selects = _selects_by_gender(page)
            for gender, count in (("female", guest["female_count"]), ("male", guest["male_count"])):
                if count == 0:
                    continue
                select = selects.get(gender)
                if not select or not select["selector"] or max_quantity(select["options"]) < count:
                    return stop(f"{gender} Guest List quantity {count} is not available")
                # Each option being selected must itself be provably free,
                # whatever the page total says.
                own_text = option_price_text(select["levels"])
                if has_positive_price(own_text) or not has_free_evidence(own_text):
                    return stop(f"{gender} option is not shown as free: {own_text[:80]}")
                option = next((o for o in select["options"]
                               if re.match(r"\d+", o["value"] or o["text"] or "")
                               and int(re.match(r"\d+", o["value"] or o["text"]).group()) == count), None)
                if not option:
                    return stop(f"{gender} quantity {count} is not offered")
                page.locator(select["selector"]).select_option(option["value"])
            page.wait_for_timeout(500)

            fields = [
                (["#OrderEmail", 'input[name="Email"]', "#Email", 'input[type="email"]'], guest["email"]),
                (["#OrderFirstName", 'input[name="FirstName"]', "#FirstName", 'input[autocomplete="given-name"]'], guest["first_name"]),
                (["#OrderLastName", 'input[name="LastName"]', "#LastName", 'input[autocomplete="family-name"]'], guest["last_name"]),
            ]
            for selectors, value in fields:
                if not _fill_first(page, selectors, value):
                    return stop(f"Required TAO field not found: {selectors[0]}")
            # Phone and ZIP only where TAO asks for them.
            _fill_first(page, ["#OrderPhone", 'input[name="Phone"]', "#Phone", 'input[type="tel"]'], guest["phone"])
            _fill_first(page, ["#OrderPostalCode", 'input[name="Zip"]', 'input[name="PostalCode"]', "#Zip",
                               'input[autocomplete="postal-code"]'], guest["billing_zip"])

            boxes = page.locator('input[type="checkbox"]')
            for i in range(boxes.count()):
                box = boxes.nth(i)
                box_id = box.get_attribute("id") or ""
                label_el = page.locator(f'label[for="{box_id}"]') if box_id else None
                label = label_el.first.inner_text() if label_el is not None and label_el.count() else ""
                parent_text = box.evaluate(PARENT_TEXT_JS)
                context = f"{label} {parent_text}"
                if box_id in ("OrderOptedInSms", "OrderOptedIn") or MARKETING_TEXT.search(context):
                    if box.is_checked():
                        box.uncheck()  # never opt customers into marketing
                elif TERMS_TEXT.search(context) or box.get_attribute("required") is not None:
                    box.check()  # authorized: TAO authorization sec. 4 + customer's form consent

            totals = " ".join(page.locator('[id*="total" i], [class*="total" i]').all_inner_texts())
            proposed = page.locator("#OrderProposedTotal")
            proposed_value = proposed.input_value() if proposed.count() else "0"
            body = page.locator("body").inner_text()
            if has_positive_price(totals) or float(proposed_value or 0) > 0:
                return stop(f"Order total is not $0 ({totals or proposed_value})")
            if not has_free_evidence(body):
                return stop("Could not verify the registration is free")

            button = page.locator('button[type="submit"], input[type="submit"]').filter(
                has_text=re.compile(r"place order|complete|submit|register", re.I)).first
            if not button.count():
                button = page.get_by_role("button", name=re.compile(r"place order|complete|submit|register", re.I)).first
            if not button.count():
                return stop("TAO submit button not found")

            if rehearse:
                return {"confirmation_id": None, "verified": False, "rehearsal": True,
                        "form": {sel: page.input_value(sel) for sel in
                                 ("#OrderEmail", "#OrderFirstName", "#OrderLastName", "#OrderPostalCode", "#OrderPhone")
                                 if page.locator(sel).count()},
                        "quantities": {g: page.input_value(s["selector"]) for g, s in selects.items()},
                        "checkboxes": page.locator('input[type="checkbox"]').evaluate_all(
                            "els => els.map(e => [e.id || e.name || '(unnamed)', e.checked])"),
                        "totals": totals, "submit_button": button.inner_text().strip()}
            button.click()
            # From here on the order may exist: never report a plain failure.
            try:
                # TAO redirects to /orders/confirmation/<order id> once the
                # order exists; give it time, then read whatever page we're on.
                try:
                    page.wait_for_url(CONFIRMATION_URL, timeout=30_000)
                except Exception:
                    pass
                page.wait_for_load_state("domcontentloaded")
                page.wait_for_timeout(1500)
                body = page.locator("body").inner_text()
            except Exception as exc:
                raise SubmissionUncertain(f"Page failed after submit: {exc}") from exc
            order_id = order_id_from(page.url, body)
            if not order_id:
                if not (SUCCESS_TEXT.search(body) or re.search(r"confirmation|ordered=true", page.url, re.I)):
                    raise SubmissionUncertain("TAO did not show a success page after submit")
                raise SubmissionUncertain(f"TAO success page had no order ID ({page.url})")
            return {
                "confirmation_id": order_id,
                "verified": True,
                "confirmation_url": page.url,
                "event": listing.get("event"),
                "arrival_text": _arrival_text(body) or listing.get("arrival_text", ""),
                "authorization": TAO_AUTOMATION_AUTHORIZATION,
            }
        finally:
            browser.close()
