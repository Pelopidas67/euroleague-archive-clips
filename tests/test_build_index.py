"""Tests for build_index.py, offline, on samples in YouTube's documented response formats and in the
EuroLeague API's real formats. Run: python3 -m unittest discover -s tests (from clips-job/)."""

import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import build_index as bi  # noqa: E402

SAMPLES = os.path.join(HERE, "samples")
FAKE_KEY = "test-key-not-real-0123456789"
NOW = dt.datetime(2026, 10, 13, 4, 17, tzinfo=dt.timezone.utc)


def sample(name):
    with open(os.path.join(SAMPLES, name), encoding="utf-8") as f:
        return json.load(f)


def raw(name):
    with open(os.path.join(SAMPLES, name), "rb") as f:
        return f.read()


class FakeYouTube:
    """Answers like YouTube from the samples, and counts requests."""

    def __init__(self, pages=None, fail=None):
        self.calls = []
        self.pages = pages
        self.fail = fail or {}  # method -> (status, sample file)

    def __call__(self, method, params):
        self.calls.append((method, dict(params)))
        if method in self.fail:
            status, name = self.fail[method]
            return status, raw(name)
        if method == "channels":
            return 200, raw("channels.json")
        if method == "playlistItems":
            if self.pages is not None:
                n = int(params.get("pageToken", "0"))
                page = {"items": [], "nextPageToken": str(n + 1) if n + 1 < self.pages else None}
                return 200, json.dumps(page).encode()
            return 200, raw("playlistItems-page2.json" if params.get("pageToken") else "playlistItems-page1.json")
        if method == "videos":
            wanted = set(params["id"].split(","))
            all_items = sample("videos.json")["items"]
            return 200, json.dumps({"items": [i for i in all_items if i["id"] in wanted]}).encode()
        if method == "search":
            q = params["q"]
            if "Vezenkov" in q:
                return 200, raw("search-vezenkov.json")
            if "Mike James" in q:
                return 200, raw("search-mike-james.json")
            return 200, json.dumps({"kind": "youtube#searchListResponse", "items": []}).encode()
        raise AssertionError(method)


class FakeEuroLeague:
    def __init__(self):
        self.paths = []

    def __call__(self, path):
        self.paths.append(path)
        if path.endswith("/seasons"):
            return 200, raw("el-seasons.json")
        if path.endswith("/seasons/E2025/games"):
            return 200, raw("el-games-E2025.json")
        if "seasonCode=E2025" in path:
            return 200, raw("el-stats-E2025.json")
        return 200, json.dumps({"data": [], "players": []}).encode()


def run(cap=bi.DEFAULT_CAP, youtube=None, searches=0, state=None, now=NOW, state_out=None):
    youtube = youtube or FakeYouTube()
    quota = bi.Quota(cap, searches)
    yt = bi.YouTube(FAKE_KEY, quota, get=youtube, pause=0)
    lines = []
    index = bi.build(yt, bi.EuroLeague(get=FakeEuroLeague(), pause=0), now, lines.append, state, searches, state_out)
    return index, quota, youtube, lines


def season_2025():
    return bi.Season("E2025", "2025-26", bi.parse_time("2025-07-01T00:00:00"), bi.parse_time("2026-06-30T23:59:59"),
                     sample("el-games-E2025.json"), sample("el-stats-E2025.json"))


def video(title, published="2025-10-11T10:00:00Z", vid="AAAAAAAAAAA"):
    return {"id": vid, "title": title, "published": bi.parse_time(published)}


class ParsingTest(unittest.TestCase):
    def test_channel(self):
        c = bi.parse_channel(sample("channels.json"))
        self.assertEqual(c, {"id": "UCGr3nR_XH9r6E5b09ZJAT9w", "uploads": "UUGr3nR_XH9r6E5b09ZJAT9w",
                             "title": "EUROLEAGUE BASKETBALL", "handle": "@euroleague"})
        self.assertIsNone(bi.parse_channel({"items": []}))

    def test_playlist_pages(self):
        items, token = bi.parse_playlist_page(sample("playlistItems-page1.json"))
        self.assertEqual(token, "EAAaBlBUOkNESQ")
        self.assertEqual(len(items), 7)
        self.assertEqual(items[0]["id"], "PlayoffG2aa")
        self.assertEqual(items[0]["published"], dt.datetime(2026, 4, 23, 9, 0, tzinfo=dt.timezone.utc))
        items, token = bi.parse_playlist_page(sample("playlistItems-page2.json"))
        self.assertIsNone(token)  # the last page has no nextPageToken
        ids = [i["id"] for i in items]
        self.assertNotIn("PrivateItem", ids)  # a private entry in the playlist is skipped at once
        self.assertEqual(len(ids), 8)

    def test_videos(self):
        videos = bi.parse_videos(sample("videos.json"))
        self.assertEqual(len(videos), 16)
        self.assertTrue(videos["Vezenkov30a"]["status"]["embeddable"])
        self.assertNotIn("HezonjaGone", videos)  # a deleted video is missing from items
        self.assertEqual(videos["MvpVez00001"]["duration"], 87)
        self.assertEqual((videos["VezShort001"]["width"], videos["VezShort001"]["height"]), (203, 360))
        self.assertEqual(videos["SrchVezSh01"]["tags"], ["shorts", "basketball"])
        self.assertEqual(bi.parse_duration("PT2H3M4S"), 7384)
        self.assertEqual(bi.parse_duration("P0D"), 0)
        self.assertIsNone(bi.parse_duration("soon"))

    def test_error_reasons(self):
        self.assertEqual(bi.error_reasons(raw("error-quota.json")), ["quotaExceeded"])
        self.assertIn("API_KEY_INVALID", bi.error_reasons(raw("error-key.json")))
        self.assertTrue(bi.YouTubeError(403, ["quotaExceeded"]).quota)
        self.assertEqual(bi.error_reasons(b"not json"), [])


class ExclusionTest(unittest.TestCase):
    def test_only_public_embeddable_and_not_for_kids(self):
        ok = {"privacyStatus": "public", "uploadStatus": "processed", "embeddable": True, "madeForKids": False}
        self.assertTrue(bi.usable(ok))
        self.assertFalse(bi.usable({**ok, "embeddable": False}))
        self.assertFalse(bi.usable({**ok, "privacyStatus": "private"}))
        self.assertFalse(bi.usable({**ok, "privacyStatus": "unlisted"}))
        self.assertFalse(bi.usable({**ok, "madeForKids": True}))
        self.assertFalse(bi.usable({**ok, "selfDeclaredMadeForKids": True}))
        self.assertFalse(bi.usable({**ok, "uploadStatus": "uploaded"}))
        missing = dict(ok)
        del missing["madeForKids"]
        self.assertFalse(bi.usable(missing))  # no answer counts as no
        self.assertFalse(bi.usable(None))

    def test_the_index_leaves_them_out(self):
        index, _, _, lines = run()
        ids = [c["v"] for c in index["clips"]]
        for gone in ("NoEmbedRd4a", "LarkinPriv1", "LlullKids01", "HezonjaGone", "PrivateItem"):
            self.assertNotIn(gone, ids)
        self.assertIn("excluded: 0 not checked, 1 gone, 1 not public, 1 not embeddable, 1 made for kids, "
                      "0 no made-for-kids answer, 0 other, 1 Shorts", lines)
        self.assertIn("Shorts excluded this run: 1", lines)


class MatchingTest(unittest.TestCase):
    def setUp(self):
        self.s = season_2025()

    def test_player_clip_by_full_name_or_unique_surname(self):
        self.assertEqual(bi.match(video("Mike James buzzer-beater!"), self.s),
                         {"v": "AAAAAAAAAAA", "season": "E2025", "players": ["P009"]})
        self.assertEqual(bi.match(video("Vezenkov with the and-one"), self.s)["players"], ["P006"])
        self.assertEqual(bi.match(video("NANDO DE COLO masterclass"), self.s)["players"], ["P013"])
        self.assertEqual(bi.match(video("Otto Porter Jr. blocks it"), self.s)["players"], ["P014"])
        self.assertEqual(bi.match(video("Hezonja & Llull light it up"), self.s)["players"], ["P007", "P008"])

    def test_names_that_are_not_enough(self):
        self.assertIsNone(bi.match(video("Smith with the slam"), self.s))      # two players are called Smith
        self.assertIsNone(bi.match(video("James with the slam"), self.s))      # "James" alone: a common word
        self.assertIsNone(bi.match(video("Top 10 Plays: Vezenkov, Llull, Hezonja, Larkin"), self.s))
        self.assertIsNone(bi.match(video("EuroCup: Vezenkov dunk"), self.s))   # another competition
        self.assertIsNone(bi.match(video("Sasha Vezenkov EuroLeague Women"), self.s))

    def test_accents_and_case_do_not_matter(self):
        self.assertEqual(bi.match(video("VEZENKÓV for three!"), self.s)["players"], ["P006"])

    def test_game_by_clubs_and_round(self):
        e = bi.match(video("Anadolu Efes - Real Madrid | Highlights | Round 2", "2025-12-20T10:00:00Z"), self.s)
        self.assertEqual(e, {"v": "AAAAAAAAAAA", "season": "E2025", "game": 12})  # round given: any date
        e = bi.match(video("Real Madrid at Olympiacos, Rd 3", "2025-10-11T10:00:00Z"), self.s)
        self.assertEqual(e["game"], 25)
        e = bi.match(video("Fenerbahce Beko v Efes R4", "2025-10-18T10:00:00Z"), self.s)
        self.assertEqual(e["game"], 40)

    def test_game_by_date_without_a_round(self):
        e = bi.match(video("Olympiacos vs Real Madrid | Game 2 | Playoffs", "2026-04-23T09:00:00Z"), self.s)
        self.assertEqual(e["game"], 301)
        # Too long after any game between them, and no round: not linked to a game.
        self.assertIsNone(bi.match(video("Olympiacos vs Real Madrid classic", "2026-02-01T09:00:00Z"), self.s))
        # An unplayed game is never a match.
        self.assertIsNone(bi.match(video("Real Madrid v Olympiacos | Round 38", "2026-05-02T09:00:00Z"), self.s))

    def test_a_player_clip_in_a_game(self):
        e = bi.match(video("Vezenkov 30 points | Olympiacos - Real Madrid | Round 3"), self.s)
        self.assertEqual(e, {"v": "AAAAAAAAAAA", "season": "E2025", "players": ["P006"], "game": 25})

    def test_a_city_two_clubs_share_is_not_a_club(self):
        self.assertEqual(self.s.clubs_in(bi.normalize("Istanbul derby")), set())
        self.assertEqual(self.s.clubs_in(bi.normalize("Efes in Istanbul")), {"IST"})

    def test_season_of_a_video(self):
        seasons = {"E2025": {"start": bi.parse_time("2025-07-01T00:00:00"), "end": bi.parse_time("2026-06-30T23:59:59")},
                   "E2024": {"start": bi.parse_time("2024-07-01T00:00:00"), "end": bi.parse_time("2025-06-30T23:59:59")}}
        self.assertEqual(bi.season_for(video("x", "2025-10-01T00:00:00Z"), seasons), "E2025")
        self.assertEqual(bi.season_for(video("x", "2025-06-01T00:00:00Z"), seasons), "E2024")
        self.assertEqual(bi.season_for(video("Best of 2024-25", "2025-10-01T00:00:00Z"), seasons), "E2024")
        self.assertIsNone(bi.season_for(video("x", "1999-10-01T00:00:00Z"), seasons))


class IndexTest(unittest.TestCase):
    def test_whole_run_on_the_samples(self):
        index, quota, youtube, _ = run()
        self.assertEqual(index["format"], 1)
        self.assertEqual(index["fetched"], "2026-10-13T04:17:00Z")
        self.assertTrue(index["complete"])
        self.assertEqual(index["clips"], [  # newest first; the official channel's are marked so
            {"v": "MvpVez00001", "season": "E2025", "players": ["P006"], "kind": "award", "official": True},
            {"v": "PlayoffG2aa", "season": "E2025", "game": 301, "official": True},
            {"v": "Vezenkov30a", "season": "E2025", "players": ["P006"], "official": True},
            {"v": "MikeJamesBB", "season": "E2025", "players": ["P009"], "official": True},
            {"v": "EfesMadRd2a", "season": "E2025", "game": 12, "official": True},
            {"v": "DeColoMastr", "season": "E2025", "players": ["P013"], "official": True},
        ])
        self.assertEqual(quota.used, 4)  # channel 1 + two pages 2 + one videos.list 1
        self.assertEqual(quota.search_calls, 0)  # no searches unless asked for
        self.assertEqual([m for m, _ in youtube.calls], ["channels", "playlistItems", "playlistItems", "videos"])
        self.assertNotIn("search", [m for m, _ in youtube.calls])
        playlist = youtube.calls[1][1]
        self.assertEqual(playlist["playlistId"], "UUGr3nR_XH9r6E5b09ZJAT9w")
        self.assertEqual(playlist["maxResults"], 50)

    def test_no_titles_descriptions_thumbnails_or_youtube_dates(self):
        index, _, _, _ = run()
        self.assertEqual(set(index), {"format", "fetched", "complete", "clips"})
        for c in index["clips"]:
            self.assertLessEqual(set(c), {"v", "season", "players", "game", "kind", "official"})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "clips-index.json")
            bi.write_index(index, path)
            with open(path, encoding="utf-8") as f:
                text = f.read()
        lowered = text.lower()
        for c in index["clips"]:  # the samples' video IDs are made of words; IDs are allowed
            lowered = lowered.replace(c["v"].lower(), "")
        for word in ("title", "description", "thumbnail", "ytimg", "http", "publishedat", "channel",
                     "vezenkov", "real madrid", "highlights", "2026-04-23", "2025-10"):
            self.assertNotIn(word, lowered)
        for page in ("playlistItems-page1.json", "playlistItems-page2.json"):
            for item in sample(page)["items"]:
                self.assertNotIn(item["snippet"]["title"].lower(), lowered)
        self.assertLess(len(text), 1000)

    def test_rebuilt_from_scratch_each_run(self):
        first, _, _, _ = run()
        # The next day one clip can no longer be embedded: it is simply not in the new index.
        youtube = FakeYouTube()
        original = youtube.__call__

        def changed(method, params):
            status, body = original(method, params)
            if method == "videos":
                obj = json.loads(body)
                for i in obj["items"]:
                    if i["id"] == "MikeJamesBB":
                        i["status"]["embeddable"] = False
                body = json.dumps(obj).encode()
            return status, body
        quota = bi.Quota(bi.DEFAULT_CAP)
        second = bi.build(bi.YouTube(FAKE_KEY, quota, get=changed, pause=0),
                          bi.EuroLeague(get=FakeEuroLeague(), pause=0), NOW, lambda _: None)
        self.assertIn("MikeJamesBB", [c["v"] for c in first["clips"]])
        self.assertNotIn("MikeJamesBB", [c["v"] for c in second["clips"]])


class ShortsTest(unittest.TestCase):
    def details(self, vid):
        return bi.parse_videos(sample("videos.json"))[vid]

    def test_vertical_is_a_short(self):
        self.assertEqual(bi.shorts_reason(self.details("VezShort001")), "vertical video")

    def test_short_horizontal_clips_are_kept(self):
        # The 1:27 MVP video and a 58-second horizontal clip: the length alone never decides.
        self.assertIsNone(bi.shorts_reason(self.details("MvpVez00001")))
        self.assertIsNone(bi.shorts_reason(self.details("MikeJamesBB")))
        self.assertIsNone(bi.shorts_reason({"width": 640, "height": 360, "duration": 12, "title": "Dunk!"}))
        self.assertIsNone(bi.shorts_reason({"width": None, "height": None, "duration": 20, "title": "Dunk!"}))

    def test_hashtag_or_square_with_a_short_length(self):
        self.assertEqual(bi.shorts_reason(self.details("SrchVezSh01")), "square video of at most 3 minutes")
        no_size = {"width": None, "height": None, "duration": 40}
        self.assertEqual(bi.shorts_reason({**no_size, "title": "Wow #Shorts"}), "#shorts and at most 3 minutes")
        self.assertEqual(bi.shorts_reason({**no_size, "description": "more: #short"}), "#shorts and at most 3 minutes")
        self.assertEqual(bi.shorts_reason({**no_size, "tags": ["shorts"]}), "#shorts and at most 3 minutes")
        self.assertIsNone(bi.shorts_reason({**no_size, "title": "the #shortstop"}))
        self.assertIsNone(bi.shorts_reason({**no_size, "tags": ["basketball shorts"]}))
        # A 12-minute video with #shorts in its description is not a Short.
        self.assertIsNone(bi.shorts_reason(self.details("SrchVezLong")))

    def test_shorts_never_reach_the_index(self):
        index, _, _, lines = run(searches=2)
        ids = [c["v"] for c in index["clips"]]
        self.assertNotIn("VezShort001", ids)  # the official channel's
        self.assertNotIn("SrchVezSh01", ids)  # another channel's, from a search
        self.assertIn("Shorts excluded this run: 2", lines)


class RelevanceTest(unittest.TestCase):
    def setUp(self):
        self.careers = bi.careers({"E2025": season_2025()})
        self.vez = self.careers["P006"]
        self.mj = self.careers["P009"]

    def check(self, career, title, description=""):
        return bi.relevant({"title": title, "description": description}, career)

    def test_names_and_distinctiveness(self):
        self.assertEqual(self.vez.name, "Sasha Vezenkov")
        self.assertEqual(self.careers["P014"].name, "Otto Porter Jr.")
        self.assertTrue(self.vez.distinctive)
        self.assertFalse(self.mj.distinctive)                     # "James": a common surname
        self.assertFalse(self.careers["P011"].distinctive)        # two Smiths
        self.assertEqual(self.vez.clubs, ["OLY"])

    def test_kept(self):
        self.assertTrue(self.check(self.vez, "Sasha Vezenkov | Olympiacos Highlights 2025-26")[0])
        self.assertTrue(self.check(self.vez, "Vezenkov 30 points vs Real Madrid | Olympiacos")[0])
        self.assertTrue(self.check(self.vez, "Sasha Vezenkov named EuroLeague MVP")[0])
        self.assertTrue(self.check(self.vez, "Sasha Vezenkov dunks", "basketball highlights")[0])  # a name only he had
        self.assertTrue(self.check(self.mj, "Mike James 30 PTS | AS Monaco EuroLeague")[0])

    def test_rejected(self):
        self.assertEqual(self.check(self.vez, "Olympiacos highlights"), (False, "his surname is not in the title"))
        self.assertEqual(self.check(self.vez, "Sasha Vezenkov & family vlog", "our holidays"),
                         (False, "full name, but nothing about basketball"))
        # Same name, someone else: a comedian, and a basketball player who is not ours.
        self.assertFalse(self.check(self.mj, "Mike James - stand-up comedy special")[0])
        ok, reason = self.check(self.mj, "Mike James career highlights", "best dunks and buzzer beaters")
        self.assertFalse(ok)
        self.assertIn("shared or common", reason)
        self.assertFalse(self.check(self.vez, "Vezenkov dunk", "basketball")[0])  # surname only, no club
        self.assertFalse(self.check(self.vez, "Sasha Vezenkov EuroLeague Women")[0])

    def test_awards(self):
        for title in ("Sasha Vezenkov is the 2025-26 Turkish Airlines EuroLeague MVP", "Final Four MVP: Sloukas",
                      "Rising Star award", "All-EuroLeague First Team", "Alphonso Ford Trophy"):
            self.assertTrue(bi.is_award(title), title)
        for title in ("Top 10 plays", "Vezenkov 30 points", "Olympiacos win the title"):
            self.assertFalse(bi.is_award(title), title)

    def test_the_season_page(self):
        seasons = {"E2025": {"start": bi.parse_time("2025-07-01T00:00:00"), "end": bi.parse_time("2026-06-30T23:59:59")},
                   "E2024": {"start": bi.parse_time("2024-07-01T00:00:00"), "end": bi.parse_time("2025-06-30T23:59:59")}}

        def page(title, published):
            return bi.assign_season({"title": title, "published": bi.parse_time(published)}, self.vez, seasons)[0]
        self.assertEqual(page("Sasha Vezenkov 2025-26 highlights", "2026-09-01T00:00:00Z"), "E2025")  # the title's
        self.assertEqual(page("Sasha Vezenkov highlights", "2025-11-01T00:00:00Z"), "E2025")          # when published
        self.assertEqual(page("Sasha Vezenkov tribute", "2026-08-01T00:00:00Z"), "*")                 # after his last
        self.assertIsNone(page("Sasha Vezenkov Kings highlights", "2025-01-10T00:00:00Z"))            # not in the EL then
        self.assertIsNone(page("Sasha Vezenkov 2024-25 highlights", "2025-01-10T00:00:00Z"))          # a season he did not play


class SearchSpreadTest(unittest.TestCase):
    def test_priority_is_the_current_season_first_by_minutes(self):
        order = bi.priority({"E2025": season_2025()})
        self.assertEqual(order[:3], ["P006", "P009", "P007"])

    def test_due_players_and_daily_budget(self):
        order = [f"p{i:04d}" for i in range(500)]
        now = NOW
        today = now.date()
        plan = bi.plan_searches(order, {}, now, 4)
        self.assertEqual(plan, ["p0000", "p0001", "p0002", "p0003"])
        # Searched 24 days ago: not due; 25 days ago: due again.
        state = {"players": {"p0000": {"on": (today - dt.timedelta(days=24)).isoformat(), "clips": []},
                             "p0001": {"on": (today - dt.timedelta(days=25)).isoformat(), "clips": []}}}
        self.assertEqual(bi.plan_searches(order, state, now, 4), ["p0001", "p0002", "p0003", "p0004"])
        # Calls already used today (Pacific day) leave less; a new day starts again.
        day = bi.pacific_day(now)
        self.assertEqual(len(bi.plan_searches(order, {**state, "day": day, "calls": 3}, now, 4)), 1)
        self.assertEqual(len(bi.plan_searches(order, {**state, "day": "2000-01-01", "calls": 90}, now, 4)), 4)
        # Only as many players as can be kept fresh: per_day x REFRESH_DAYS.
        everyone_fresh = {"players": {c: {"on": today.isoformat(), "clips": []} for c in order[:100]}}
        self.assertEqual(bi.plan_searches(order, everyone_fresh, now, 4), [])  # 4 x 25 = 100 covered

    def test_results_are_dropped_before_30_days(self):
        today = NOW.date()
        state = {"players": {"old": {"on": (today - dt.timedelta(days=28)).isoformat(), "clips": [{"v": "x"}]},
                             "ok": {"on": (today - dt.timedelta(days=27)).isoformat(), "clips": [{"v": "y"}]}}}
        self.assertEqual(set(bi.age_state(state, NOW)["players"]), {"ok"})
        self.assertLess(bi.KEEP_DAYS, 30)
        self.assertLess(bi.REFRESH_DAYS, bi.KEEP_DAYS)

    def test_a_run_with_searches_and_the_next_days(self):
        state = {}
        index, quota, youtube, lines = run(searches=2, state_out=state)
        self.assertEqual(quota.search_calls, 2)
        searches = [p for m, p in youtube.calls if m == "search"]
        self.assertEqual([p["q"] for p in searches], ["Sasha Vezenkov EuroLeague", "Mike James EuroLeague"])
        for p in searches:
            self.assertEqual((p["type"], p["videoEmbeddable"], p["safeSearch"]), ("video", "true", "strict"))
        by_id = {c["v"]: c for c in index["clips"]}
        self.assertEqual(by_id["SrchVezHl01"], {"v": "SrchVezHl01", "season": "E2025", "players": ["P006"]})
        self.assertEqual(by_id["SrchVezMvp1"], {"v": "SrchVezMvp1", "season": "E2025", "players": ["P006"],
                                                "kind": "award", "official": True})
        self.assertIn("SrchVezRd03", by_id)
        self.assertIn("SrchVezLong", by_id)
        self.assertIn("SrchMJ00001", by_id)
        for gone in ("SrchVezNba1", "SrchVezVlog", "SrchMJcomic", "SrchMJnba01", "SrchVezSh01"):
            self.assertNotIn(gone, by_id)
        self.assertEqual(state["day"], bi.pacific_day(NOW))
        self.assertEqual(state["calls"], 2)
        self.assertEqual(set(state["players"]), {"P006", "P009"})
        self.assertNotIn("title", json.dumps(state).lower())
        # The next day: Vezenkov and James are not searched again; their clips stay; the next ones are.
        next_day = NOW + dt.timedelta(days=1)
        later = {}
        index2, quota2, youtube2, _ = run(searches=2, state=state, now=next_day, state_out=later)
        self.assertEqual([p["q"] for m, p in youtube2.calls if m == "search"],
                         ["Sergio Llull EuroLeague", "Mario Hezonja EuroLeague"])
        self.assertIn("SrchVezHl01", [c["v"] for c in index2["clips"]])
        # 25 days later Vezenkov is searched again; without that, his results go by 28 days.
        refresh = run(searches=2, state=later, now=NOW + dt.timedelta(days=25))[2]
        self.assertIn("Sasha Vezenkov EuroLeague", [p["q"] for m, p in refresh.calls if m == "search"])
        stale, _, _, _ = run(searches=0, state=later, now=NOW + dt.timedelta(days=28))
        self.assertNotIn("SrchVezHl01", [c["v"] for c in stale["clips"]])
        self.assertNotIn("SrchMJ00001", [c["v"] for c in stale["clips"]])

    def test_index_with_searches_has_no_titles(self):
        index, _, _, _ = run(searches=2)
        text = json.dumps(index).lower()
        for c in index["clips"]:
            text = text.replace(c["v"].lower(), "")
        for word in ("title", "description", "thumbnail", "ytimg", "hoops", "interview", "olympiacos", "monaco",
                     "channel", "highlights", "2026-05"):
            self.assertNotIn(word, text)

    def test_the_search_limit(self):
        self.assertEqual(bi.Quota(100, 500).search_cap, bi.SEARCH_CALLS_LIMIT - 5)
        self.assertLessEqual(bi.SEARCHES_PER_DAY + bi.SAMPLE_SEARCHES, bi.SEARCH_CALLS_LIMIT - 5)
        quota = bi.Quota(100, 1)
        quota.spend("search")
        with self.assertRaises(bi.QuotaCap):
            quota.spend("search")
        self.assertEqual(quota.used, 0)  # search calls are counted apart from the units

    def test_search_quota_used_up_keeps_the_rest(self):
        youtube = FakeYouTube(fail={"search": (403, "error-quota.json")})
        index, quota, _, lines = run(searches=2, youtube=youtube)
        self.assertTrue(any("stopped searching for today" in l for l in lines))
        self.assertIn("PlayoffG2aa", [c["v"] for c in index["clips"]])


class SampleModeTest(unittest.TestCase):
    def test_the_report_explains_every_decision(self):
        quota = bi.Quota(bi.DEFAULT_CAP, bi.SAMPLE_SEARCHES)
        youtube = FakeYouTube()
        yt = bi.YouTube(FAKE_KEY, quota, get=youtube, pause=0)
        lines = []
        report = bi.sample_report(yt, bi.EuroLeague(get=FakeEuroLeague(), pause=0), NOW, lines.append,
                                  [("P006", "ABC"), ("P009", "AB"), ("NOBODY", "A")])
        self.assertEqual(quota.search_calls, 5)
        self.assertIn("## Sasha Vezenkov (P006)", report)
        self.assertIn("### Variant A: `Sasha Vezenkov highlights`", report)
        self.assertIn("### Variant C: `Sasha Vezenkov Olympiacos basketball`", report)
        self.assertIn("| KEEP | full name, and EuroLeague or one of his clubs | Sasha Vezenkov / Olympiacos Highlights 2025-26 "
                      "| Hoops Greece | 6:10 | 640x360 | E2025 |", report)
        self.assertIn("reject | Shorts: square video of at most 3 minutes", report)
        self.assertIn("reject | published while he was not in the EuroLeague", report)
        self.assertIn("| yes | yes | https://www.youtube.com/watch?v=SrchVezMvp1 |", report)  # award, official
        self.assertIn("## Summary", report)
        self.assertNotIn(FAKE_KEY, report)
        # The log has counts only: no titles.
        log = "\n".join(lines)
        self.assertNotIn("Olympiacos Highlights", log)
        self.assertIn("player NOBODY is not in the EuroLeague data", log)
        self.assertTrue(any(l.startswith("sample: 5 searches") for l in lines))
        self.assertLessEqual(sum(len(v) for _, v in bi.SAMPLE_PLAYERS), bi.SAMPLE_SEARCHES)


class QuotaTest(unittest.TestCase):
    def test_the_cap_is_never_passed(self):
        # 10,000 pages of uploads (500,000 videos): the run stops at its share of the cap.
        youtube = FakeYouTube(pages=10_000)
        quota = bi.Quota(100)
        yt = bi.YouTube(FAKE_KEY, quota, get=youtube, pause=0)
        lines = []
        with self.assertRaises(RuntimeError):  # empty pages: no uploads, nothing to publish
            bi.build(yt, bi.EuroLeague(get=FakeEuroLeague(), pause=0), NOW, lines.append)
        self.assertLessEqual(quota.used, 100)
        self.assertEqual(quota.used, len(youtube.calls))  # every request was counted
        self.assertEqual(quota.used, 60)  # 60% of the cap for the uploads
        self.assertTrue(any("stopped reading uploads at the cap" in l for l in lines))

    def test_a_small_cap_gives_an_incomplete_index(self):
        index, quota, youtube, _ = run(cap=4)  # uploads may use 2: the channel and the first page
        self.assertFalse(index["complete"])
        self.assertLessEqual(quota.used, 4)
        self.assertEqual([c["v"] for c in index["clips"]], ["PlayoffG2aa", "Vezenkov30a", "MikeJamesBB"])  # page 1 only

    def test_the_cap_cannot_be_set_near_the_daily_limit(self):
        self.assertEqual(bi.Quota(9_999).cap, bi.HARD_CAP)
        self.assertLessEqual(bi.HARD_CAP, bi.DAILY_LIMIT // 2)
        self.assertLessEqual(bi.DEFAULT_CAP, bi.DAILY_LIMIT // 4)

    def test_quota_used_up_before_any_check_publishes_nothing(self):
        youtube = FakeYouTube(fail={"videos": (403, "error-quota.json")})
        # Matched videos that could not be checked are never published; with none checked at all,
        # nothing is published (the last index stays until the app finds it too old).
        with self.assertRaises(RuntimeError):
            run(youtube=youtube)

    def test_a_rejected_key_publishes_nothing(self):
        youtube = FakeYouTube(fail={"channels": (400, "error-key.json")})
        with self.assertRaises(bi.YouTubeError) as e:
            run(youtube=youtube)
        self.assertEqual(e.exception.status, 400)
        self.assertNotIn(FAKE_KEY, str(e.exception))


class SecrecyTest(unittest.TestCase):
    def test_the_key_never_reaches_the_log_or_the_index(self):
        index, _, _, lines = run()
        self.assertNotIn(FAKE_KEY, "\n".join(lines))
        self.assertNotIn(FAKE_KEY, json.dumps(index))

    def test_main_without_a_key_stops_at_once(self):
        env = dict(os.environ)
        os.environ.pop("YOUTUBE_API_KEY", None)
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(bi.main(["--out", os.devnull]), 2)
        finally:
            os.environ.clear()
            os.environ.update(env)

    def test_the_key_is_only_a_query_parameter_of_the_real_request(self):
        seen = {}

        def fake_http_get(url, headers, timeout=30):
            seen["url"] = url
            return 200, raw("channels.json")
        original = bi.http_get
        bi.http_get = fake_http_get
        try:
            yt = bi.YouTube(FAKE_KEY, bi.Quota(10), pause=0)
            yt.list("channels", {"part": "snippet", "id": bi.CHANNEL_ID})
        finally:
            bi.http_get = original
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen["url"]).query)
        self.assertEqual(query["key"], [FAKE_KEY])
        self.assertTrue(seen["url"].startswith("https://www.googleapis.com/youtube/v3/channels?"))


if __name__ == "__main__":
    unittest.main()
