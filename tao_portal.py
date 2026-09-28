"""
TAO Group promoter-portal automation.

IMPORTANT — this file is a scaffold, not a finished integration. The actual
CSS selectors / element structure of tickets.taogroup.com must be captured
by recording a real session (e.g. `playwright codegen <promoter_url>`) and
then filled in below. This cannot be done reliably from a text-only chat
session, so treat every `TODO(selectors)` as a required step before this
runs against production.

Design contract with rules_engine.py:
    check_availability(venue, date_obj) -> dict | None
        None                => no live Passes/Guest List entry for that venue+date
        {"event": ..., "listing_type": "Passes"|"Tickets",
         "price": 0,   # price per pass as shown on TAO; only 0 is ever booked
         "female_cutoff": ..., "male_cutoff": ..., "listing_url": ...}

    submit_registration(event_listing, guest) -> dict
        Returns {"confirmation_id": ..., "verified": bool}. Must abort without
        submitting if the checkout shows any total other than $0. Never fabricate
        a confirmation_id — if the success page/response can't be parsed,
        return {"confirmation_id": None, "verified": False}.
"""
from config.rules import TAO_PROMOTER_URL

# Set to True only once check_availability() and submit_registration() are
# filled in from a recorded TAO session. main.py refuses --live until then.
READY = False


def check_availability(venue, date_obj):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(TAO_PROMOTER_URL)

        # TODO(selectors): filter/search the event list for `venue` on `date_obj`.
        # The promoter page lists events by night; each has a "Passes" or a
        # sold-out/code-gated state. Example shape once selectors are known:
        #
        # page.fill('[data-testid="venue-filter"]', venue)
        # listing = page.locator(f'text="{venue}"').first
        # if listing.locator('text="Passes"').count() == 0:
        #     browser.close()
        #     return None  # sold out or requires a code -> not available
        #
        # event_name = listing.locator('.event-title').inner_text()
        # cutoff_text = listing.locator('.cutoff').inner_text()

        browser.close()
        raise NotImplementedError(
            "Fill in the selectors above after recording a real session "
            "with `playwright codegen` against the promoter URL."
        )


def submit_registration(event_listing, guest):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(event_listing["listing_url"])

        # TODO(selectors): fill the checkout form.
        # page.fill('[name="firstName"]', guest["first_name"])
        # page.fill('[name="lastName"]', guest["last_name"])
        # page.fill('[name="email"]', guest["email"])
        # page.fill('[name="phone"]', guest["phone"])
        # page.fill('[name="zip"]', guest["billing_zip"])
        # page.check('[name="agreeToTerms"]')  # POA-authorized acceptance
        # page.click('button[type="submit"]')
        #
        # confirmation_id = page.locator('.confirmation-id').inner_text()
        # browser.close()
        # return {"confirmation_id": confirmation_id, "verified": True}

        browser.close()
        raise NotImplementedError(
            "Fill in the selectors above after recording a real session."
        )
