"""Offline tests for cogs/media.py - nothing here touches the network.

Run from the repo root:  python -m unittest discover -s tests -v

Sonarr/Radarr are replaced by FakeArr, which records every call, so
the add / monitor / search paths are checked without adding anything
real or triggering a download.
"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cogs import media  # noqa: E402
from cogs.media import MediaRequest, parse_request  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeArr:
    """Stands in for media.Arr: canned GET answers, recorded writes."""

    def __init__(self, gets=None, fail=None):
        self.name = "Fake"
        self.gets = gets or {}
        self.fail = fail
        self.calls = []

    async def call(self, method, path, params=None, json=None):
        if self.fail:
            raise media.ArrError(self.fail)
        self.calls.append((method, path, params, json))
        if method == "GET":
            answer = self.gets.get(path, [])
            return answer(params) if callable(answer) else answer
        if method == "POST" and path in ("/series", "/movie"):
            return {**json, "id": 77}
        return {}

    def writes(self):
        return [(m, p, j) for m, p, _, j in self.calls if m != "GET"]

    def commands(self):
        return [j for m, p, j in self.writes() if p == "/command"]


SHOW = {"title": "Stranger Things", "year": 2016, "tvdbId": 305288,
        "seasons": [{"seasonNumber": n, "monitored": False} for n in range(0, 5)]}
MOVIE = {"title": "Dune", "year": 2021, "tmdbId": 438631}
TV_DEFAULTS = {"qualityProfileId": 4, "rootFolderPath": "/tv", "languageProfileId": None,
               "tags": [2], "seasonFolder": True, "seriesType": "standard",
               "monitorNewItems": "all"}
MOVIE_DEFAULTS = {"qualityProfileId": 6, "rootFolderPath": "/movies",
                  "minimumAvailability": "released", "tags": []}


def ep(id_, season, number, has_file=False, monitored=False, aired=True):
    return {"id": id_, "seasonNumber": season, "episodeNumber": number,
            "hasFile": has_file, "monitored": monitored,
            "airDateUtc": "2016-07-15T07:00:00Z" if aired else "2999-01-01T00:00:00Z"}


def in_library(monitored_seasons=(), monitored=True):
    return [{**SHOW, "id": 9, "monitored": monitored,
             "seasons": [{"seasonNumber": n, "monitored": n in monitored_seasons}
                         for n in range(0, 5)]}]


def episodes(eps):
    def answer(params):
        season = params.get("seasonNumber")
        return [e for e in eps if season is None or e["seasonNumber"] == season]
    return answer


class Parsing(unittest.TestCase):
    def test_the_four_forms(self):
        self.assertEqual(parse_request("Stranger Things"),
                         MediaRequest("Stranger Things"))
        self.assertEqual(parse_request("Stranger Things season 1"),
                         MediaRequest("Stranger Things", season=1))
        self.assertEqual(parse_request("Stranger Things S03E04"),
                         MediaRequest("Stranger Things", season=3, episode=4))
        self.assertEqual(parse_request("The Matrix"), MediaRequest("The Matrix"))

    def test_selector_variants(self):
        self.assertEqual(parse_request("stranger things s3e4"),
                         MediaRequest("stranger things", season=3, episode=4))
        self.assertEqual(parse_request("Stranger  Things Season 2 Episode 5"),
                         MediaRequest("Stranger Things", season=2, episode=5))
        self.assertEqual(parse_request("Stranger Things S2"),
                         MediaRequest("Stranger Things", season=2))
        self.assertEqual(parse_request("The Office (2005) season 3"),
                         MediaRequest("The Office", year=2005, season=3))

    def test_titles_that_look_like_selectors(self):
        for title in ("Season of the Witch", "Blade Runner 2049", "1917",
                      "Se7en", "Class of 1999", "(500) Days of Summer"):
            self.assertEqual(parse_request(title), MediaRequest(title))
        self.assertEqual(parse_request("Dune (2021)"), MediaRequest("Dune", year=2021))
        self.assertEqual(parse_request(""), MediaRequest(""))


class Matching(unittest.TestCase):
    def test_exact_beats_fuzzy_and_remakes_stay_ambiguous(self):
        hits = [{"title": "Dune", "year": 2021}, {"title": "Dune", "year": 1984},
                {"title": "Dune: Part Two", "year": 2024}]
        self.assertEqual(len(media.close_matches(hits, "dune", None)), 2)
        self.assertEqual(media.close_matches(hits, "Dune", 1984), [hits[1]])
        self.assertEqual(len(media.close_matches(hits, "dun", None)), 3)
        self.assertEqual(media.close_matches([], "zzqx-not-a-title", None), [])

    def test_seerr_defaults(self):
        servers = [
            {"isDefault": True, "is4k": True, "activeDirectory": "/4k"},
            {"isDefault": True, "is4k": False, "activeProfileId": 4,
             "activeDirectory": "/tv", "tags": [2], "enableSeasonFolders": True,
             "seriesType": "standard", "animeSeriesType": "anime",
             "activeAnimeProfileId": 8, "activeAnimeDirectory": "/anime",
             "monitorNewItems": "all", "minimumAvailability": "released"},
        ]
        server = media.seerr_default_server(servers)
        self.assertEqual(server["activeDirectory"], "/tv")
        self.assertEqual(media.tv_defaults(server, anime=False), TV_DEFAULTS)
        anime = media.tv_defaults(server, anime=True)
        self.assertEqual((anime["rootFolderPath"], anime["qualityProfileId"],
                          anime["seriesType"]), ("/anime", 8, "anime"))
        partial = media.tv_defaults({**server, "activeAnimeProfileId": None}, anime=True)
        self.assertEqual((partial["rootFolderPath"], partial["qualityProfileId"]),
                         ("/anime", 4))
        self.assertEqual(media.movie_defaults(server)["rootFolderPath"], "/tv")
        self.assertIsNone(media.seerr_default_server([servers[0]]))
        self.assertIsNone(media.env_defaults("NOT_SET_ANYWHERE"))


class NewShow(unittest.TestCase):
    def test_whole_show(self):
        arr = FakeArr()
        reply = run(media.request_tv(arr, SHOW, MediaRequest("x"), TV_DEFAULTS))
        (_, path, body), = arr.writes()
        self.assertEqual(path, "/series")
        self.assertEqual([s["monitored"] for s in body["seasons"]],
                         [False, True, True, True, True])  # specials stay off
        self.assertTrue(body["addOptions"]["searchForMissingEpisodes"])
        self.assertEqual((body["rootFolderPath"], body["qualityProfileId"], body["tags"]),
                         ("/tv", 4, [2]))
        self.assertIn("all seasons monitored", reply)

    def test_one_season(self):
        arr = FakeArr()
        reply = run(media.request_tv(arr, SHOW, MediaRequest("x", season=1), TV_DEFAULTS))
        (_, _, body), = arr.writes()
        self.assertEqual([s["seasonNumber"] for s in body["seasons"] if s["monitored"]], [1])
        self.assertTrue(body["addOptions"]["searchForMissingEpisodes"])
        self.assertIn("only season 1 monitored", reply)

    def test_one_episode(self):
        arr = FakeArr({"/episode": episodes([ep(31, 3, 3), ep(32, 3, 4)])})
        reply = run(media.request_tv(arr, SHOW, MediaRequest("x", season=3, episode=4),
                                     TV_DEFAULTS, tries=2, delay=0))
        add, command = arr.writes()
        self.assertFalse(any(s["monitored"] for s in add[2]["seasons"]))
        self.assertFalse(add[2]["addOptions"]["searchForMissingEpisodes"])
        self.assertEqual(add[2]["monitorNewItems"], "none")
        self.assertEqual(command[2], {"name": "EpisodeSearch", "episodeIds": [32]})
        self.assertIn("S03E04", reply)

    def test_episode_list_never_arrives(self):
        arr = FakeArr()
        reply = run(media.request_tv(arr, SHOW, MediaRequest("x", season=3, episode=4),
                                     TV_DEFAULTS, tries=2, delay=0))
        self.assertEqual(arr.commands(), [])
        self.assertIn("again in a minute", reply)

    def test_refuses_without_defaults_or_season(self):
        arr = FakeArr()
        self.assertEqual(run(media.request_tv(arr, SHOW, MediaRequest("x"), None)),
                         media.NO_DEFAULTS)
        reply = run(media.request_tv(arr, SHOW, MediaRequest("x", season=9), TV_DEFAULTS))
        self.assertIn("no season 9", reply)
        self.assertEqual(arr.writes(), [])


class ExistingShow(unittest.TestCase):
    def test_season_adds_monitoring_without_touching_others(self):
        arr = FakeArr({"/series": in_library(monitored_seasons=(2,)),
                       "/episode": episodes([ep(11, 1, 1), ep(12, 1, 2, has_file=True)])})
        reply = run(media.request_tv(arr, SHOW, MediaRequest("x", season=1), None))
        put, monitor, command = arr.writes()
        self.assertEqual(put[1], "/series/9")
        self.assertEqual([s["seasonNumber"] for s in put[2]["seasons"] if s["monitored"]],
                         [1, 2])
        self.assertEqual(monitor[2]["episodeIds"], [11, 12])
        self.assertEqual(command[2], {"name": "SeasonSearch", "seriesId": 9,
                                      "seasonNumber": 1})
        self.assertIn("season 1", reply)

    def test_whole_show_search(self):
        arr = FakeArr({"/series": in_library(),
                       "/episode": episodes([ep(1, 0, 1), ep(11, 1, 1), ep(21, 2, 1)])})
        run(media.request_tv(arr, SHOW, MediaRequest("x"), None))
        put, monitor, command = arr.writes()
        self.assertEqual([s["monitored"] for s in put[2]["seasons"]],
                         [False, True, True, True, True])
        self.assertEqual(monitor[2]["episodeIds"], [11, 21])  # not the special
        self.assertEqual(command[2], {"name": "SeriesSearch", "seriesId": 9})

    def test_already_downloaded_does_nothing(self):
        eps = [ep(11, 1, 1, has_file=True), ep(12, 1, 2, has_file=True),
               ep(13, 1, 3, aired=False)]
        for req in (MediaRequest("x"), MediaRequest("x", season=1),
                    MediaRequest("x", season=1, episode=2)):
            arr = FakeArr({"/series": in_library(), "/episode": episodes(eps)})
            reply = run(media.request_tv(arr, SHOW, req, None))
            self.assertIn("Already have", reply)
            self.assertEqual(arr.writes(), [])

    def test_single_episode_and_missing_episode(self):
        arr = FakeArr({"/series": in_library(), "/episode": episodes([ep(34, 3, 4)])})
        run(media.request_tv(arr, SHOW, MediaRequest("x", season=3, episode=4), None))
        self.assertEqual(arr.commands(), [{"name": "EpisodeSearch", "episodeIds": [34]}])
        self.assertIn(("PUT", "/episode/monitor", {"episodeIds": [34], "monitored": True}),
                      arr.writes())
        self.assertFalse(any(p.startswith("/series") for _, p, _ in arr.writes()))
        arr = FakeArr({"/series": in_library(), "/episode": episodes([ep(34, 3, 4)])})
        reply = run(media.request_tv(arr, SHOW, MediaRequest("x", season=3, episode=99), None))
        self.assertIn("no S03E99", reply)
        self.assertEqual(arr.writes(), [])


class Movies(unittest.TestCase):
    def test_new_movie(self):
        arr = FakeArr()
        reply = run(media.request_movie(arr, MOVIE, MOVIE_DEFAULTS))
        (_, path, body), = arr.writes()
        self.assertEqual(path, "/movie")
        self.assertEqual((body["tmdbId"], body["rootFolderPath"], body["qualityProfileId"],
                          body["monitored"], body["addOptions"]),
                         (438631, "/movies", 6, True, {"searchForMovie": True}))
        self.assertIn("Added", reply)
        self.assertEqual(run(media.request_movie(FakeArr(), MOVIE, None)), media.NO_DEFAULTS)

    def test_existing_movie(self):
        have = FakeArr({"/movie": [{**MOVIE, "id": 5, "hasFile": True, "monitored": True}]})
        self.assertIn("Already have", run(media.request_movie(have, MOVIE, None)))
        self.assertEqual(have.writes(), [])
        missing = FakeArr({"/movie": [{**MOVIE, "id": 5, "hasFile": False,
                                       "monitored": False}]})
        run(media.request_movie(missing, MOVIE, None))
        put, command = missing.writes()
        self.assertTrue(put[2]["monitored"])
        self.assertEqual(command[2], {"name": "MoviesSearch", "movieIds": [5]})

    def test_unreachable_surfaces_as_arr_error(self):
        down = FakeArr(fail="Radarr is unreachable from potofgreed.")
        with self.assertRaises(media.ArrError):
            run(media.request_movie(down, MOVIE, MOVIE_DEFAULTS))


class FakeCtx:
    """Just enough of commands.Context for the !request callback."""

    class _Typing:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    def __init__(self, author_id):
        self.guild = object()
        self.author = type("Author", (), {"id": author_id, "__str__": lambda s: "tester"})()
        self.replies = []

    def typing(self):
        return self._Typing()

    async def reply(self, content=None, view=None):
        self.replies.append((content, view))


class Command(unittest.TestCase):
    """The whole !request flow with Sonarr/Radarr swapped for fakes."""

    def ask(self, query, sonarr_hits=(), radarr_hits=(), author=1, fail=None):
        async def go():
            cog = media.Media.__new__(media.Media)
            cog.admin_ids, cog.prefix = {1}, "!"
            self.audits = []

            async def audit(_guild, text):
                self.audits.append(text)
            cog.audit = audit
            self.sonarr = FakeArr({"/series/lookup": list(sonarr_hits),
                                   "/episode": episodes([ep(34, 3, 4)])}, fail=fail)
            self.radarr = FakeArr({"/movie/lookup": list(radarr_hits)})
            self.asked = []

            async def service(name):
                self.asked.append(name)
                if name == "Sonarr":
                    return self.sonarr, {"isDefault": True, "activeProfileId": 4,
                                         "activeDirectory": "/tv"}
                return self.radarr, {"isDefault": True, "activeProfileId": 6,
                                     "activeDirectory": "/movies"}
            cog._service = service
            ctx = FakeCtx(author)
            await media.Media.request.callback(cog, ctx, query=query)
            return ctx.replies
        return run(go())

    def test_non_admin_gets_total_silence(self):
        self.assertEqual(self.ask("Stranger Things", [SHOW], author=2), [])
        self.assertEqual(self.asked, [])

    def test_bare_title_routes_to_sonarr_when_only_a_show_matches(self):
        (content, view), = self.ask("Stranger Things", [SHOW], [])
        self.assertIsNone(view)
        self.assertIn("all seasons monitored", content)
        self.assertEqual(self.radarr.writes(), [])
        self.assertEqual(len(self.audits), 1)

    def test_bare_title_routes_to_radarr_when_only_a_movie_matches(self):
        (content, view), = self.ask("dune", [], [MOVIE])
        self.assertIn("to Radarr", content)
        self.assertEqual(self.sonarr.writes(), [])
        self.assertEqual(self.radarr.writes()[0][1], "/movie")

    def test_selector_never_asks_radarr(self):
        (content, _), = self.ask("Stranger Things season 1", [SHOW], [MOVIE])
        self.assertEqual(self.asked, ["Sonarr"])
        self.assertIn("only season 1 monitored", content)
        (content, _), = self.ask("Stranger Things S03E04", [SHOW], [MOVIE])
        self.assertEqual(self.asked, ["Sonarr"])
        self.assertIn("S03E04", content)

    def test_show_and_movie_with_the_same_title_asks(self):
        film = {"title": "Stranger Things", "year": 2010, "tmdbId": 1}
        (content, view), = self.ask("Stranger Things", [SHOW], [film])
        self.assertIn("Which one", content)
        self.assertEqual([b.label for b in view.children],
                         ["Stranger Things (2016) - TV",
                          "Stranger Things (2010) - movie", "Cancel"])
        self.assertEqual(self.sonarr.writes() + self.radarr.writes(), [])

    def test_inexact_title_asks_instead_of_guessing(self):
        (content, view), = self.ask("stranger thing", [SHOW], [])
        self.assertIn("Which one", content)
        self.assertEqual(self.sonarr.writes(), [])

    def test_not_found_and_unreachable_and_usage(self):
        (content, _), = self.ask("zzqx not a real title 9931")
        self.assertIn("Couldn't find", content)
        (content, _), = self.ask("Dune", fail="Sonarr is unreachable from potofgreed.")
        self.assertEqual(content, "Sonarr is unreachable from potofgreed.")
        (content, _), = self.ask("")
        self.assertIn("Usage", content)


if __name__ == "__main__":
    unittest.main()
