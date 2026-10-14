#!/usr/bin/env python3
"""Builds the clips index for EuroLeague Archive.

Run by GitHub Actions once a day (see .github/workflows/clips.yml). It:

1. reads the uploads playlist of the official EuroLeague Basketball YouTube channel with
   playlistItems.list (1 quota unit per 50 videos; search.list is never used);
2. reads the EuroLeague data it needs from the public EuroLeague API (the same endpoints the app
   uses): the seasons, each season's games and each season's players;
3. matches each video to our data: a player clip by the player's name in the title, a game's
   highlights by the two clubs (and the round when the title has one) in that season;
4. checks the matched videos with videos.list (1 unit per 50): only public, embeddable videos that
   are not "made for kids" are kept;
5. writes the index: video IDs, our season, player and game codes, and the date fetched. No YouTube
   titles, descriptions, thumbnails or dates are ever written.

The whole index is rebuilt on every run, so a video that was deleted, made private or can no
longer be embedded drops out at the next run (YouTube's rule: stored API data is refreshed at
least every 30 days; the app ignores an index older than 30 days).

The YouTube API key comes only from the environment variable YOUTUBE_API_KEY (a GitHub Actions
secret). It is never printed: no request URL is ever printed or put in an error message.

Only the Python standard library is used.

The YouTube parsing below follows YouTube's documented response formats
(developers.google.com/youtube/v3/docs: channels.list, playlistItems.list, videos.list).
UNVERIFIED against a live response until the first real run.
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


def parse_videos(obj: dict) -> dict[str, dict]:
    """videos.list, part=status:
    { "items": [ { "id": "…", "status": { "uploadStatus", "privacyStatus", "embeddable",
                   "madeForKids", "selfDeclaredMadeForKids", … } } ] }
    A video that was deleted is simply missing from "items"."""
    out = {}
    for item in obj.get("items") or []:
        if item.get("id"):
            out[item["id"]] = item.get("status") or {}
    return out


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
        for g in games_json.get("data") or []:
            local = ((g.get("local") or {}).get("club") or {})
            road = ((g.get("road") or {}).get("club") or {})
            if not local.get("code") or not road.get("code") or not g.get("played", False):
                continue
            for c in (local, road):
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
            seen[code] = {"code": code, "first": normalize(first), "last": " ".join(last_words)}
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


# --- Quota -----------------------------------------------------------------------------------------

class Quota:
    def __init__(self, cap: int):
        self.cap = max(1, min(int(cap), HARD_CAP))
        self.used = 0
        self.by_method: dict[str, int] = {}

    def spend(self, method: str, limit: int | None = None) -> None:
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


def build(yt: YouTube, euroleague: EuroLeague, now: dt.datetime, log=print) -> dict:
    """Builds the index. Raises on errors that leave nothing worth publishing."""
    complete = True
    channel = find_channel(yt, log)
    log(f"channel: {channel['title']} ({channel['handle']}, {channel['id']}), uploads {channel['uploads']}")

    # 1. The uploads, newest first, until the end or this part's share of the cap.
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
    videos = sorted(unique, key=lambda v: v["published"], reverse=True)
    log(f"uploads read: {len(videos)}")
    if not videos:
        raise RuntimeError("no uploads were read, so nothing is published")

    # 2. Our data for the seasons those videos fall in.
    seasons = euroleague.seasons()
    by_season: dict[str, list[dict]] = {}
    for v in videos:
        code = season_for(v, seasons)
        if code:
            by_season.setdefault(code, []).append(v)
    data = {code: euroleague.season(code, seasons[code]) for code in sorted(by_season)}
    log(f"EuroLeague seasons read: {len(data)} ({euroleague.requests} requests)")

    # 3. Matching.
    matched = []
    for code, vs in by_season.items():
        for v in vs:
            entry = match(v, data[code])
            if entry:
                matched.append((v["published"], entry))
    matched.sort(key=lambda m: m[0], reverse=True)
    log(f"matched: {len(matched)} "
        f"({sum(1 for _, e in matched if 'players' in e)} player clips, "
        f"{sum(1 for _, e in matched if 'players' not in e)} game highlights)")

    # 4. Only public, embeddable videos that are not made for kids.
    statuses: dict[str, dict] = {}
    checked = set()
    ids = [e["v"] for _, e in matched]
    try:
        for i in range(0, len(ids), PAGE):
            batch = ids[i:i + PAGE]
            statuses.update(parse_videos(yt.list("videos", {"part": "status", "id": ",".join(batch),
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
    excluded = {"not checked": 0, "gone": 0, "not public": 0, "not embeddable": 0, "made for kids": 0,
                "no made-for-kids answer": 0, "other": 0}
    clips = []
    for _, e in matched:
        if e["v"] not in checked:
            excluded["not checked"] += 1
            continue
        s = statuses.get(e["v"])
        if s is None:
            excluded["gone"] += 1
        elif s.get("privacyStatus") != "public":
            excluded["not public"] += 1
        elif s.get("embeddable") is not True:
            excluded["not embeddable"] += 1
        elif s.get("madeForKids") is True or s.get("selfDeclaredMadeForKids") is True:
            excluded["made for kids"] += 1
        elif "madeForKids" not in s:
            excluded["no made-for-kids answer"] += 1
        elif not usable(s):
            excluded["other"] += 1
        else:
            clips.append(e)
    log("excluded: " + ", ".join(f"{n} {k}" for k, n in excluded.items()))
    if matched and not clips:
        raise RuntimeError("no video could be checked, so nothing is published")

    return {
        "format": INDEX_FORMAT,
        "fetched": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "complete": complete,
        "clips": clips,
    }


def write_index(index: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, separators=(",", ":"), ensure_ascii=True)
        f.write("\n")
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default="out/clips-index.json")
    parser.add_argument("--cap", type=int, default=int(os.environ.get("CLIPS_QUOTA_CAP", DEFAULT_CAP)),
                        help=f"YouTube quota units this run may use (at most {HARD_CAP})")
    args = parser.parse_args(argv)

    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        print("error: the YOUTUBE_API_KEY secret is not set for this workflow", file=sys.stderr)
        return 2
    quota = Quota(args.cap)
    yt = YouTube(key, quota)
    lines = []

    def log(line: str) -> None:
        line = line.replace(key, "***")  # belt and braces: no message ever carries the key
        lines.append(line)
        print(line, flush=True)

    status = 0
    try:
        index = build(yt, EuroLeague(), dt.datetime.now(dt.timezone.utc), log)
        write_index(index, args.out)
        log(f"index written: {len(index['clips'])} clips, complete: {str(index['complete']).lower()}")
        if not index["complete"]:
            print("::warning::The clips index is incomplete this run (see the log).")
    except (YouTubeError, RuntimeError, QuotaCap, ValueError) as e:
        log(f"error: {e}")
        status = 1
    log(f"YouTube quota used this run: {quota.used} units "
        f"({', '.join(f'{m} {n}' for m, n in sorted(quota.by_method.items())) or 'none'}); "
        f"cap {quota.cap}, daily limit {DAILY_LIMIT}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("## Clips index\n\n" + "\n".join(f"- {l}" for l in lines) + "\n")
    return status


if __name__ == "__main__":
    sys.exit(main())
