#!/usr/bin/env python3
"""Builds the clips index for EuroLeague Archive.

Run by GitHub Actions once a day (see .github/workflows/clips.yml). It:

1. reads the uploads playlist of the official EuroLeague Basketball YouTube channel with
   playlistItems.list (1 quota unit per 50 videos);
2. reads the EuroLeague data it needs from the public EuroLeague API (the same endpoints the app
   uses): the seasons, each season's games and each season's players;
3. matches each upload to our data: a player clip by the player's name in the title, a game's
   highlights by the two clubs (and the round when the title has one) in that season;
4. searches YouTube for players (search.list: any channel, embeddable videos, safe search), a few
   dozen players a day in priority order (the current season first), each searched again within 25
   days; a result is kept only when it is clearly about that player (his name in the title and
   EuroLeague, one of his clubs or basketball words) and is put on the season page it belongs to;
   award videos (MVP, Rising Star, All-EuroLeague...) are marked as such;
5. checks every video with videos.list (1 unit per 50): only public, embeddable videos that are not
   "made for kids" and are not YouTube Shorts are kept (see shorts_reason);
6. writes the index: video IDs, our season, player and game codes, "award" and "official" marks, and
   the date fetched. No YouTube titles, descriptions, thumbnails or dates are ever written. The
   search state (which players were searched when, and the IDs found) goes to its own file.

The whole index is rebuilt on every run, so a video that was deleted, made private or can no
longer be embedded drops out at the next run (YouTube's rule: stored API data is refreshed at
least every 30 days; the app ignores an index older than 30 days).

The YouTube API key comes only from the environment variable YOUTUBE_API_KEY (a GitHub Actions
secret). It is never printed: no request URL is ever printed or put in an error message.

Only the Python standard library is used.

The YouTube parsing below follows YouTube's documented response formats
(developers.google.com/youtube/v3/docs: channels.list, playlistItems.list, videos.list,
search.list). channels, playlistItems and videos (status) were confirmed by the first real run on
14 October 2026; search.list and videos.list's snippet, contentDetails and player parts are
UNVERIFIED until the next one.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# --- Settings --------------------------------------------------------------------------------------

# The official EuroLeague Basketball channel (youtube.com/@euroleague, "EUROLEAGUE BASKETBALL").
CHANNEL_ID = "UCGr3nR_XH9r6E5b09ZJAT9w"
CHANNEL_HANDLE = "@euroleague"

YOUTUBE_API = "https://www.googleapis.com/youtube/v3/"
EUROLEAGUE_API = "https://api-live.euroleague.net"
COMPETITION = "E"

DAILY_LIMIT = 10_000       # YouTube's default daily quota for a project
DEFAULT_CAP = 2_000        # this job stops well below it (20%)
HARD_CAP = 5_000           # the cap can never be set above half the daily limit
UPLOADS_SHARE = 0.6        # of the cap, for reading the uploads; the rest checks the matches
COST = {"channels": 1, "playlistItems": 1, "videos": 1}  # documented units per list request
PAGE = 50                  # the most items one list request returns

# Player searches (search.list). Google limits search.list to 100 calls a day, apart from the
# 10,000 units of everything else. The daily run uses at most SEARCHES_PER_DAY of them, the sample
# mode at most SAMPLE_SEARCHES, so both on the same day still stay below 100.
SEARCH_CALLS_LIMIT = 100   # Google's daily limit for search.list calls
SEARCHES_PER_DAY = 65      # the daily run's share (CLIPS_SEARCHES_PER_DAY can lower it)
SAMPLE_SEARCHES = 25       # the sample mode's share
REFRESH_DAYS = 25          # a searched player is searched again after this many days...
KEEP_DAYS = 28             # ...and his search results are dropped after this many (YouTube: 30)
SEARCH_RESULTS = 25        # maxResults of one search
MAX_SEARCH_CLIPS = 12      # clips kept per player from one search
DEFAULT_QUERY = "B"        # the query variant the daily run uses (see QUERY_VARIANTS)
SHORT_MAX_SECONDS = 180    # YouTube Shorts are at most 3 minutes long

INDEX_FORMAT = 1
MAX_PLAYERS_PER_CLIP = 3   # a title naming more players is a compilation, not a player's clip
USER_AGENT = "EuroLeagueArchive-clips-job/1 (+https://github.com)"

# Titles about other competitions on the same channel.
OTHER_COMPETITIONS = ("eurocup", "next generation", "angt", "women", "u18", "junior", "youth")


class QuotaCap(Exception):
    """The next request would go over this run's cap."""


class YouTubeError(Exception):
    """YouTube answered with an error. `reasons` are YouTube's documented error reasons."""

    def __init__(self, status: int, reasons: list[str]):
        super().__init__(f"YouTube answered HTTP {status} ({', '.join(reasons) or 'no reason given'})")
        self.status = status
        self.reasons = reasons

    @property
    def quota(self) -> bool:
        return any(r in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded") for r in self.reasons)


# --- Text ------------------------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Lower case, no accents, only letters and digits separated by single spaces."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def contains_phrase(normalized_text: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {normalized_text} "


def parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# --- YouTube responses (documented formats; UNVERIFIED until the first real run) -----------------

def parse_channel(obj: dict) -> dict | None:
    """channels.list, part=snippet,contentDetails:
    { "items": [ { "id": "UC…", "snippet": { "title", "customUrl": "@handle", … },
                   "contentDetails": { "relatedPlaylists": { "uploads": "UU…" } } } ] }"""
    items = obj.get("items") or []
    if not items:
        return None
    item = items[0]
    snippet = item.get("snippet") or {}
    uploads = ((item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    if not item.get("id") or not uploads:
        return None
    return {"id": item["id"], "uploads": uploads, "title": snippet.get("title", ""),
            "handle": snippet.get("customUrl", "")}


def parse_playlist_page(obj: dict) -> tuple[list[dict], str | None]:
    """playlistItems.list, part=snippet,contentDetails,status:
    { "nextPageToken": "…", "pageInfo": { "totalResults", "resultsPerPage" },
      "items": [ { "snippet": { "publishedAt", "title", "resourceId": { "kind": "youtube#video",
                                "videoId" }, … },
                   "contentDetails": { "videoId", "videoPublishedAt" },
                   "status": { "privacyStatus" } } ] }
    Returns [{"id", "title", "published"}] (the title only for matching; it is never stored)."""
    out = []
    for item in obj.get("items") or []:
        snippet = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        resource = snippet.get("resourceId") or {}
        video_id = details.get("videoId") or resource.get("videoId")
        if not video_id or (resource.get("kind") not in (None, "youtube#video")):
            continue
        privacy = (item.get("status") or {}).get("privacyStatus")
        if privacy is not None and privacy != "public":
            continue  # "Private video" / "Deleted video" entries
        published = parse_time(details.get("videoPublishedAt")) or parse_time(snippet.get("publishedAt"))
        if published is None:
            continue
        out.append({"id": video_id, "title": snippet.get("title", ""), "published": published})
    return out, obj.get("nextPageToken") or None


def parse_duration(value: str | None) -> int | None:
    """ISO 8601 durations as videos.list gives them ("PT1M27S", "PT2H3M", "P0D") -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value or "")
    if not m or not value:
        return None
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def parse_videos(obj: dict) -> dict[str, dict]:
    """videos.list, part=snippet,status,contentDetails,player with maxWidth:
    { "items": [ { "id": "…",
                   "snippet": { "channelId", "channelTitle", "title", "description", "tags": [] },
                   "status": { "uploadStatus", "privacyStatus", "embeddable", "madeForKids", … },
                   "contentDetails": { "duration": "PT1M27S", … },
                   "player": { "embedHtml", "embedWidth", "embedHeight" } } ] }
    embedWidth / embedHeight are documented as returned when the request gives maxWidth or
    maxHeight; they follow the video's aspect ratio. A deleted video is missing from "items".
    The title, description and tags are used here only to spot Shorts, never written to the index."""
    out = {}
    for item in obj.get("items") or []:
        if not item.get("id"):
            continue
        snippet = item.get("snippet") or {}
        player = item.get("player") or {}

        def number(v):
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        out[item["id"]] = {
            "status": item.get("status") or {},
            "duration": parse_duration((item.get("contentDetails") or {}).get("duration")),
            "width": number(player.get("embedWidth")),
            "height": number(player.get("embedHeight")),
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "tags": snippet.get("tags") or [],
            "channel": snippet.get("channelId", ""),
            "channel_title": snippet.get("channelTitle", ""),
        }
    return out


SHORTS_TAG = re.compile(r"(?<![\w#])#shorts?\b", re.IGNORECASE)


def shorts_reason(video: dict | None) -> str | None:
    """Why a video counts as a YouTube Short, or None. Only documented data, several signals
    together; the duration alone never decides (a 1:27 horizontal MVP video is kept):
    - vertical: the embedded player is taller than wide (player.embedHeight > embedWidth);
    - square and at most 3 minutes long;
    - "#shorts" / "#short" in the title, description or tags, and at most 3 minutes long (or of
      unknown length)."""
    if not video:
        return None
    w, h, seconds = video.get("width"), video.get("height"), video.get("duration")
    short_enough = seconds is None or seconds <= SHORT_MAX_SECONDS
    if w and h and h > w:
        return "vertical video"
    if w and h and h == w and seconds is not None and seconds <= SHORT_MAX_SECONDS:
        return "square video of at most 3 minutes"
    tags = " ".join(f"#{t.lstrip('#')}" for t in video.get("tags") or [])
    text = " ".join([video.get("title", ""), video.get("description", ""), tags])
    if SHORTS_TAG.search(text) and short_enough:
        return "#shorts and at most 3 minutes"
    return None


def usable(status: dict | None) -> bool:
    """Public, finished uploading, embeddable and not made for kids. Anything missing counts as no."""
    if not status:
        return False
    return (status.get("privacyStatus") == "public"
            and status.get("uploadStatus", "processed") == "processed"
            and status.get("embeddable") is True
            and status.get("madeForKids") is False
            and status.get("selfDeclaredMadeForKids", False) is not True)


def error_reasons(body: bytes) -> list[str]:
    """{ "error": { "code", "message", "errors": [ { "reason" } ], "details": [ { "reason" } ] } }"""
    try:
        error = json.loads(body.decode("utf-8", "replace")).get("error") or {}
    except (ValueError, AttributeError):
        return []
    reasons = [e.get("reason", "") for e in error.get("errors") or []]
    reasons += [d.get("reason", "") for d in error.get("details") or [] if isinstance(d, dict)]
    return [r for r in reasons if r]


# --- EuroLeague data -------------------------------------------------------------------------------

GENERIC_WORDS = {
    "basket", "basketball", "club", "team", "sport", "sports", "real", "united", "city", "the",
    "fc", "bc", "kk", "bk", "cb", "sad", "sk", "pbc", "cska", "and", "euroleague",
}
EXTRA_CLUB_NAMES = {   # names titles use that the API's club names do not contain
    "RED": ["red star"], "MIL": ["olimpia milano", "milano", "olimpia"], "ULK": ["fener"],
    "BAR": ["barca"], "IST": ["efes"], "PAN": ["pao"], "TEL": ["maccabi tel aviv"],
}
TITLE_WORDS = {  # words often in titles: never a player's name on their own
    "best", "top", "play", "plays", "game", "games", "final", "four", "round", "week", "night",
    "highlights", "highlight", "dunk", "dunks", "block", "blocks", "assist", "assists", "shot",
    "shots", "buzzer", "beater", "magic", "king", "show", "career", "season", "playoffs", "points",
    "rebounds", "crossover", "steal", "mvp", "rising", "star", "stars", "coach", "award", "awards",
    "moments", "moment", "clutch", "win", "wins", "big", "great", "little", "young", "long", "love",
    "rice", "price", "hall", "wood", "black", "white", "brown", "green", "rivers", "james", "jordan",
}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def natural_name(api_name: str) -> str:
    """"VEZENKOV, SASHA" -> "Sasha Vezenkov"; "PORTER JR., OTTO" -> "Otto Porter Jr."."""
    last, _, first = api_name.partition(",")

    def nice(words: str) -> str:
        return " ".join(w[:1].upper() + w[1:].lower() if not w.isdigit() else w for w in words.split())
    return f"{nice(first)} {nice(last)}".strip()


class Season:
    """One season of our data: its clubs (with the names titles may use), games and players."""

    def __init__(self, code: str, alias: str, start: dt.datetime | None, end: dt.datetime | None,
                 games_json: dict, stats_json: dict):
        self.code = code
        self.alias = alias
        self.start = start
        self.end = end
        self.games = []      # {"code", "round", "date", "clubs": {local, road}}
        clubs = {}           # code -> set of names
        self.club_short: dict[str, str] = {}  # code -> the club's short name ("Olympiacos", "Real Madrid")
        for g in games_json.get("data") or []:
            local = ((g.get("local") or {}).get("club") or {})
            road = ((g.get("road") or {}).get("club") or {})
            if not local.get("code") or not road.get("code") or not g.get("played", False):
                continue
            for c in (local, road):
                short, full = c.get("abbreviatedName") or "", c.get("name") or ""
                if len(short) <= 4 and full.startswith(short + " ") and full.count(" ") == 1:
                    short = full  # "Real" -> "Real Madrid"
                self.club_short.setdefault(c["code"], short or full)
                names = clubs.setdefault(c["code"], set())
                for key in ("name", "abbreviatedName", "editorialName"):
                    if c.get(key):
                        names.add(normalize(c[key]))
            self.games.append({"code": int(g["gameCode"]), "round": g.get("round"),
                               "date": parse_time(g.get("utcDate")),
                               "clubs": frozenset((local["code"], road["code"]))})
        self.club_names = self._club_aliases(clubs)
        self.players = self._players(stats_json)

    @staticmethod
    def _club_aliases(clubs: dict[str, set[str]]) -> dict[str, list[str]]:
        word_owner: dict[str, set[str]] = {}
        for code, names in clubs.items():
            for name in names:
                for w in name.split():
                    word_owner.setdefault(w, set()).add(code)
        out = {}
        for code, names in clubs.items():
            aliases = {n for n in names if n not in GENERIC_WORDS}
            aliases.update(normalize(x) for x in EXTRA_CLUB_NAMES.get(code, []))
            for name in names:
                for w in name.split():
                    if len(w) >= 4 and not w.isdigit() and w not in GENERIC_WORDS and word_owner[w] == {code}:
                        aliases.add(w)
            out[code] = sorted(aliases, key=len, reverse=True)
        return out

    @staticmethod
    def _players(stats_json: dict) -> list[dict]:
        # v3 season stats: { "players": [ { "player": { "code", "name": "LAST, FIRST", … } } ] }
        seen = {}
        for row in stats_json.get("players") or []:
            p = row.get("player") or {}
            code, name = p.get("code"), p.get("name") or ""
            if not code or code in seen:
                continue
            last, _, first = name.partition(",")
            last_words = [w for w in normalize(last).split() if w not in NAME_SUFFIXES]
            team = (p.get("team") or {}).get("code") or ""
            try:
                minutes = float(row.get("minutesPlayed") or 0)
            except (TypeError, ValueError):
                minutes = 0.0
            seen[code] = {"code": code, "first": normalize(first), "last": " ".join(last_words),
                          "natural": natural_name(name), "clubs": [c for c in team.split(";") if c],
                          "minutes": minutes}
        players = list(seen.values())
        # A surname alone identifies a player only if no other player of the season has that name
        # (as surname or first name), it is not a word titles use anyway, and it is long enough.
        counts: dict[str, int] = {}
        for p in players:
            for part in {p["last"], p["first"]}:
                if part:
                    counts[part] = counts.get(part, 0) + 1
        for p in players:
            last = p["last"]
            p["full"] = f"{p['first']} {last}".strip() if p["first"] else ""
            p["last_alone"] = (len(last.replace(" ", "")) >= 4 and counts.get(last, 0) == 1
                               and last not in TITLE_WORDS)
        return players

    def clubs_in(self, title: str) -> set[str]:
        found = set()
        for code, names in self.club_names.items():
            if any(contains_phrase(title, n) for n in names):
                found.add(code)
        return found

    def players_in(self, title: str, clubs_found: set[str]) -> list[str]:
        club_words = {w for c in clubs_found for n in self.club_names[c] for w in n.split()}
        found = []
        for p in self.players:
            if p["full"] and contains_phrase(title, p["full"]):
                found.append(p["code"])
            elif p["last_alone"] and p["last"] not in club_words and contains_phrase(title, p["last"]):
                found.append(p["code"])
        return found

    def game_for(self, title: str, published: dt.datetime, clubs_found: set[str]) -> int | None:
        if len(clubs_found) < 2:
            return None
        candidates = [g for g in self.games if g["clubs"] <= clubs_found]
        if not candidates:
            return None
        m = re.search(r"\b(?:round|rd|r)\s?(\d{1,2})\b", title)
        if m:
            by_round = [g for g in candidates if g["round"] == int(m.group(1))]
            if len(by_round) == 1:
                return by_round[0]["code"]
        # No round (playoffs, Final Four) or no game in that round: the game that ended shortly
        # before the video was published (a highlights video comes within a few days).
        near = [g for g in candidates if g["date"] and
                dt.timedelta(hours=-12) <= published - g["date"] <= dt.timedelta(days=5)]
        if len(near) >= 1:
            near.sort(key=lambda g: published - g["date"])
            return near[0]["code"]
        return None


def season_alias_in(title: str, seasons: dict[str, dict]) -> str | None:
    """A title that names its season ("2024-25", "2024/25", "2024 25") belongs to it."""
    for m in re.finditer(r"\b(19|20)(\d\d) (\d\d)\b", title):
        first, second = int(m.group(1) + m.group(2)), int(m.group(3))
        if (first + 1) % 100 == second:
            code = f"{COMPETITION}{first}"
            if code in seasons:
                return code
    return None


def season_for(video: dict, seasons: dict[str, dict]) -> str | None:
    title = normalize(video["title"])
    named = season_alias_in(title, seasons)
    if named:
        return named
    for code, s in seasons.items():
        if s["start"] and s["end"] and s["start"] <= video["published"] <= s["end"]:
            return code
    return None


def match(video: dict, season: Season) -> dict | None:
    """The index entry for a video, or None if it is not a player clip or a game's highlights."""
    title = normalize(video["title"])
    if any(contains_phrase(title, w) for w in OTHER_COMPETITIONS):
        return None
    clubs = season.clubs_in(title)
    players = season.players_in(title, clubs)
    game = season.game_for(title, video["published"], clubs)
    if len(players) > MAX_PLAYERS_PER_CLIP:
        players = []
        if game is None:
            return None
    if not players and game is None:
        return None
    entry = {"v": video["id"], "season": season.code}
    if players:
        entry["players"] = sorted(players)
    if game is not None:
        entry["game"] = game
    return entry


# --- Player searches -------------------------------------------------------------------------------

# The query variants (the sample mode compares them; the daily run uses DEFAULT_QUERY).
QUERY_VARIANTS = {
    "A": "{name} highlights",
    "B": "{name} EuroLeague",
    "C": "{name} {club} basketball",
}
STRONG_CONTEXT = ("euroleague", "euro league", "final four", "turkish airlines")
WEAK_CONTEXT = ("basketball", "basket", "highlights", "highlight", "mvp", "dunk", "dunks", "points",
                "pts", "rebounds", "assists", "buzzer", "game winner", "crossover", "nba", "fiba",
                "eurobasket", "acb", "liga endesa", "career", "mix", "mixtape", "best plays")
AWARD_PHRASES = ("mvp", "most valuable player", "rising star", "all euroleague", "best defender",
                 "defensive player of the year", "sixth man", "top scorer", "alphonso ford",
                 "award", "trophy", "of the decade", "greatest players", "hall of fame")
NOT_HIM = ("women", "womens", "wnba", "u18", "u16", "u20", "high school")
COMMON_SURNAMES = {
    "smith", "johnson", "williams", "brown", "jones", "james", "miller", "davis", "wilson",
    "anderson", "taylor", "thomas", "moore", "jackson", "white", "harris", "martin", "thompson",
    "garcia", "martinez", "robinson", "clark", "rodriguez", "lewis", "lee", "walker", "hall", "allen",
    "young", "king", "wright", "scott", "green", "baker", "adams", "nelson", "hill", "campbell",
    "mitchell", "roberts", "carter", "phillips", "evans", "turner", "parker", "collins", "edwards",
    "stewart", "morris", "murphy", "cook", "rogers", "morgan", "cooper", "peterson", "reed", "bailey",
    "bell", "kelly", "howard", "ward", "cox", "richardson", "wood", "watson", "brooks", "bennett",
    "gray", "hughes", "price", "sanders", "myers", "long", "ross", "foster", "gonzalez", "lopez",
    "perez", "sanchez", "hernandez", "fernandez", "diaz", "torres", "ramirez", "silva", "santos",
    "jovanovic", "petrovic", "nikolic", "markovic", "popovic", "papadopoulos", "kim", "wang",
}


def parse_search(obj: dict) -> list[dict]:
    """search.list, part=snippet, type=video:
    { "items": [ { "id": { "kind": "youtube#video", "videoId" },
                   "snippet": { "publishedAt", "channelId", "title", "description",
                                "channelTitle", … } } ] }
    HTML entities in titles (&amp; &#39; &quot;) are decoded for the checks."""
    import html
    out = []
    for item in obj.get("items") or []:
        vid = (item.get("id") or {}).get("videoId")
        if not vid:
            continue  # a channel or playlist result
        sn = item.get("snippet") or {}
        published = parse_time(sn.get("publishedAt"))
        if published is None:
            continue
        out.append({"id": vid, "title": html.unescape(sn.get("title", "")),
                    "description": html.unescape(sn.get("description", "")),
                    "channel": sn.get("channelId", ""), "channel_title": html.unescape(sn.get("channelTitle", "")),
                    "published": published})
    return out


class Career:
    """One player across our seasons: what the searches and their checks need."""

    def __init__(self, code: str):
        self.code = code
        self.name = ""          # "Sasha Vezenkov"
        self.first = ""         # normalized
        self.last = ""
        self.seasons: list[str] = []   # season codes he played, oldest first
        self.clubs: list[str] = []     # club codes, most minutes first
        self.club_names: set[str] = set()
        self.minutes = 0.0
        self.distinctive = False  # a surname no other EuroLeague player had, and not a common one

    @property
    def full(self) -> str:
        return f"{self.first} {self.last}".strip()


def careers(data: dict[str, Season]) -> dict[str, Career]:
    out: dict[str, Career] = {}
    club_minutes: dict[str, dict[str, float]] = {}
    surname_owners: dict[str, set[str]] = {}
    for code in sorted(data):
        season = data[code]
        for p in season.players:
            c = out.setdefault(p["code"], Career(p["code"]))
            c.name, c.first, c.last = p["natural"], p["first"], p["last"]  # the latest spelling
            if code not in c.seasons:
                c.seasons.append(code)
            c.minutes += p["minutes"]
            surname_owners.setdefault(p["last"], set()).add(p["code"])
            for club in p["clubs"]:
                club_minutes.setdefault(p["code"], {})
                club_minutes[p["code"]][club] = club_minutes[p["code"]].get(club, 0) + p["minutes"]
                c.club_names.update(season.club_names.get(club, []))
    for c in out.values():
        mins = club_minutes.get(c.code, {})
        c.clubs = sorted(mins, key=lambda k: mins[k], reverse=True)
        c.distinctive = (c.last not in COMMON_SURNAMES and len(c.last.replace(" ", "")) >= 5
                         and len(surname_owners.get(c.last, set())) == 1)
    return out


def relevant(result: dict, career: Career) -> tuple[bool, str]:
    """Is this search result clearly about this player? (kept, reason in plain words)"""
    title = normalize(result.get("title", ""))
    text = title + " " + normalize(result.get("description", ""))
    if not career.last or not contains_phrase(title, career.last):
        return False, "his surname is not in the title"
    if any(contains_phrase(text, w) for w in NOT_HIM):
        return False, "about women's or youth basketball"
    full = bool(career.first) and contains_phrase(title, career.full)
    club_in_title = any(contains_phrase(title, n) for n in career.club_names)
    club_anywhere = club_in_title or any(contains_phrase(text, n) for n in career.club_names)
    strong_title = club_in_title or any(contains_phrase(title, w) for w in STRONG_CONTEXT)
    strong = strong_title or club_anywhere or any(contains_phrase(text, w) for w in STRONG_CONTEXT)
    weak = any(contains_phrase(text, w) for w in WEAK_CONTEXT)
    if full and strong:
        return True, "full name, and EuroLeague or one of his clubs"
    if full and weak and career.distinctive:
        return True, "full name (a name only he had) and basketball words"
    if not full and strong_title:
        return True, "surname, with EuroLeague or one of his clubs in the title"
    if full and weak:
        return False, "full name and basketball words, but the name is shared or common: needs EuroLeague or his club"
    if full:
        return False, "full name, but nothing about basketball"
    return False, "surname only, without EuroLeague or his club in the title"


def is_award(title: str) -> bool:
    t = normalize(title)
    return any(contains_phrase(t, w) for w in AWARD_PHRASES)


def assign_season(result: dict, career: Career, seasons: dict[str, dict]) -> tuple[str | None, str]:
    """The season page a result belongs on: a season he played that the title names, else the one
    it was published in; after his last EuroLeague season, "*" (all his pages)."""
    title = normalize(result.get("title", ""))
    named = season_alias_in(title, seasons)
    if named:
        if named in career.seasons:
            return named, "the season in the title"
        return None, "the title names a season he did not play in the EuroLeague"
    published = result["published"]
    for code in career.seasons:
        s = seasons.get(code) or {}
        if s.get("start") and s.get("end") and s["start"] <= published <= s["end"]:
            return code, "published during that season"
    last = seasons.get(career.seasons[-1]) if career.seasons else None
    if last and last.get("end") and published > last["end"]:
        return "*", "published after his last EuroLeague season"
    return None, "published while he was not in the EuroLeague"


def query_for(career: Career, variant: str, club_short: dict[str, str] | None = None) -> str:
    club = (club_short or {}).get(career.clubs[0], "") if career.clubs else ""
    return " ".join(QUERY_VARIANTS[variant].format(name=career.name, club=club).split())


def search_player(yt: "YouTube", career: Career, variant: str, club_short: dict[str, str]) -> list[dict]:
    obj = yt.list("search", {"part": "snippet", "q": query_for(career, variant, club_short), "type": "video",
                             "videoEmbeddable": "true", "safeSearch": "strict",
                             "maxResults": SEARCH_RESULTS})
    return parse_search(obj)


def pacific_day(now: dt.datetime) -> str:
    """Google's daily quota resets at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return now.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:  # no time zone data: Pacific standard time
        return (now.astimezone(dt.timezone.utc) - dt.timedelta(hours=8)).date().isoformat()


def priority(data: dict[str, Season]) -> list[str]:
    """Players in search order: the current season's (most minutes first), then each earlier
    season's, newest first. Each player once, at his most recent season's place."""
    order, seen = [], set()
    for code in sorted(data, reverse=True):
        for p in sorted(data[code].players, key=lambda p: p["minutes"], reverse=True):
            if p["code"] not in seen:
                seen.add(p["code"])
                order.append(p["code"])
    return order


def plan_searches(order: list[str], state: dict, now: dt.datetime, per_day: int) -> list[str]:
    """Today's searches: the players due (never searched, or REFRESH_DAYS ago), in priority order,
    among the first per_day x REFRESH_DAYS players (the most that can be kept fresh), up to what is
    left of today's calls."""
    today = now.date()
    day = pacific_day(now)
    used = state.get("calls", 0) if state.get("day") == day else 0
    budget = max(0, min(per_day, SEARCH_CALLS_LIMIT - 5) - used)
    covered = order[:per_day * REFRESH_DAYS]
    due = []
    for code in covered:
        entry = state.get("players", {}).get(code)
        age = (today - dt.date.fromisoformat(entry["on"])).days if entry else None
        if entry is None or age >= REFRESH_DAYS:
            due.append(code)
        if len(due) >= budget:
            break
    return due


def age_state(state: dict, now: dt.datetime) -> dict:
    """Drops search results KEEP_DAYS old or older (never kept past YouTube's 30 days)."""
    today = now.date()
    players = {code: e for code, e in (state.get("players") or {}).items()
               if (today - dt.date.fromisoformat(e["on"])).days < KEEP_DAYS}
    return {"format": 1, "day": state.get("day"), "calls": state.get("calls", 0), "players": players}


# --- Quota -----------------------------------------------------------------------------------------

class Quota:
    def __init__(self, cap: int, search_calls: int = 0):
        self.cap = max(1, min(int(cap), HARD_CAP))
        self.used = 0
        self.by_method: dict[str, int] = {}
        self.search_cap = max(0, min(int(search_calls), SEARCH_CALLS_LIMIT - 5))
        self.search_calls = 0

    def spend(self, method: str, limit: int | None = None) -> None:
        if method == "search":  # its own daily limit (calls), apart from the units
            if self.search_calls + 1 > self.search_cap:
                raise QuotaCap(method)
            self.search_calls += 1
            return
        cost = COST[method]
        if self.used + cost > min(self.cap, limit if limit is not None else self.cap):
            raise QuotaCap(method)
        self.used += cost
        self.by_method[method] = self.by_method.get(method, 0) + cost


# --- Clients ---------------------------------------------------------------------------------------

def http_get(url: str, headers: dict, timeout: int = 30) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return reply.status, reply.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""


class YouTube:
    """YouTube Data API v3 list requests. `get(method, params) -> (status, body)` can be replaced
    in tests. The key is added here and nowhere else, and no URL is ever printed."""

    def __init__(self, key: str, quota: Quota, get=None, pause: float = 0.1):
        self._key = key
        self.quota = quota
        self._get = get or self._network
        self._pause = pause

    def _network(self, method: str, params: dict) -> tuple[int, bytes]:
        query = urllib.parse.urlencode({**params, "key": self._key})
        for attempt in range(3):
            status, body = http_get(YOUTUBE_API + method + "?" + query, {"Accept": "application/json"})
            if status not in (0, 500, 502, 503, 504):
                return status, body
            time.sleep(2 * (attempt + 1))
        return status, body

    def list(self, method: str, params: dict, limit: int | None = None) -> dict:
        self.quota.spend(method, limit)
        status, body = self._get(method, params)
        if self._pause:
            time.sleep(self._pause)
        if status != 200:
            raise YouTubeError(status, error_reasons(body))
        return json.loads(body.decode("utf-8"))


class EuroLeague:
    """The public EuroLeague API, politely (one request a second, three tries)."""

    def __init__(self, get=None, pause: float = 1.0):
        self._get = get or self._network
        self._pause = pause
        self.requests = 0

    def _network(self, path: str) -> tuple[int, bytes]:
        for attempt in range(4):
            status, body = http_get(EUROLEAGUE_API + path, {"Accept": "application/json"})
            if status == 200 or status in (204, 404):
                return status, body
            time.sleep(2 ** (attempt + 1))
        return status, body

    def get(self, path: str) -> dict:
        self.requests += 1
        status, body = self._get(path)
        if self._pause:
            time.sleep(self._pause)
        if status == 204 or not body:
            return {}
        if status != 200:
            raise RuntimeError(f"EuroLeague API answered HTTP {status} for {path}")
        return json.loads(body.decode("utf-8"))

    def seasons(self) -> dict[str, dict]:
        out = {}
        for s in self.get(f"/v2/competitions/{COMPETITION}/seasons").get("data") or []:
            out[s["code"]] = {"alias": s.get("alias", ""), "start": parse_time(s.get("startDate")),
                              "end": parse_time(s.get("endDate"))}
        return out

    def season(self, code: str, info: dict) -> Season:
        games = self.get(f"/v2/competitions/{COMPETITION}/seasons/{code}/games")
        stats = self.get(f"/v3/competitions/{COMPETITION}/statistics/players/traditional"
                         f"?seasonMode=Single&seasonCode={code}&statisticMode=Accumulated&limit=1000")
        return Season(code, info["alias"], info["start"], info["end"], games, stats)


# --- The run ---------------------------------------------------------------------------------------

def find_channel(yt: YouTube, log) -> dict:
    channel = parse_channel(yt.list("channels", {"part": "snippet,contentDetails", "id": CHANNEL_ID}))
    if channel and channel["handle"].lower() == CHANNEL_HANDLE:
        return channel
    log(f"warning: channel {CHANNEL_ID} is not {CHANNEL_HANDLE}; looking the handle up instead")
    by_handle = parse_channel(yt.list("channels", {"part": "snippet,contentDetails", "forHandle": CHANNEL_HANDLE}))
    if not by_handle:
        raise RuntimeError(f"the channel {CHANNEL_HANDLE} was not found")
    log(f"warning: using channel {by_handle['id']}; put this ID in CHANNEL_ID once checked")
    return by_handle


def read_uploads(yt: YouTube, channel: dict, log) -> tuple[list[dict], bool]:
    """The channel's uploads, newest first, until the end or this part's share of the cap."""
    complete = True
    uploads_limit = int(yt.quota.cap * UPLOADS_SHARE)
    videos, token = [], None
    try:
        while True:
            params = {"part": "snippet,contentDetails,status", "playlistId": channel["uploads"],
                      "maxResults": PAGE}
            if token:
                params["pageToken"] = token
            items, token = parse_playlist_page(yt.list("playlistItems", params, limit=uploads_limit))
            videos.extend(items)
            if not token:
                break
    except QuotaCap:
        complete = False
        log(f"warning: stopped reading uploads at the cap ({uploads_limit} units); older videos are left out")
    except YouTubeError as e:
        if not e.quota or not videos:
            raise
        complete = False
        log(f"warning: {e}; stopped reading uploads")
    seen, unique = set(), []
    for v in videos:
        if v["id"] not in seen:
            seen.add(v["id"])
            unique.append(v)
    return sorted(unique, key=lambda v: v["published"], reverse=True), complete


def check_videos(yt: YouTube, ids: list[str], log) -> tuple[dict[str, dict], set[str], bool]:
    """videos.list for these IDs, 50 at a time: (details, IDs checked, complete)."""
    details: dict[str, dict] = {}
    checked: set[str] = set()
    complete = True
    try:
        for i in range(0, len(ids), PAGE):
            batch = ids[i:i + PAGE]
            details.update(parse_videos(yt.list("videos", {"part": "snippet,status,contentDetails,player",
                                                           "id": ",".join(batch), "maxWidth": 640,
                                                           "maxResults": PAGE})))
            checked.update(batch)
    except QuotaCap:
        complete = False
        log(f"warning: stopped checking videos at the cap ({yt.quota.cap} units); unchecked videos are left out")
    except YouTubeError as e:
        if not e.quota:
            raise
        complete = False
        log(f"warning: {e}; unchecked videos are left out")
    return details, checked, complete


def exclusion(video_id: str, details: dict[str, dict], checked: set[str]) -> str | None:
    """Why a video may not be listed, or None."""
    if video_id not in checked:
        return "not checked"
    d = details.get(video_id)
    if d is None:
        return "gone"
    s = d["status"]
    if s.get("privacyStatus") != "public":
        return "not public"
    if s.get("embeddable") is not True:
        return "not embeddable"
    if s.get("madeForKids") is True or s.get("selfDeclaredMadeForKids") is True:
        return "made for kids"
    if "madeForKids" not in s:
        return "no made-for-kids answer"
    if not usable(s):
        return "other"
    if shorts_reason(d):
        return "Shorts"
    return None


def club_short_names(data: dict[str, Season]) -> dict[str, str]:
    """Each club's short name, as in its latest season ("Olympiacos", "Real Madrid", "Monaco")."""
    out: dict[str, str] = {}
    for code in sorted(data):
        out.update(data[code].club_short)
    return out


def run_searches(yt: YouTube, data: dict[str, Season], seasons: dict[str, dict], state: dict,
                 now: dt.datetime, per_day: int, official_channel: str, log) -> dict:
    """Today's player searches; returns the new state (results kept per player, with dates)."""
    state = age_state(state or {}, now)
    if per_day <= 0:
        return state
    all_careers = careers(data)
    order = [c for c in priority(data) if c in all_careers]
    due = plan_searches(order, state, now, per_day)
    shorts = club_short_names(data)
    day = pacific_day(now)
    calls_before = state["calls"] if state.get("day") == day else 0
    searched = 0
    try:
        for code in due:
            career = all_careers[code]
            results = search_player(yt, career, DEFAULT_QUERY, shorts)
            searched += 1
            clips = []
            for r in results:
                ok, _ = relevant(r, career)
                if not ok:
                    continue
                season, _ = assign_season(r, career, seasons)
                if season is None:
                    continue
                clip = {"v": r["id"], "season": season}
                if is_award(r["title"]):
                    clip["kind"] = "award"
                if r["channel"] == official_channel:
                    clip["official"] = True
                clips.append(clip)
                if len(clips) >= MAX_SEARCH_CLIPS:
                    break
            state["players"][code] = {"on": now.date().isoformat(), "clips": clips}
    except QuotaCap:
        log("warning: stopped searching at today's limit of search calls")
    except YouTubeError as e:
        if not e.quota:
            raise
        log(f"warning: {e}; stopped searching for today")
    state["day"] = day
    state["calls"] = calls_before + searched
    covered = min(len(order), per_day * REFRESH_DAYS)
    fresh = sum(1 for c in order[:covered] if c in state["players"])
    log(f"player searches: {searched} today ({state['calls']} search.list calls on {day}, Pacific day; "
        f"limit {SEARCH_CALLS_LIMIT}); {fresh} of the {covered} players covered have fresh results "
        f"(of {len(order)} players in all)")
    return state


def build(yt: YouTube, euroleague: EuroLeague, now: dt.datetime, log=print, state: dict | None = None,
          searches_per_day: int = 0, state_out: dict | None = None) -> dict:
    """Builds the index. With searches, the new search state (which players were searched when,
    and their results: video IDs and our codes only) is put into `state_out` for its own file.
    Raises on errors that leave nothing worth publishing."""
    channel = find_channel(yt, log)
    log(f"channel: {channel['title']} ({channel['handle']}, {channel['id']}), uploads {channel['uploads']}")

    # 1. The official channel's uploads.
    videos, complete = read_uploads(yt, channel, log)
    log(f"uploads read: {len(videos)}")
    if len(videos) == 20_000:
        log("note: YouTube lists only the newest 20,000 uploads of a channel; older ones are not reached")
    if not videos:
        raise RuntimeError("no uploads were read, so nothing is published")

    # 2. Our data, every season.
    seasons = euroleague.seasons()
    data = {code: euroleague.season(code, seasons[code]) for code in sorted(seasons)}
    log(f"EuroLeague seasons read: {len(data)} ({euroleague.requests} requests)")

    # 3. Matching the uploads.
    matched = []
    for v in videos:
        code = season_for(v, seasons)
        if code and code in data:
            entry = match(v, data[code])
            if entry:
                if "players" in entry and is_award(v["title"]):
                    entry["kind"] = "award"
                entry["official"] = True
                matched.append((v["published"], entry))
    matched.sort(key=lambda m: m[0], reverse=True)
    log(f"matched: {len(matched)} "
        f"({sum(1 for _, e in matched if 'players' in e)} player clips, "
        f"{sum(1 for _, e in matched if 'players' not in e)} game highlights)")

    # 4. Player searches across YouTube (spread over the days).
    new_state = run_searches(yt, data, seasons, state or {}, now, searches_per_day, channel["id"], log)
    searched = []
    for code, e in new_state["players"].items():
        for clip in e["clips"]:
            searched.append({**clip, "players": [code]})

    # 5. Only public, embeddable videos that are not made for kids and not Shorts.
    entries = [e for _, e in matched] + searched
    ids = list(dict.fromkeys(e["v"] for e in entries))
    details, checked, checks_complete = check_videos(yt, ids, log)
    complete = complete and checks_complete
    excluded = {k: 0 for k in ("not checked", "gone", "not public", "not embeddable", "made for kids",
                               "no made-for-kids answer", "other", "Shorts")}
    clips, by_id = [], {}
    for e in entries:
        why = exclusion(e["v"], details, checked)
        if why:
            excluded[why] += 1
            continue
        if e["v"] in by_id:  # the same video from the channel and a search: one entry
            first = by_id[e["v"]]
            first["players"] = sorted(set(first.get("players", [])) | set(e.get("players", [])))
            if e.get("kind") == "award":
                first["kind"] = "award"
            continue
        e = dict(e)
        by_id[e["v"]] = e
        clips.append(e)
    # Search results that no longer pass are dropped from the state too.
    for code, e in new_state["players"].items():
        e["clips"] = [c for c in e["clips"] if c["v"] in by_id]
    log("excluded: " + ", ".join(f"{n} {k}" for k, n in excluded.items()))
    log(f"Shorts excluded this run: {excluded['Shorts']}")
    if entries and not clips:
        raise RuntimeError("no video could be checked, so nothing is published")

    index = {
        "format": INDEX_FORMAT,
        "fetched": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "complete": complete,
        "clips": clips,
    }
    if state_out is not None:
        state_out.clear()
        state_out.update(new_state)
    return index


# --- Sample mode -----------------------------------------------------------------------------------

# Five well-known players with three query variants each, five lesser-known ones from different
# seasons with two: 25 search.list calls.
SAMPLE_PLAYERS = [
    ("003469", "ABC"),  # Sasha Vezenkov
    ("005985", "ABC"),  # Mike James
    ("001926", "ABC"),  # Kostas Sloukas
    ("JUO", "ABC"),     # Vassilis Spanoulis
    ("003941", "ABC"),  # Nikola Milutinov
    ("CTN", "AB"),      # Charles Smith (2004-05; a very common name)
    ("BTR", "AB"),      # Tanoka Beard (2004-05)
    ("JWO", "AB"),      # Pete Mickeal (2012-13)
    ("004172", "AB"),   # Mateusz Ponitka (2019-20)
    ("010781", "AB"),   # Iffe Lundberg (2025-26)
]


def cell(text) -> str:
    return " ".join(str(text if text is not None else "").replace("|", "/").split())


def sample_report(yt: YouTube, euroleague: EuroLeague, now: dt.datetime, log=print,
                  players: list[tuple[str, str]] = SAMPLE_PLAYERS) -> str:
    """Searches the sample players with each query variant and explains every decision. The
    report has YouTube titles and channels, so it is only ever stored encrypted (see the workflow);
    the log gets counts only."""
    channel = find_channel(yt, log)
    seasons = euroleague.seasons()
    data = {code: euroleague.season(code, seasons[code]) for code in sorted(seasons)}
    all_careers = careers(data)
    shorts = club_short_names(data)
    runs = []  # (career, variant, query, results)
    try:
        for code, variants in players:
            career = all_careers.get(code)
            if not career:
                log(f"sample: player {code} is not in the EuroLeague data")
                continue
            for variant in variants:
                runs.append((career, variant, query_for(career, variant, shorts),
                             search_player(yt, career, variant, shorts)))
    except QuotaCap:
        log("warning: the sample stopped at its share of search calls")
    except YouTubeError as e:
        if not e.quota and not runs:
            raise
        log(f"warning: {e}; the sample stopped early")
    ids = list(dict.fromkeys(r["id"] for _, _, _, results in runs for r in results))
    details, checked, _ = check_videos(yt, ids, log)

    lines = [f"# Clips sample report ({now.astimezone(dt.timezone.utc):%Y-%m-%d %H:%M} UTC)", "",
             "Private: it has YouTube titles and channels. Delete it after sending it.", "",
             "Columns: decision; reason; title; channel; length; embedded size (width x height); the season page "
             "it would go on (* = all his pages); award?; official EuroLeague channel?; link.", ""]
    totals: dict[str, list[int]] = {}
    current = None
    for career, variant, query, results in runs:
        if career.code != current:
            current = career.code
            lines += [f"## {career.name} ({career.code})", "",
                      f"EuroLeague seasons {career.seasons[0]} to {career.seasons[-1]} ({len(career.seasons)}); "
                      f"clubs {', '.join(career.clubs)}; surname only he had: {'yes' if career.distinctive else 'no'}", ""]
        lines += [f"### Variant {variant}: `{query}` ({len(results)} results)", "",
                  "| # | Decision | Reason | Title | Channel | Length | Size | Page | Award | Official | Video |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        kept = 0
        for i, r in enumerate(results, 1):
            d = details.get(r["id"]) or {}
            ok, reason = relevant(r, career)
            season = ""
            if ok:
                season, why = assign_season(r, career, seasons)
                if season is None:
                    ok, reason, season = False, why, ""
            if ok:
                bad = exclusion(r["id"], details, checked)
                if bad:
                    ok = False
                    reason = f"{bad}: {shorts_reason(d)}" if bad == "Shorts" else bad
            kept += ok
            length = d.get("duration")
            size = f"{d.get('width')}x{d.get('height')}" if d.get("width") and d.get("height") else "?"
            lines.append("| " + " | ".join(cell(x) for x in [
                i, "KEEP" if ok else "reject", reason, r["title"], r["channel_title"],
                f"{length // 60}:{length % 60:02d}" if length is not None else "?", size, season,
                "yes" if is_award(r["title"]) else "", "yes" if r["channel"] == channel["id"] else "",
                f"https://www.youtube.com/watch?v={r['id']}"]) + " |")
            totals.setdefault(variant, [0, 0])
        totals.setdefault(variant, [0, 0])
        totals[variant][0] += kept
        totals[variant][1] += len(results)
        lines.append("")
    lines += ["## Summary", "", "| Variant | Query | Kept | Results |", "|---|---|---|---|"]
    for variant in sorted(totals):
        lines.append(f"| {variant} | `{QUERY_VARIANTS[variant]}` | {totals[variant][0]} | {totals[variant][1]} |")
    log(f"sample: {len(runs)} searches, {len(ids)} videos looked at; "
        + ", ".join(f"variant {v} kept {t[0]} of {t[1]}" for v, t in sorted(totals.items())))
    return "\n".join(lines) + "\n"


def write_index(index: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"), ensure_ascii=True)
        f.write("\n")
    os.replace(tmp, path)


def read_state(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) and state.get("format") == 1 else {}
    except (OSError, ValueError):
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="out/clips-index.json")
    parser.add_argument("--state", help="the search state file (read, then written back)")
    parser.add_argument("--cap", type=int, default=int(os.environ.get("CLIPS_QUOTA_CAP", DEFAULT_CAP)),
                        help=f"YouTube quota units this run may use (at most {HARD_CAP})")
    parser.add_argument("--searches-per-day", type=int,
                        default=int(os.environ.get("CLIPS_SEARCHES_PER_DAY", SEARCHES_PER_DAY)),
                        help=f"player searches (search.list calls) a day, at most {SEARCH_CALLS_LIMIT - 5}")
    parser.add_argument("--sample-report", help="sample mode: write the report here instead of an index")
    args = parser.parse_args(argv)

    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        print("error: the YOUTUBE_API_KEY secret is not set for this workflow", file=sys.stderr)
        return 2
    sample = bool(args.sample_report)
    per_day = 0 if sample else max(0, min(args.searches_per_day, SEARCH_CALLS_LIMIT - 5 - SAMPLE_SEARCHES))
    quota = Quota(args.cap, SAMPLE_SEARCHES if sample else per_day)
    yt = YouTube(key, quota)
    lines = []

    def log(line: str) -> None:
        line = line.replace(key, "***")  # belt and braces: no message ever carries the key
        lines.append(line)
        print(line, flush=True)

    status = 0
    now = dt.datetime.now(dt.timezone.utc)
    try:
        if sample:
            report = sample_report(yt, EuroLeague(), now, log)
            os.makedirs(os.path.dirname(os.path.abspath(args.sample_report)), exist_ok=True)
            with open(args.sample_report, "w", encoding="utf-8") as f:
                f.write(report.replace(key, "***"))
            log("sample report written (it is stored encrypted, never in the log)")
        else:
            new_state: dict = {}
            index = build(yt, EuroLeague(), now, log, read_state(args.state), per_day, new_state)
            write_index(index, args.out)
            if args.state:
                write_index(new_state, args.state)
            log(f"index written: {len(index['clips'])} clips, complete: {str(index['complete']).lower()}")
            if not index["complete"]:
                print("::warning::The clips index is incomplete this run (see the log).")
    except (YouTubeError, RuntimeError, QuotaCap, ValueError) as e:
        log(f"error: {e}")
        status = 1
    log(f"YouTube quota used this run: {quota.used} units "
        f"({', '.join(f'{m} {n}' for m, n in sorted(quota.by_method.items())) or 'none'}); "
        f"cap {quota.cap}, daily limit {DAILY_LIMIT}")
    log(f"search.list calls this run: {quota.search_calls} (this run may use {quota.search_cap}; "
        f"Google's limit is {SEARCH_CALLS_LIMIT} a day)")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(("## Clips sample\n\n" if sample else "## Clips index\n\n") + "\n".join(f"- {l}" for l in lines) + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
