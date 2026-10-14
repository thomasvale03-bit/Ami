
# Newsletter opt-out store: tests never touch Gmail; default "nobody opted out".
import os as _os
_os.environ.setdefault("NEWSLETTER_LINK_SECRET", "test-link-secret")
_os.environ["AMYI_DISABLED"] = "true"  # tests never ping the live AMYI webhook
_os.environ.pop("AMYI_WEBHOOK_URL", None)


class _NullNewsletterStore:
    def is_opted_out(self, email):
        return False

    def all(self, refresh=False):
        return {}


try:
    import main as _main
    _main._NEWSLETTER_STORE = _NullNewsletterStore()
except Exception:  # noqa: BLE001
    pass
