"""TAO open/closed: Women/Men selectable means open, whatever the code box says (Thomas, Nov 15 2026)."""
import unittest
from unittest import mock

import tao_portal

CODE = 'input[id*="AccessCode" i]'
OPTS = [{"value": str(i), "text": str(i)} for i in range(11)]


class Field:
    def __init__(self, visible=True, enabled=True, present=True):
        self.visible, self.enabled, self.present = visible, enabled, present
        self.first = self

    def count(self):
        return 1 if self.present else 0

    def nth(self, _i):
        return self

    def is_visible(self):
        return self.visible

    def is_enabled(self):
        return self.enabled


class Page:
    def __init__(self, fields, body=""):
        self.fields, self.body = fields, body

    def locator(self, sel):
        if sel == "body":
            return mock.Mock(inner_text=mock.Mock(return_value=self.body))
        return self.fields.get(sel, Field(present=False))


def selects(**genders):
    return {g: {"selector": f"#{g}", "options": opts, "levels": [f"Guest List - {g} FREE"]}
            for g, opts in genders.items()}


class CodeGateTests(unittest.TestCase):
    def check(self, page, sel):
        with mock.patch.object(tao_portal, "_selects_by_gender", return_value=sel):
            return tao_portal.is_code_gated(page)

    def test_visible_code_box_with_selectable_options_is_open(self):
        page = Page({"#female": Field(), "#male": Field(), CODE: Field(visible=True)},
                    body="Have a promo code? Enter Code  Access code")
        self.assertFalse(self.check(page, selects(female=OPTS, male=OPTS)))

    def test_hidden_promo_box_is_open(self):
        page = Page({"#female": Field(), "#male": Field(), CODE: Field(visible=False)})
        self.assertFalse(self.check(page, selects(female=OPTS, male=OPTS)))

    def test_one_gender_selectable_is_open(self):
        page = Page({"#female": Field(enabled=False), "#male": Field(), CODE: Field()})
        self.assertFalse(self.check(page, selects(female=OPTS, male=OPTS)))

    def test_no_selectable_options_and_code_required_is_gated(self):
        page = Page({"#female": Field(enabled=False), "#male": Field(enabled=False), CODE: Field()})
        self.assertTrue(self.check(page, selects(female=OPTS, male=OPTS)))
        self.assertTrue(self.check(Page({CODE: Field()}), {}))
        self.assertTrue(self.check(Page({}, body="Access code required"), {}))

    def test_zero_only_quantity_is_not_selectable(self):
        page = Page({"#female": Field(), CODE: Field()})
        self.assertTrue(self.check(page, selects(female=[{"value": "0", "text": "0"}])))

    def test_no_options_and_no_code_is_not_gated(self):
        self.assertFalse(self.check(Page({}), {}))


if __name__ == "__main__":
    unittest.main()
