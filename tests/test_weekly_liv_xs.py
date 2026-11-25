"""Optional LIV & XS section on the weekly newsletter."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from templates import weekly_newsletter as w


WEDNESDAY = date(2026, 10, 14)  # week Tue Oct 13 – Mon Oct 19
NEXT_WED = date(2026, 12, 2)    # week Tue Dec 1 – Mon Dec 7
THIS_WED = date(2026, 11, 25)   # week Tue Nov 24 – Mon Nov 30
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "newsletter_2026-10-13")


def _flyers(d, lineups=True):
    names = ["261017_HKLV_SS_Ohgeesy_1080x1350.jpg", "261016_JWL_Lucky-Lou_1080x1350.jpg"]
    if lineups:
        names += ["weekly_nightclub.jpg", "weekly_dayclub.jpg"]
    for n in names:
        with open(os.path.join(d, n), "wb") as fh:
            fh.write(b"\xff\xd8\xff\xe0fakejpeg")


def _write(path, obj):
    with open(path, "w") as fh:
        json.dump(obj, fh)


class LivXsSectionTests(unittest.TestCase):
    def test_missing_file_omits_section_and_still_builds(self):
        with tempfile.TemporaryDirectory() as d:
            _flyers(d)
            subject, text, html, inline, files = w.build(WEDNESDAY, FIXTURE, d, {})
        self.assertIn("This Week in Vegas", subject)
        self.assertNotIn("LIV & XS", text)
        self.assertNotIn("LIV &amp; XS", html)
        self.assertTrue(any(c == "lineup-nightclub" for _, c in inline))

    def test_empty_and_disabled_and_past_week_omit_section(self):
        start, end = w.week_window(WEDNESDAY)
        with tempfile.TemporaryDirectory() as data:
            shutil.copy(os.path.join(FIXTURE, "top_picks.json"), data)
            shutil.copy(os.path.join(FIXTURE, "also_this_week.json"), data)
            Path(data, "liv_xs.json").write_text("")
            self.assertIsNone(w.load_liv_xs(data, start, end))
            _write(os.path.join(data, "liv_xs.json"), {})
            self.assertIsNone(w.load_liv_xs(data, start, end))
            _write(os.path.join(data, "liv_xs.json"), {
                "week_start": "2026-12-01", "enabled": False,
                "events": [{"venue": "LIV", "date": "2026-12-04", "event": "SOSA",
                            "rules": "Guys and girls comp with open bar, list until 2 AM."}],
            })
            self.assertIsNone(w.load_liv_xs(data, start, end))
            _write(os.path.join(data, "liv_xs.json"), {
                "week_start": "2026-10-06", "enabled": True,
                "events": [{"venue": "LIV", "date": "2026-10-16", "event": "SOSA",
                            "rules": "comp with open bar"}],
            })
            self.assertIsNone(w.load_liv_xs(data, start, end))

    def test_current_week_renders_rules_flyers_and_capacity(self):
        with tempfile.TemporaryDirectory() as root:
            data = os.path.join(root, "data")
            flyers = os.path.join(root, "flyers")
            os.makedirs(data)
            os.makedirs(flyers)
            shutil.copy(os.path.join(FIXTURE, "top_picks.json"), data)
            shutil.copy(os.path.join(FIXTURE, "also_this_week.json"), data)
            _flyers(flyers)
            src = os.path.join(root, "sosa.jpg")
            Path(src).write_bytes(b"\xff\xd8\xff\xe0sosa")
            dest = w.stage_liv_xs_flyer(src, flyers, "LIV", "2026-10-16", "SOSA")
            self.assertTrue(os.path.basename(dest).startswith("261016_LIV_SOSA"))
            lineup = os.path.join(flyers, "261013_LIV_weekend-lineup.jpg")
            Path(lineup).write_bytes(b"\xff\xd8\xff\xe0lineup")
            _write(os.path.join(data, "liv_xs.json"), {
                "week_start": "2026-10-13",
                "enabled": True,
                "lineup_flyer": "261013_LIV_weekend-lineup.jpg",
                "events": [
                    {"venue": "LIV", "date": "2026-10-16", "weekday": "Fri", "event": "SOSA",
                     "rules": "Guys and girls comp with open bar, list until 2 AM.",
                     "flyer": "261016_LIV_SOSA.jpg"},
                    {"venue": "XS", "date": "2026-10-16", "event": "LOUD LUXURY",
                     "rules": "Ladies and guys free until 1 AM, reduced after. No drinks."},
                ],
                "notes": ["LIV is closed Sun Oct 18."],
            })
            subject, text, html, inline, files = w.build(WEDNESDAY, data, flyers, {})
        self.assertIn("LIV & XS", text)
        self.assertIn("LIV &amp; XS", html)
        self.assertIn("SOSA", html)
        self.assertIn("comp with open bar", html)
        self.assertIn("LOUD LUXURY", html)
        self.assertIn("LIV is closed Sun Oct 18.", html)
        self.assertIn("All subject to capacity.", html)
        self.assertIn("All subject to capacity.", text)
        self.assertIn("cid:liv-xs-lineup", html)
        self.assertEqual(html.count('src="cid:liv-xs-lineup"'), 1)  # top row only, not also in LIV & XS
        self.assertLess(html.index("cid:liv-xs-lineup"), html.index("LIV &amp; XS"))
        # Individual night flyers are omitted from the body (the weekly lineup already shows them)
        self.assertNotIn("cid:liv-xs-0", html)
        self.assertFalse(any(c == "liv-xs-0" for _, c in inline))
        self.assertTrue(any(c == "liv-xs-lineup" for _, c in inline))
        # Three weekly lineups (TAO night, TAO day, LIV) share one equal-tile row
        self.assertIn('width="33%"', html)
        self.assertIn("Fri-10-16_LIV_SOSA.jpg", [f for _, f in files])  # still attached from flyers dir

    def test_no_lineup_flyer_keeps_tao_top_row(self):
        with tempfile.TemporaryDirectory() as d:
            _flyers(d)
            subject, text, html, inline, files = w.build(WEDNESDAY, FIXTURE, d, {})
        self.assertIn("cid:lineup-nightclub", html)
        self.assertIn("cid:lineup-dayclub", html)
        self.assertNotIn("cid:liv-xs-lineup", html)
        self.assertEqual(html.count('width="50%"'), 2)  # just the two TAO weekly flyers

    def test_liv_and_xs_weekly_lineups_in_top_grid(self):
        start, end = w.week_window(WEDNESDAY)
        with tempfile.TemporaryDirectory() as root:
            data = os.path.join(root, "data")
            flyers = os.path.join(root, "flyers")
            os.makedirs(data)
            os.makedirs(flyers)
            shutil.copy(os.path.join(FIXTURE, "top_picks.json"), data)
            shutil.copy(os.path.join(FIXTURE, "also_this_week.json"), data)
            _flyers(flyers)
            Path(flyers, "261013_LIV_weekend-lineup.jpg").write_bytes(b"\xff\xd8\xff\xe0liv")
            Path(flyers, "261013_XS_weekend-lineup.jpg").write_bytes(b"\xff\xd8\xff\xe0xs")
            _write(os.path.join(data, "liv_xs.json"), {
                "week_start": "2026-10-13",
                "enabled": True,
                "lineup_flyer": "261013_LIV_weekend-lineup.jpg",
                "xs_lineup_flyer": "261013_XS_weekend-lineup.jpg",
                "events": [{"venue": "LIV", "date": "2026-10-16", "event": "SOSA",
                            "rules": "Guys and girls comp with open bar, list until 2 AM."}],
            })
            subject, text, html, inline, files = w.build(WEDNESDAY, data, flyers, {})
        self.assertIn("cid:liv-xs-lineup", html)
        self.assertIn("cid:liv-xs-lineup-xs", html)
        self.assertLess(html.index("cid:liv-xs-lineup"), html.index("LIV &amp; XS"))
        self.assertLess(html.index("cid:liv-xs-lineup-xs"), html.index("LIV &amp; XS"))
        # 2x2 of equal tiles: TAO night, TAO day, LIV, XS
        self.assertEqual(html.count('width="50%"'), 4)
        lineup_html = html.split("Don&rsquo;t Miss")[0]
        self.assertNotIn('width="33%"', lineup_html)  # 2x2, not 4-across
        self.assertEqual(html.count('src="cid:liv-xs-lineup"'), 1)
        self.assertEqual(html.count('src="cid:liv-xs-lineup-xs"'), 1)
        self.assertNotIn("cid:liv-xs-0", html)

    def test_stage_weekly_lineup_flyer_name(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "xs.jpg")
            Path(src).write_bytes(b"\xff\xd8\xff\xe0xs")
            dest = w.stage_weekly_lineup_flyer(src, d, "XS", "2026-12-01")
            self.assertEqual(os.path.basename(dest), "261201_XS_weekend-lineup.jpg")
            dest_liv = w.stage_weekly_lineup_flyer(src, d, "LIV", date(2026, 12, 1))
            self.assertEqual(os.path.basename(dest_liv), "261201_LIV_weekend-lineup.jpg")

    def test_does_not_promise_entry(self):


        start, end = w.week_window(WEDNESDAY)
        with tempfile.TemporaryDirectory() as data:
            _write(os.path.join(data, "liv_xs.json"), {
                "week_start": "2026-10-13",
                "enabled": True,
                "events": [{
                    "venue": "LIV", "date": "2026-10-16", "event": "SOSA",
                    "rules": "Guys and girls comp with open bar. Guaranteed entry. Skip the line.",
                }],
                "notes": ["We'll get you in before 2 AM."],
            })
            section = w.load_liv_xs(data, start, end)
        self.assertIn("comp with open bar", section["events"][0]["rules"])
        self.assertNotIn("Guaranteed entry", section["events"][0]["rules"])
        self.assertNotIn("Skip the line", section["events"][0]["rules"])
        self.assertFalse(section["notes"])  # promised-entry note dropped
        html = w.render_html(start, end, [], [], [], section)
        text = w.render_text(start, end, [], [], section)
        for body in (html, text):
            self.assertNotIn("guaranteed entry", body.lower())
            self.assertNotIn("skip the line", body.lower())
            self.assertNotIn("we'll get you in", body.lower())
        self.assertIn("All subject to capacity.", html)

    def test_shipped_placeholder_will_not_render_this_week(self):
        repo_data = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        start, end = w.week_window(THIS_WED)
        self.assertIsNone(w.load_liv_xs(repo_data, start, end))
        start, end = w.week_window(NEXT_WED)
        self.assertIsNone(w.load_liv_xs(repo_data, start, end))  # enabled: false


if __name__ == "__main__":
    unittest.main()
