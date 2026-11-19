"""Unit tests for tolerant TAO promoter-page loads. No browser needed."""
from unittest import mock

import pytest

import tao_portal


class TimeoutError(Exception):
    """Stand-in for Playwright's TimeoutError."""


class FakeSettlePage:
    def __init__(self, outcomes, ready="complete", body=True):
        self.outcomes = list(outcomes)
        self.calls = []
        self.ready = ready
        self.body = body

    def wait_for_load_state(self, state, timeout=None):
        self.calls.append((state, timeout))
        if not self.outcomes:
            return
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

    def evaluate(self, script):
        return self.ready

    class _Body:
        def __init__(self, present):
            self.present = present

        def count(self):
            return 1 if self.present else 0

    def locator(self, sel):
        return self._Body(self.body)


class FakeNavPage:
    def __init__(self, goto_results, reload_results=None):
        self.goto_results = list(goto_results)
        self.reload_results = list(reload_results or [])
        self.gotos = []
        self.reloads = []

    def goto(self, url, **kwargs):
        self.gotos.append((url, kwargs))
        result = self.goto_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def reload(self, **kwargs):
        self.reloads.append(kwargs)
        result = self.reload_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_settle_returns_on_networkidle():
    page = FakeSettlePage([None])
    tao_portal._settle(page)
    assert page.calls == [("networkidle", tao_portal.PAGE_LOAD_TIMEOUT_MS)]


def test_settle_falls_back_to_load_then_returns():
    page = FakeSettlePage([TimeoutError("networkidle"), None])
    tao_portal._settle(page)
    assert page.calls == [
        ("networkidle", tao_portal.PAGE_LOAD_TIMEOUT_MS),
        ("load", tao_portal.PAGE_LOAD_TIMEOUT_MS),
    ]


def test_settle_does_not_raise_when_load_times_out_but_dom_is_usable():
    page = FakeSettlePage([TimeoutError("networkidle"), TimeoutError("load")], ready="complete")
    tao_portal._settle(page)  # must not raise
    assert page.calls[1] == ("load", tao_portal.PAGE_LOAD_TIMEOUT_MS)


def test_settle_does_not_raise_even_if_dom_is_not_ready():
    page = FakeSettlePage(
        [TimeoutError("networkidle"), TimeoutError("load")], ready="loading", body=False
    )
    tao_portal._settle(page)  # must not raise; later reads handle a blank page


def test_goto_uses_explicit_60s_timeout():
    page = FakeNavPage([None])
    tao_portal._goto_promoter(page)
    assert len(page.gotos) == 1
    url, kwargs = page.gotos[0]
    assert url == tao_portal.TAO_PROMOTER_URL
    assert kwargs["wait_until"] == "domcontentloaded"
    assert kwargs["timeout"] == tao_portal.PAGE_LOAD_TIMEOUT_MS
    assert page.reloads == []


def test_goto_returns_after_successful_reload():
    err = TimeoutError("Timeout 60000ms exceeded.")
    page = FakeNavPage([err], reload_results=[None])
    with mock.patch.object(tao_portal.time, "sleep") as slept:
        tao_portal._goto_promoter(page)
    slept.assert_not_called()
    assert len(page.gotos) == 1 and len(page.reloads) == 1
    assert page.reloads[0]["timeout"] == tao_portal.PAGE_LOAD_TIMEOUT_MS


def test_goto_retries_three_times_with_backoff_then_raises():
    err = TimeoutError("Timeout 60000ms exceeded.")
    page = FakeNavPage([err, err, err], reload_results=[err, err])
    with mock.patch.object(tao_portal.time, "sleep") as slept:
        with pytest.raises(TimeoutError, match="60000ms"):
            tao_portal._goto_promoter(page)
    assert len(page.gotos) == 3
    assert len(page.reloads) == 2
    assert [c.args[0] for c in slept.call_args_list] == [2, 4]
    assert all(kwargs["timeout"] == tao_portal.PAGE_LOAD_TIMEOUT_MS for _, kwargs in page.gotos)


def test_goto_succeeds_on_second_attempt_after_backoff():
    err = TimeoutError("Timeout 60000ms exceeded.")
    page = FakeNavPage([err, None], reload_results=[err])
    with mock.patch.object(tao_portal.time, "sleep") as slept:
        tao_portal._goto_promoter(page)
    slept.assert_called_once_with(2)
    assert len(page.gotos) == 2 and len(page.reloads) == 1
