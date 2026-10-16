#!/usr/bin/env python3
"""Assisted TAO guest-list sign-up — run on Thomas's own computer.

    python tools/assisted_signup.py --job <token from Amy's email>
    python tools/assisted_signup.py --file job.json
    python tools/assisted_signup.py --url URL --first Ana --last Ruiz \\
        --email ana@x.com --phone 7025550100 --female 2 --male 1

What it does:
  1. Opens a normal, visible Chrome window (your installed Chrome, with its
     own saved profile in ~/.amy-signup-profile).
  2. If TAO's Cloudflare "verify you are human" check is showing, it prints
     "Click the human check" and simply waits (up to 3 minutes) for YOU to
     clear it. It never touches, solves, or disguises anything about that
     check — no stealth, no solvers, no fingerprint changes.
  3. Once the real form is showing, it picks the party counts, fills name /
     email / phone / ZIP, unticks marketing boxes, ticks required terms,
     checks the total is $0, and clicks submit (or asks first with --confirm).
  4. Reports the order id + saves a screenshot in ./signup-results/.
  If anything can't be found, it stops BEFORE submitting and leaves the
  window open so you can finish by hand.

Selectors live in SELECTORS below — edit there if TAO changes its form.
"""
import argparse
import json
import os
import pathlib
import re
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import assisted_jobs  # noqa: E402
import tao_portal  # noqa: E402

CHALLENGE_TIMEOUT_S = 180
PROFILE_DIR = os.environ.get("AMY_SIGNUP_PROFILE", str(pathlib.Path.home() / ".amy-signup-profile"))
RESULTS_DIR = pathlib.Path(os.environ.get("AMY_SIGNUP_RESULTS", "signup-results"))

# One place for every form guess. Each field: CSS selectors tried in order,
# then label regexes (get_by_label). The #Order* ids match what Amy's
# rehearsal saw on 2026-09-28; label/name fallbacks are guesses.
SELECTORS = {
    "email": {"css": ["#OrderEmail", 'input[name="Email"]', "#Email", 'input[type="email"]'],
              "labels": [r"e-?mail"], "required": True},
    "first_name": {"css": ["#OrderFirstName", 'input[name="FirstName"]', "#FirstName",
                           'input[autocomplete="given-name"]'], "labels": [r"first\s*name"], "required": True},
    "last_name": {"css": ["#OrderLastName", 'input[name="LastName"]', "#LastName",
                          'input[autocomplete="family-name"]'], "labels": [r"last\s*name"], "required": True},
    "phone": {"css": ["#OrderPhone", 'input[name="Phone"]', "#Phone", 'input[type="tel"]'],
              "labels": [r"phone|mobile"], "required": False},
    "billing_zip": {"css": ["#OrderPostalCode", 'input[name="Zip"]', 'input[name="PostalCode"]', "#Zip",
                            'input[autocomplete="postal-code"]'], "labels": [r"zip|postal"], "required": False},
    "notes": {"css": ['textarea[name="Notes"]', "#OrderNotes", "textarea"],
              "labels": [r"notes?|comments?|special"], "required": False},
    "submit": {"css": ['button[type="submit"]', 'input[type="submit"]'],
               "text": r"place order|complete|submit|register"},
}
QUANTITY_LABELS = {"female": [r"female|women|ladies"], "male": [r"\bmale\b|\bmen\b|guys"]}


def challenge_showing(page):
    try:
        return tao_portal.is_security_check(page.title(), page.locator("body").inner_text(timeout=5000))
    except Exception:
        return False


def wait_for_human(page, timeout_s=CHALLENGE_TIMEOUT_S, poll_s=2, clock=time.monotonic, sleep=time.sleep,
                   say=print):
    """Wait until the human check is gone. Returns True when cleared, False on timeout.
    Only reads the page; never clicks or alters the check."""
    if not challenge_showing(page):
        return True
    say("\n>>> Click the human check in the Chrome window. Waiting up to "
        f"{timeout_s // 60} min...\a")
    deadline = clock() + timeout_s
    while clock() < deadline:
        sleep(poll_s)
        if not challenge_showing(page):
            say(">>> Thanks — check cleared. Filling the form.")
            return True
    return False


def find_field(page, spec):
    for sel in spec.get("css", []):
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            continue
    for pattern in spec.get("labels", []):
        loc = page.get_by_label(re.compile(pattern, re.I)).first
        try:
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def fill_fields(page, job, selectors=SELECTORS):
    """Fill every text field. Returns a list of required fields not found."""
    missing = []
    for key in ("email", "first_name", "last_name", "phone", "billing_zip", "notes"):
        spec, value = selectors[key], job.get(key)
        if not value:
            continue
        field = find_field(page, spec)
        if field is None:
            if spec.get("required"):
                missing.append(key)
            continue
        field.fill(str(value))
    return missing


def pick_quantities(page, job):
    """Choose the women/men counts. Returns a list of problems."""
    problems = []
    selects = tao_portal._selects_by_gender(page)
    for gender in ("female", "male"):
        count = int(job.get(f"{gender}_count") or 0)
        if not count:
            continue
        sel = selects.get(gender)
        if sel and sel.get("selector"):
            own = tao_portal.option_price_text(sel["levels"])
            if tao_portal.has_positive_price(own):
                problems.append(f"{gender} option is not free: {own[:60]}")
                continue
            option = next((o for o in sel["options"] if re.match(rf"^\s*{count}\b", o.get("value") or o.get("text") or "")), None)
            if not option:
                problems.append(f"{gender} quantity {count} not offered")
                continue
            page.locator(sel["selector"]).select_option(option["value"])
            continue
        loc = find_field(page, {"labels": QUANTITY_LABELS[gender]})
        if loc is None:
            problems.append(f"{gender} quantity box not found")
        else:
            try:
                loc.select_option(str(count))
            except Exception:
                loc.fill(str(count))
    return problems


def set_checkboxes(page):
    boxes = page.locator('input[type="checkbox"]')
    for i in range(boxes.count()):
        box = boxes.nth(i)
        context = box.evaluate(tao_portal.PARENT_TEXT_JS) or ""
        if (box.get_attribute("id") or "") in ("OrderOptedInSms", "OrderOptedIn") or tao_portal.MARKETING_TEXT.search(context):
            if box.is_checked():
                box.uncheck()
        elif tao_portal.TERMS_TEXT.search(context) or box.get_attribute("required") is not None:
            if not box.is_checked():
                box.check()


def submit_button(page, selectors=SELECTORS):
    spec = selectors["submit"]
    rx = re.compile(spec["text"], re.I)
    for sel in spec["css"]:
        loc = page.locator(sel).filter(has_text=rx).first
        if loc.count():
            return loc
    loc = page.get_by_role("button", name=rx).first
    return loc if loc.count() else None


def leave_open(page, why, say=print):
    say(f"\n!!! Stopped before submitting: {why}\n    Finish this one by hand in the Chrome window, "
        "then press Enter here to close it.")
    try:
        input()
    except EOFError:
        pass
    return {"ok": False, "reason": why}


def run_job(job, confirm=False, say=print):
    from playwright.sync_api import sync_playwright
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    channel = os.environ.get("AMY_SIGNUP_CHANNEL", "chrome") or None
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(PROFILE_DIR, headless=False, channel=channel,
                                                   no_viewport=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(15_000)
        try:
            say(f"Opening {job['url']}")
            page.goto(job["url"], wait_until="domcontentloaded")
            if not wait_for_human(page, say=say):
                return leave_open(page, "the human check was not cleared within 3 minutes", say)
            page.wait_for_load_state("load")
            page.wait_for_timeout(1500)
            if wait_for_human(page, say=say) is False:
                return leave_open(page, "the human check came back", say)
            body = page.locator("body").inner_text()
            if tao_portal.UNAVAILABLE_TEXT.search(body):
                return leave_open(page, "page says sold out / closed", say)

            problems = pick_quantities(page, job)
            page.wait_for_timeout(500)
            problems += [f"{k} field not found" for k in fill_fields(page, job)]
            if problems:
                return leave_open(page, "; ".join(problems), say)
            set_checkboxes(page)
            totals = " ".join(page.locator('[id*="total" i], [class*="total" i]').all_inner_texts())
            if tao_portal.has_positive_price(totals):
                return leave_open(page, f"order total is not $0 ({totals[:80]})", say)
            button = submit_button(page)
            if button is None:
                return leave_open(page, "submit button not found", say)
            if confirm:
                say("Form filled. Check it in the window, then press Enter to submit (Ctrl+C to stop).")
                input()
            button.click()
            try:
                page.wait_for_url(tao_portal.CONFIRMATION_URL, timeout=30_000)
            except Exception:
                wait_for_human(page, say=say)  # a check after submit: you clear it, we just wait
            page.wait_for_timeout(1500)
            body = page.locator("body").inner_text()
            shot = RESULTS_DIR / f"{stamp}-{job['last_name']}.png"
            page.screenshot(path=str(shot), full_page=True)
            order_id = tao_portal.order_id_from(page.url, body)
            ok = bool(order_id or tao_portal.SUCCESS_TEXT.search(body))
            result = {"ok": ok, "order_id": order_id, "url": page.url, "screenshot": str(shot),
                      "confirmation_text": body[:500]}
            say(("SUCCESS" if ok else "NOT CONFIRMED — check the window/TAO before retrying") +
                f": order {order_id or '(none)'}; screenshot {shot}")
            if not ok:
                return leave_open(page, "no confirmation page after submit (do NOT resubmit blindly)", say) | result
            return result
        finally:
            ctx.close()


def job_from_args(args):
    if args.job:
        return assisted_jobs.decode(args.job)
    if args.file:
        return assisted_jobs.validate(json.loads(pathlib.Path(args.file).read_text()))
    return assisted_jobs.make_job(args.url, {
        "first_name": args.first, "last_name": args.last, "email": args.email, "phone": args.phone,
        "female_count": args.female, "male_count": args.male, "billing_zip": args.zip}, notes=args.notes)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--job", help="token from Amy's email")
    ap.add_argument("--file", help="JSON job file")
    ap.add_argument("--url")
    ap.add_argument("--first")
    ap.add_argument("--last")
    ap.add_argument("--email")
    ap.add_argument("--phone", default="")
    ap.add_argument("--zip", default="")
    ap.add_argument("--female", type=int, default=0)
    ap.add_argument("--male", type=int, default=0)
    ap.add_argument("--notes", default="")
    ap.add_argument("--confirm", action="store_true", help="pause for Enter before the final submit")
    ap.add_argument("--show", action="store_true", help="print the job and exit")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    job = job_from_args(args)
    if not tao_portal.is_safe_pass_url(job["url"].split("?")[0]):
        sys.exit(f"Refusing: not a tickets.taogroup.com guest-list URL: {job['url']}")
    if args.show:
        print(json.dumps(job, indent=2))
        return 0
    result = run_job(job, confirm=args.confirm)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_DIR / "results.jsonl", "a") as f:
        f.write(json.dumps({"job": job, "result": {k: v for k, v in result.items() if k != "confirmation_text"}}) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
