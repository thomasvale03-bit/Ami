"""Open the worker's browser profile HEADED on the box desktop at TAO's
promoter page and wait (no automatic clicks) for a person to clear the
Cloudflare check. Polls every 10 s for up to WARMUP_MINUTES (20); when the
promoter page with Pass links shows, closes cleanly so the profile keeps the
clearance cookies. Logs to stdout (redirect to a file)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tao_portal  # noqa: E402
from config.rules import TAO_PROMOTER_URL  # noqa: E402


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S %Z"), msg, flush=True)


def main():
    from playwright.sync_api import sync_playwright
    os.environ["AMY_BROWSER_HEADLESS"] = "false"
    minutes = int(os.environ.get("WARMUP_MINUTES", "20"))
    with sync_playwright() as p:
        context, page = tao_portal._browser_page(p)
        try:
            page.goto(TAO_PROMOTER_URL, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + minutes * 60
            while time.time() < deadline:
                try:
                    title = page.title()
                    body = page.locator("body").inner_text(timeout=5000)
                except Exception as exc:  # noqa: BLE001 - page mid-navigation
                    log(f"reading page: {type(exc).__name__}")
                    page.wait_for_timeout(10000)
                    continue
                check = tao_portal.is_security_check(title, body)
                passes = 0 if check else page.locator("a", has_text="Pass").count()
                log(f"title={title!r} security_check={check} pass_links={passes}")
                if not check and passes:
                    log("CLEARED: promoter page with Pass links is showing; saving profile")
                    page.wait_for_timeout(5000)
                    return 0
                page.wait_for_timeout(10000)
            log("TIMEOUT: check not cleared in time")
            return 2
        finally:
            context.close()
            log("browser closed")


if __name__ == "__main__":
    sys.exit(main())
