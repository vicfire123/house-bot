"""Offline tests for the NVIDIA restore and the GTA VI Support switch
in cogs/updates.py (2026-11-24). Nothing here touches the network.

Run from the repo root:  python -m unittest discover -s tests -v

Fixtures live in tests/fixtures/. The GTA article and /VI page are
SYNTHETIC (built from the Nov 24 brief - see each file's header);
the NVIDIA JSON carries the values proven live on Nov 24.
"""

import asyncio
import copy
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIX = os.path.join(ROOT, "tests", "fixtures")

from cogs import updates  # noqa: E402


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def run(coro):
    return asyncio.run(coro)


def old_gta_watcher(html):
    """The pre-Nov 24 /VI watcher logic, copied verbatim from
    _feed_gta6 at 1f8a595 (GTA_URL inlined). Kept here only as the
    regression proof of why 01.002.002 went unreported."""
    gta_url = "https://www.rockstargames.com/VI"
    preorder = "Pre-Order Now" in html
    idx = html.find("Coming")
    if idx != -1:
        nearby = updates.DATE_RE.search(html[idx:idx + 200])
        if nearby:
            release = nearby.group(0)
            label = f"Official page: Coming {release}"
            if preorder:
                label += " - pre-orders open"
            return [updates._item(f"gta-{release}-{int(preorder)}",
                                  label, gta_url)]
    if ("Available Now" in html or "Out Now" in html
            or "Buy Now" in html):
        return [updates._item("gta-launched",
                              "GTA VI is out now - available on PS5",
                              gta_url)]
    return []


class GtaSupportTests(unittest.TestCase):
    def test_article_yields_nov23_item(self):
        items = updates.parse_gta_support(fixture("gta_support_article.html"))
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it["id"], "gta-patch-November 23, 2026")
        self.assertEqual(it["label"], "Patch notes updated - November 23, 2026")
        self.assertEqual(it["url"], updates.GTA_SUPPORT_URL)
        bullets = it["notes"].splitlines()
        self.assertEqual(len(bullets), 7)
        self.assertTrue(all(b.startswith("- ") for b in bullets))
        self.assertIn("deadlift bar", it["notes"])
        self.assertNotIn("Version", it["label"])  # Rockstar publishes none

    def test_multiple_entries_newest_first_and_same_date_merged(self):
        html = """
        <h3>[November 23, 2026] - General / Miscellaneous</h3><ul><li>A</li></ul>
        <h3>[November 23, 2026] - Online</h3><ul><li>B</li></ul>
        <h3>[November 30, 2026] - General / Miscellaneous</h3><ul><li>C</li></ul>
        """
        items = updates.parse_gta_support(html)
        self.assertEqual([i["id"] for i in items],
                         ["gta-patch-November 30, 2026",
                          "gta-patch-November 23, 2026"])
        self.assertEqual(items[1]["notes"], "- A\n- B")

    def test_notes_trimmed_to_embed_limit(self):
        lis = "".join(f"<li>{'x' * 90} {n}</li>" for n in range(40))
        html = f"<p>[November 23, 2026] - General</p><ul>{lis}</ul>"
        notes = updates.parse_gta_support(html)[0]["notes"]
        self.assertLessEqual(len(notes), 1024)
        self.assertTrue(notes.endswith("..."))

    def test_no_dated_sections_is_none_not_empty(self):
        cog = updates.Updates.__new__(updates.Updates)

        async def fake_fetch(url):
            return "<html><body>Rockstar Support - Access Denied</body></html>"
        cog._get_text_browser = fake_fetch
        self.assertIsNone(run(cog._feed_gta6()))

    def test_fetch_failure_is_none(self):
        cog = updates.Updates.__new__(updates.Updates)

        async def fake_fetch(url):
            return None
        cog._get_text_browser = fake_fetch
        self.assertIsNone(run(cog._feed_gta6()))

    def test_old_watcher_regression(self):
        """Post-launch /VI page -> the old watcher answers the id that
        was already stored, so the Nov 23 patch could never post."""
        items = old_gta_watcher(fixture("gta_vi_page_postlaunch.html"))
        self.assertEqual([i["id"] for i in items], ["gta-launched"])

    def test_browser_fetch_uses_chrome_impersonation_off_loop(self):
        cog = updates.Updates.__new__(updates.Updates)
        calls = {}

        class Resp:
            status_code = 200
            text = "ok"

        def fake_get(url, **kw):
            calls.update(kw, url=url)
            return Resp()
        with mock.patch("curl_cffi.requests.get", fake_get), \
                mock.patch("asyncio.to_thread", wraps=asyncio.to_thread) as tt:
            self.assertEqual(run(cog._get_text_browser("https://x")), "ok")
            self.assertTrue(tt.called)
        self.assertEqual(calls["impersonate"], "chrome")
        self.assertEqual(calls["timeout"], 30)


class NvidiaTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(fixture("nvidia_rtx_620.json"))

    def _with(self, **kw):
        d = copy.deepcopy(self.data)
        d["IDS"][0]["downloadInfo"].update(kw)
        return d

    def test_rtx_driver_release_620_posts(self):
        items = updates.parse_nvidia(self.data)
        self.assertEqual(items, [{
            "id": "nvidia-621.24",
            "label": "GeForce Game Ready Driver 621.24",
            "url": "https://www.nvidia.com/en-us/drivers/details/285521/",
            "extra": "Wed Nov 18, 2026"}])

    def test_geforce_name_still_posts(self):
        items = updates.parse_nvidia(
            self._with(Name="GeForce%20Game%20Ready%20Driver"))
        self.assertEqual(items[0]["id"], "nvidia-621.24")

    def test_wrong_family_is_silent(self):
        for name in ("Data%20Center%20Driver%20for%20Windows",
                     "NVIDIA%20RTX%20PRO%20Driver%20Release%20620",
                     "Quadro%20Desktop%20Driver",
                     "NVIDIA%20RTX%20Driver%20Release%20620%20Data%20Center"):
            self.assertEqual(updates.parse_nvidia(self._with(Name=name)), [],
                             name)

    def test_not_whql_or_beta_is_silent(self):
        self.assertEqual(updates.parse_nvidia(self._with(IsWHQL="0")), [])
        self.assertEqual(updates.parse_nvidia(self._with(IsBeta="1")), [])

    def test_empty_version_is_silent(self):
        self.assertEqual(updates.parse_nvidia(self._with(Version=" ")), [])

    def test_broken_payload_is_none(self):
        self.assertIsNone(updates.parse_nvidia({"IDS": []}))

    def test_url_uses_new_params(self):
        for p in ("psID=131", "pfID=1070", "osID=135"):
            self.assertIn(p, updates.NVIDIA_URL)


class EngineTests(unittest.TestCase):
    """Seed / catch-up behavior through the real _run_feed."""

    def _cog(self, state):
        cog = updates.Updates.__new__(updates.Updates)
        cog.state = state
        cog.health = {}
        cog._save = lambda: None
        cog._save_health = lambda: None
        cog.posted = []

        async def fake_post(feed, it, channel):
            cog.posted.append((feed["key"], it["id"]))
        cog._post = fake_post
        return cog

    def _feed(self, key):
        return next(f for f in updates.FEEDS if f["key"] == key)

    def test_registry(self):
        self.assertEqual(len(updates.FEEDS), 17)
        nv = self._feed("nvidia")
        self.assertEqual(nv, {"key": "nvidia", "name": "NVIDIA GeForce Drivers",
                              "emoji": "🟢", "color": 0x76B900,
                              "fetch": "_feed_nvidia"})
        gta = self._feed("gta6")
        self.assertEqual(gta["name"], "GTA VI (PS5)")
        self.assertNotIn("channel", gta)  # stays on #game-updates

    def test_nvidia_seeds_silently_at_62124(self):
        cog = self._cog({})
        data = json.loads(fixture("nvidia_rtx_620.json"))

        async def fake_json(url):
            return data
        cog._get_json = fake_json
        run(cog._run_feed(self._feed("nvidia"), channel=object()))
        self.assertEqual(cog.posted, [])
        self.assertEqual(cog.state["nvidia"]["id"], "nvidia-621.24")

    def test_gta_posts_nov23_once_from_gta_launched(self):
        cog = self._cog({"gta6": {"id": "gta-launched",
                                  "label": "GTA VI is out now - available on PS5"}})
        html = fixture("gta_support_article.html")

        async def fake_fetch(url):
            return html
        cog._get_text_browser = fake_fetch
        run(cog._run_feed(self._feed("gta6"), channel=object()))
        self.assertEqual(cog.posted, [("gta6", "gta-patch-November 23, 2026")])
        run(cog._run_feed(self._feed("gta6"), channel=object()))
        self.assertEqual(len(cog.posted), 1)  # no repost next poll


if __name__ == "__main__":
    unittest.main()
