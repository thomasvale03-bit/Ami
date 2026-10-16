"""Skeleton client for the official TicketSauce v2 API.

VERIFIED (public docs, speca.io/ticketsauce/ticketsauce-public-api):
    POST https://api.ticketsauce.com/v2/oauth/token
      grant_type=client_credentials, client_id, client_secret
      -> {"access_token": ...}, short-lived; send as "Authorization: Bearer".
    Credentials come from TicketSauce: My Account -> API.

UNVERIFIED: the public docs found only describe read endpoints (events,
orders, check-in ids). No documented endpoint for creating a guest-list
registration / $0 order was found. REGISTRATION_PATH and the payload in
create_registration() are placeholders and MUST be confirmed with
TicketSauce/TAO before use. Until TICKETSAUCE_REGISTRATION_VERIFIED=true is
set, create_registration() refuses to send anything.

Env: TICKETSAUCE_CLIENT_ID, TICKETSAUCE_CLIENT_SECRET,
     TICKETSAUCE_REGISTRATION_PATH (optional override),
     TICKETSAUCE_REGISTRATION_VERIFIED.
"""
import json
import os
import time
import urllib.parse
import urllib.request

BASE = "https://api.ticketsauce.com"
TOKEN_PATH = "/v2/oauth/token"
REGISTRATION_PATH = os.environ.get("TICKETSAUCE_REGISTRATION_PATH", "/v2/orders")  # UNVERIFIED guess


class ApiNotReady(Exception):
    pass


def credentials():
    cid = os.environ.get("TICKETSAUCE_CLIENT_ID", "").strip()
    secret = os.environ.get("TICKETSAUCE_CLIENT_SECRET", "").strip()
    return (cid, secret) if cid and secret else None


def has_credentials():
    return credentials() is not None


def registration_verified():
    return os.environ.get("TICKETSAUCE_REGISTRATION_VERIFIED", "").strip().lower() in ("1", "true", "yes")


def _post(url, data, headers, opener):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with opener(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


class Client:
    def __init__(self, client_id, client_secret, opener=urllib.request.urlopen, clock=time.time):
        self.client_id, self.client_secret = client_id, client_secret
        self._open, self._clock = opener, clock
        self._token, self._expires = None, 0

    @classmethod
    def from_env(cls, **kw):
        creds = credentials()
        if not creds:
            raise ApiNotReady("TICKETSAUCE_CLIENT_ID / TICKETSAUCE_CLIENT_SECRET not set")
        return cls(*creds, **kw)

    def token(self):
        if self._token and self._clock() < self._expires:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.client_secret}).encode()
        data = _post(BASE + TOKEN_PATH, body, {"Content-Type": "application/x-www-form-urlencoded",
                                               "Accept": "application/json"}, self._open)
        token = data.get("access_token")
        if not token:
            raise ApiNotReady(f"No access_token in TicketSauce response: {list(data)}")
        # Docs: expires "after a few minutes"; refresh well before that.
        self._token, self._expires = token, self._clock() + min(int(data.get("expires_in") or 240), 240) - 30
        return token

    def create_registration(self, event_id, guest):
        """UNVERIFIED: create a $0 guest-list registration. Returns
        {"confirmation_id", "verified", ...} in tao_portal's shape."""
        if not registration_verified():
            raise ApiNotReady("TicketSauce registration endpoint is not verified "
                              "(set TICKETSAUCE_REGISTRATION_VERIFIED=true once confirmed)")
        payload = {  # UNVERIFIED field names
            "event_id": event_id,
            "first_name": guest["first_name"], "last_name": guest["last_name"],
            "email": guest["email"], "phone": guest.get("phone") or "",
            "female_count": guest.get("female_count", 0), "male_count": guest.get("male_count", 0),
            "utm_source": "promoter",
        }
        data = _post(BASE + REGISTRATION_PATH, json.dumps(payload).encode(),
                     {"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json",
                      "Accept": "application/json"}, self._open)
        order_id = data.get("order_id") or data.get("id") or (data.get("Order") or {}).get("id")
        if not order_id:
            return {"confirmation_id": None, "verified": False, "reason": f"API gave no order id: {data}"}
        return {"confirmation_id": order_id, "verified": True, "via": "ticketsauce_api"}
