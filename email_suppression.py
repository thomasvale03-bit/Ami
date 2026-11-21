"""Durable skip-list for rejected or bounced email addresses.

A hard reject (SMTP 5xx, user unknown, invalid recipient) or a mailer-daemon
bounce is recorded once and never retried. Transient failures (timeouts,
429, 4xx SMTP greylisting) are not recorded.

Store: AMY_CACHE_DIR/suppressed_emails.json (on Railway, /data). kadiance_olson@hotmail.com
is always seeded so a volume wipe cannot put her back on the follow-up list.
"""
from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import tempfile
from datetime import datetime, timezone

log = logging.getLogger("amy.suppression")

SEED_SUPPRESSED = {
    "kadiance_olson@hotmail.com": {
        "reason": "rejected",
        "source": "owner-request-2026-11-21",
    },
}

# Internal addresses we never put on the list (team notices still need to send).
_INTERNAL_DOMAINS = ("playmakerentertainment.com",)

_REJECT_RE = re.compile(
    r"(?i)("
    r"address not found|user unknown|mailbox unavailable|invalid recipient|"
    r"recipient rejected|rejected address|undeliverable|"
    r"550\s*5\.[0-9]\.[0-9]|5\.1\.1|5\.1\.10|5\.2\.1|"
    r"no such user|does not exist|account disabled|disabled mailbox|"
    r"recipient address rejected|bad destination|unknown user|"
    r"mailbox not found|invalid mailbox|not a valid mailbox|"
    r"the email account that you tried to reach does not exist|"
    r"invalid.?to.?header"
    r")"
)

_HARD_SMTP = {550, 551, 552, 553, 554}
_TRANSIENT_HTTP = {0, 429, 502, 503}
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_BOUNCE_QUERY = (
    'from:(mailer-daemon OR postmaster) '
    '(subject:"Delivery Status Notification (Failure)" OR '
    'subject:"Undelivered Mail Returned to Sender" OR '
    'subject:"Address not found" OR "wasn\'t delivered") '
    '-in:sent'
)

_BOUNCE_RECIPIENT_RES = (
    re.compile(r"(?i)final-recipient:\s*(?:rfc822;)?\s*(" + _EMAIL_RE.pattern + r")"),
    re.compile(r"(?i)x-failed-recipients:\s*(" + _EMAIL_RE.pattern + r")"),
    re.compile(r"(?i)original-recipient:\s*(?:rfc822;)?\s*(" + _EMAIL_RE.pattern + r")"),
    re.compile(r"(?i)wasn['’]t delivered to\s+(" + _EMAIL_RE.pattern + r")"),
    re.compile(r"(?i)the following address(?:es)? failed:\s*(" + _EMAIL_RE.pattern + r")"),
)

_INGEST_KEY = "__ingest__"
_cache: dict | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(address: str) -> str:
    raw = (address or "").strip()
    m = _EMAIL_RE.search(raw)
    return (m.group(0) if m else raw).strip().lower().strip("<>")


def _path() -> str:
    d = os.environ.get("AMY_CACHE_DIR") or ("/data" if os.path.isdir("/data") else "")
    return os.path.join(d, "suppressed_emails.json") if d else ""


def _seed() -> dict:
    return {addr: {**dict(rec), "email": addr} for addr, rec in SEED_SUPPRESSED.items()}


def load() -> dict:
    """Address -> record. Always includes the seeded addresses."""
    global _cache
    if _cache is not None:
        return _cache
    out = _seed()
    path = _path()
    if path:
        try:
            with open(path) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                for key, rec in loaded.items():
                    if key == _INGEST_KEY:
                        out[key] = rec if isinstance(rec, dict) else {}
                        continue
                    addr = norm(key)
                    if addr and "@" in addr and isinstance(rec, dict):
                        out[addr] = rec
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("Ignoring unreadable suppression store %s", path)
    _cache = out
    return out


def _save(records: dict) -> None:
    path = _path()
    if not path:
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="suppressed.", suffix=".json", dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(records, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        log.exception("Could not save suppression store %s", path)


def is_suppressed(address: str) -> bool:
    addr = norm(address)
    if not addr:
        return False
    return addr in load() and addr != _INGEST_KEY


def record(address: str, reason: str = "rejected", error: str = "", source: str = "") -> dict | None:
    addr = norm(address)
    if not addr or "@" not in addr or _is_internal(addr):
        return None
    records = load()
    rec = dict(records.get(addr) or {})
    rec.update({
        "email": addr,
        "reason": (reason or "rejected")[:200],
        "error": (error or rec.get("error") or "")[:500],
        "source": (source or rec.get("source") or "")[:120],
        "added_at": rec.get("added_at") or _now(),
        "updated_at": _now(),
    })
    records[addr] = rec
    global _cache
    _cache = records
    _save(records)
    log.info("Suppressed %s (%s, %s)", addr, rec["reason"], rec["source"])
    return rec


def _is_internal(address: str) -> bool:
    addr = norm(address)
    return any(addr.endswith("@" + d) or addr.endswith("." + d) for d in _INTERNAL_DOMAINS)


def _smtp_codes(exc: BaseException) -> list[int]:
    codes = []
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        codes.append(code)
    recipients = getattr(exc, "recipients", None)
    if isinstance(recipients, dict):
        for item in recipients.values():
            if isinstance(item, tuple) and item and isinstance(item[0], int):
                codes.append(item[0])
            elif isinstance(item, int):
                codes.append(item)
    return codes


def is_hard_reject(exc_or_code=None, body: str = "") -> bool:
    """True for a permanent delivery / recipient failure, not a rate limit or timeout."""
    if isinstance(exc_or_code, smtplib.SMTPAuthenticationError):
        return False
    if isinstance(exc_or_code, (TimeoutError, ConnectionError)):
        return False

    text = body or ""
    codes: list[int] = []
    status = None
    if isinstance(exc_or_code, BaseException):
        text = f"{exc_or_code} {text}"
        codes = _smtp_codes(exc_or_code)
        status = getattr(getattr(exc_or_code, "resp", None), "status", None)
    else:
        try:
            codes.append(int(exc_or_code))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            if exc_or_code:
                text = f"{exc_or_code} {text}"

    if status is not None:
        codes.append(int(status))
    transient = {421, 450, 451, 452, 403, 429, 502, 503, 0}
    hard = any(c in _HARD_SMTP or c >= 550 or c == 422 for c in codes)
    if hard:
        return True
    if any(c in transient for c in codes):
        return False
    # Gmail API 400 for an invalid recipient is permanent; other 400s are not.
    if 400 in codes:
        return bool(_REJECT_RE.search(text))
    if _REJECT_RE.search(text):
        if 200 in codes and re.search(r'(?i)"sent"\s*:\s*true', text):
            return False
        return True
    return False


def bounce_recipients(body: str) -> list[str]:
    """Failed recipient addresses parsed from a mailer-daemon DSN body."""
    found: list[str] = []
    text = body or ""
    for cre in _BOUNCE_RECIPIENT_RES:
        for m in cre.finditer(text):
            addr = norm(m.group(1))
            if addr and addr not in found and not _is_internal(addr):
                found.append(addr)
    return found


def ingest_bounces(service, days: int = 45) -> int:
    """Record recipients of mailer-daemon failure notices. Returns how many were newly added."""
    if service is None:
        return 0
    import gmail_client

    records = load()
    ingest = dict(records.get(_INGEST_KEY) or {})
    seen = list(ingest.get("ids") or [])
    seen_set = set(seen)
    added = 0
    try:
        resp = service.users().messages().list(
            userId="me",
            q=f"{_BOUNCE_QUERY} newer_than:{max(int(days), 1)}d",
            maxResults=100,
        ).execute()
    except Exception:  # noqa: BLE001
        log.exception("Bounce ingest list failed")
        return 0
    for ref in resp.get("messages") or []:
        mid = ref.get("id")
        if not mid or mid in seen_set:
            continue
        try:
            _msg, body = gmail_client.get_plain_text_body(service, mid)
        except Exception:  # noqa: BLE001
            log.exception("Bounce ingest read failed for %s", mid)
            body = ""
        already = {addr for addr in bounce_recipients(body or "") if is_suppressed(addr)}
        for addr in bounce_recipients(body or ""):
            if record(addr, reason="bounced", error="mailer-daemon", source="mailer-daemon") and addr not in already:
                added += 1
        seen.append(mid)
        seen_set.add(mid)
    ingest["ids"] = seen[-500:]
    ingest["last_at"] = _now()
    records = load()
    records[_INGEST_KEY] = ingest
    global _cache
    _cache = records
    _save(records)
    return added


def reset_for_tests() -> None:
    """Drop the in-memory cache so tests can use an isolated AMY_CACHE_DIR."""
    global _cache
    _cache = None
