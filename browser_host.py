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

Chrome can crash between pickups (it did after the Nov 9 4 AM and 9 AM runs).
ensure() / restart() bring it back; the worker retries a job once instead of
sending it to manual on the first connection refused.
"""
import atexit
import logging
import os
import shutil
import subprocess
import time
import urllib.request

log = logging.getLogger(__name__)

FORBIDDEN_PROFILES = ("/home/box/chrome-profile",)  # computerUse's real browser

_procs = []
_atexit_registered = False
_log_handle = None


def port():
    return int(os.environ.get("AMY_BROWSER_CDP_PORT", "9335"))


def cdp_url():
    return os.environ.get("AMY_BROWSER_CDP_URL", "").strip() or f"http://127.0.0.1:{port()}"


def healthy(url=None, timeout=2):
    """True when the worker Chrome is answering on its debugging port."""
    url = url or cdp_url()
    try:
        with urllib.request.urlopen(url + "/json/version", timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _wait(url, seconds=30):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if healthy(url, timeout=2):
            return True
        time.sleep(0.5)
    return False


def _free_display():
    for n in range(80, 120):
        if not os.path.exists(f"/tmp/.X11-unix/X{n}") and not os.path.exists(f"/tmp/.X{n}-lock"):
            return f":{n}"
    raise RuntimeError("no free X display")


def _log_file():
    global _log_handle
    if _log_handle and not _log_handle.closed:
        return _log_handle
    path = os.environ.get("AMY_BROWSER_LOG", "").strip()
    if not path:
        work = os.environ.get("AMY_WORKER_DIR", "").strip()
        path = os.path.join(os.path.dirname(work), "chrome.log") if work else "/tmp/amy-worker-chrome.log"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _log_handle = open(path, "a", buffering=1)  # noqa: SIM115 - kept open for the Chrome process
    return _log_handle


def _stop_tracked():
    global _procs
    for proc in reversed(_procs):
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
        except Exception:  # noqa: BLE001
            pass
    _procs = []


def stop():
    """Stop only the worker's plain Chrome and its private Xvfb."""
    _stop_tracked()
    profile = os.path.abspath(os.environ.get("AMY_BROWSER_PROFILE") or "/data/worker/profile")
    if profile and not any(profile.startswith(p) for p in FORBIDDEN_PROFILES):
        try:
            subprocess.run(["pkill", "-f", f"user-data-dir={profile}"], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass
    for _ in range(20):
        if not healthy(timeout=0.3):
            break
        time.sleep(0.1)


def restart():
    """Kill a dead or stale worker Chrome and start a fresh one."""
    if os.environ.get("AMY_BROWSER_LAUNCH", "").lower() != "plain":
        return None
    log.warning("Restarting the booking Chrome")
    stop()
    return start(force=True)


def ensure():
    """Return the CDP URL, starting or restarting Chrome if it is down.

    No-op (returns the existing URL, or None) when AMY_BROWSER_LAUNCH is not
    plain, so tests and the Railway worker path are unchanged.
    """
    if os.environ.get("AMY_BROWSER_LAUNCH", "").lower() != "plain":
        return os.environ.get("AMY_BROWSER_CDP_URL") or None
    if healthy():
        url = cdp_url()
        os.environ["AMY_BROWSER_CDP_URL"] = url
        return url
    log.warning("Booking Chrome is not answering on %s; restarting it", cdp_url())
    return restart()


def start(force=False):
    if os.environ.get("AMY_BROWSER_LAUNCH", "").lower() != "plain":
        return None
    if not force and healthy():
        url = cdp_url()
        os.environ["AMY_BROWSER_CDP_URL"] = url
        return url
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
    port_n = port()
    env = dict(os.environ)
    global _procs
    _procs = []
    if not env.get("DISPLAY"):
        display = _free_display()
        logf = _log_file()
        _procs.append(subprocess.Popen(["Xvfb", display, "-screen", "0", "1366x900x24"],
                                       stdout=logf, stderr=logf))
        env["DISPLAY"] = display
        time.sleep(1)
    args = [binary, f"--user-data-dir={profile}", f"--remote-debugging-port={port_n}",
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
    logf = _log_file()
    _procs.append(subprocess.Popen(args, env=env, stdout=logf, stderr=logf))
    url = f"http://127.0.0.1:{port_n}"
    if not _wait(url):
        _stop_tracked()
        raise RuntimeError("plain Chrome did not open its debugging port")

    global _atexit_registered
    if not _atexit_registered:
        atexit.register(_stop_tracked)
        _atexit_registered = True
    os.environ["AMY_BROWSER_CDP_URL"] = url
    log.info("Booking Chrome is up at %s", url)
    return url
