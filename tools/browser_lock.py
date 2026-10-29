"""Shared box-browser lock (same mkdir protocol as /workspace/tao_lock.sh).

Parallel TAO sign-up runs must not drive the shared Chrome at once: they land on
each other's tabs and confirm the wrong guest. Lock dir holds an `owner` file:
line 1 = ref, line 2 = epoch seconds. Locks older than 12 min are stale.
"""
import contextlib
import datetime
import os
import shutil
import time

LOCK_DIR = os.environ.get("TAO_LOCK_DIR", "/workspace/.tao_browser.lock")
LOG_FILE = os.environ.get("TAO_LOCK_LOG", "/workspace/tao_lock.log")
POLL_S = float(os.environ.get("TAO_LOCK_POLL", 5))
WAIT_S = float(os.environ.get("TAO_LOCK_WAIT", 1200))
STALE_S = float(os.environ.get("TAO_LOCK_STALE", 720))
MST = datetime.timezone(datetime.timedelta(hours=-7), "MST")


class LockTimeout(RuntimeError):
    pass


def _log(msg, log_file):
    try:
        with open(log_file, "a") as f:
            f.write(f"{datetime.datetime.now(MST):%Y-%m-%d %H:%M:%S} MST [{os.getpid()}] {msg}\n")
    except OSError:
        pass


def _owner(lock_dir):
    try:
        lines = open(os.path.join(lock_dir, "owner")).read().splitlines()
    except OSError:
        return "", None
    ref = lines[0] if lines else ""
    try:
        ts = float(lines[1])
    except (IndexError, ValueError):
        ts = None
    return ref, ts


def acquire(ref, lock_dir=LOCK_DIR, log_file=LOG_FILE, poll_s=POLL_S, wait_s=WAIT_S,
            stale_s=STALE_S, clock=time.time, sleep=time.sleep):
    start = clock()
    logged_wait = False
    while True:
        try:
            os.mkdir(lock_dir)
        except FileExistsError:
            pass
        else:
            with open(os.path.join(lock_dir, "owner"), "w") as f:
                f.write(f"{ref}\n{int(clock())}\n")
            _log(f"ACQUIRED ref={ref} after {int(clock() - start)}s", log_file)
            return True
        holder, ts = _owner(lock_dir)
        if ts is None:
            try:
                ts = os.stat(lock_dir).st_mtime
            except OSError:
                continue
        now = clock()
        if now - ts > stale_s:
            _log(f"STALE lock held by ref={holder} age={int(now - ts)}s broken by ref={ref}", log_file)
            shutil.rmtree(lock_dir, ignore_errors=True)
            continue
        if now - start >= wait_s:
            _log(f"TIMEOUT ref={ref} waited {int(wait_s)}s holder={holder}", log_file)
            raise LockTimeout(f"browser busy (held by {holder})")
        if not logged_wait:
            _log(f"WAITING ref={ref} holder={holder}", log_file)
            logged_wait = True
        sleep(poll_s)


def release(ref, lock_dir=LOCK_DIR, log_file=LOG_FILE):
    holder, _ = _owner(lock_dir)
    if os.path.isdir(lock_dir) and holder == ref:
        shutil.rmtree(lock_dir, ignore_errors=True)
        _log(f"RELEASED ref={ref}", log_file)
        return True
    _log(f"NOT_OWNER release ref={ref} holder={holder}", log_file)
    return False


@contextlib.contextmanager
def held(ref, **kw):
    rel = {k: kw[k] for k in ("lock_dir", "log_file") if k in kw}
    acquire(ref, **kw)
    try:
        yield
    finally:
        release(ref, **rel)
