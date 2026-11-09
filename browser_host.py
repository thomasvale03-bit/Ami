"""Start a plain Chrome for the booking worker and expose it over CDP.

Why: Chrome launched BY Playwright carries automation flags and TAO's
Cloudflare check re-challenges it forever (even after a person clicks it).
The same Chrome binary started normally, with its own profile, clears the
check on its own in a few seconds (tested on the box, Nov 9 2026). The worker
then attaches with connect_over_cdp (AMY_BROWSER_CDP_URL).

Nothing here clicks, solves or hides the check; it is an ordinary browser.

AMY_BROWSER_LAUNCH=plain       start it (otherwise do nothing)
AMY_BROWSER_PROFILE            its own profile dir (NEVER the computerUse profile)
AMY_BROWSER_CDP_PORT           default 9335, bound to 127.0.0.1 only
CHROMIUM_PATH                  binary (default: google-chrome / chromium on PATH)
AMY_BROWSER_PROXY              optional http(s) proxy WITHOUT credentials
                               (Chrome flags cannot carry proxy auth)
No DISPLAY -> starts a private Xvfb so the window never appears on a desktop.
"""
import atexit
import os
import shutil
import subprocess
import time
import urllib.request

FORBIDDEN_PROFILES = ("/home/box/chrome-profile",)  # computerUse's real browser


def _wait(url, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/json/version", timeout=2):
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def _free_display():
    for n in range(80, 120):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
            return f":{n}"
    raise RuntimeError("no free X display")


def start():
    if os.environ.get("AMY_BROWSER_LAUNCH", "").lower() != "plain":
        return None
    profile = os.path.abspath(os.environ.get("AMY_BROWSER_PROFILE") or "/data/worker/profile")
    if any(profile.startswith(p) for p in FORBIDDEN_PROFILES):
        raise RuntimeError("refusing to run the worker on the computerUse browser profile")
    os.makedirs(profile, exist_ok=True)
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            os.remove(os.path.join(profile, name))  # stale after a crash; own profile only
        except FileNotFoundError:
            pass
    binary = (os.environ.get("CHROMIUM_PATH") or shutil.which("google-chrome") or shutil.which("chromium")
              or shutil.which("chromium-browser"))
    if not binary:
        try:  # Playwright's bundled Chromium (Railway image)
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                binary = p.chromium.executable_path
        except Exception:  # noqa: BLE001
            binary = None
    if not binary:
        raise RuntimeError("no Chrome/Chromium binary found")
    port = int(os.environ.get("AMY_BROWSER_CDP_PORT", "9335"))
    env = dict(os.environ)
    procs = []
    if not env.get("DISPLAY"):
        display = _free_display()
        procs.append(subprocess.Popen(["Xvfb", display, "-screen", "0", "1366x900x24"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        env["DISPLAY"] = display
        time.sleep(1)
    args = [binary, f"--user-data-dir={profile}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
            "--password-store=basic", "--window-size=1366,900"]
    if os.geteuid() == 0:
        args.append("--no-sandbox")  # containers run as root
    proxy = os.environ.get("AMY_BROWSER_PROXY", "").strip()
    if proxy:
        from urllib.parse import urlparse
        u = urlparse(proxy)
        if u.username:
            raise RuntimeError("plain launch cannot use a proxy with credentials; use an IP-allowlisted proxy")
        args.append(f"--proxy-server={proxy}")
    args.append("about:blank")
    procs.append(subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    url = f"http://127.0.0.1:{port}"
    if not _wait(url):
        for proc in procs:
            proc.terminate()
        raise RuntimeError("plain Chrome did not open its debugging port")

    def _stop():
        for proc in reversed(procs):
            try:
                proc.terminate()
                proc.wait(10)
            except Exception:  # noqa: BLE001
                pass
    atexit.register(_stop)
    os.environ["AMY_BROWSER_CDP_URL"] = url
    return url
