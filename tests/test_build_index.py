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


def run(cap=bi.DEFAULT_CAP, youtube=None):
    youtube = youtube or FakeYouTube()
    quota = bi.Quota(cap)
    yt = bi.YouTube(FAKE_KEY, quota, get=youtube, pause=0)
    lines = []
    index = bi.build(yt, bi.EuroLeague(get=FakeEuroLeague(), pause=0), NOW, lines.append)
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
        self.assertEqual(len(ids), 6)

    def test_videos(self):
        statuses = bi.parse_videos(sample("videos.json"))
        self.assertEqual(len(statuses), 8)
        self.assertTrue(statuses["Vezenkov30a"]["embeddable"])
        self.assertNotIn("HezonjaGone", statuses)  # a deleted video is missing from items

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
                      "0 no made-for-kids answer, 0 other", lines)


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
        self.assertEqual(index["clips"], [  # newest first
            {"v": "PlayoffG2aa", "season": "E2025", "game": 301},
            {"v": "Vezenkov30a", "season": "E2025", "players": ["P006"]},
            {"v": "MikeJamesBB", "season": "E2025", "players": ["P009"]},
            {"v": "EfesMadRd2a", "season": "E2025", "game": 12},
            {"v": "DeColoMastr", "season": "E2025", "players": ["P013"]},
        ])
        self.assertEqual(quota.used, 4)  # channel 1 + two pages 2 + one videos.list 1
        self.assertEqual([m for m, _ in youtube.calls], ["channels", "playlistItems", "playlistItems", "videos"])
        self.assertNotIn("search", [m for m, _ in youtube.calls])
        playlist = youtube.calls[1][1]
        self.assertEqual(playlist["playlistId"], "UUGr3nR_XH9r6E5b09ZJAT9w")
        self.assertEqual(playlist["maxResults"], 50)

    def test_no_titles_descriptions_thumbnails_or_youtube_dates(self):
        index, _, _, _ = run()
        self.assertEqual(set(index), {"format", "fetched", "complete", "clips"})
        for c in index["clips"]:
            self.assertLessEqual(set(c), {"v", "season", "players", "game"})
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
        self.assertEqual([c["v"] for c in index["clips"]], ["PlayoffG2aa", "Vezenkov30a", "MikeJamesBB"])

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
