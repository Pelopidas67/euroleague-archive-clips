# EuroLeague Archive: clips index

This repository holds only the job that builds the **clips index** for the EuroLeague Archive app,
and the index itself. The app downloads the index when it starts, so every user gets highlight
clips without a YouTube key of their own. There is no key in the app.

- The index: `https://raw.githubusercontent.com/<owner>/<this repo>/index/clips-index.json`
  (the `index` branch; it always holds exactly one commit, today's index).
- The job: `.github/workflows/clips.yml` runs `build_index.py` every day at 04:17 UTC, and when
  **Actions > Clips index > Run workflow** is pressed.
- The YouTube API key: only the encrypted Actions secret `YOUTUBE_API_KEY`
  (**Settings > Secrets and variables > Actions**). It is never in this repository, in the index or
  in the logs.

The source of these files is the `clips-job/` folder of the app's own (private) repository; copy
it here again after it changes (`scripts/copy-clips-job.sh` there).

## What the job does

1. Reads the uploads of the official EuroLeague Basketball channel (`@euroleague`,
   `UCGr3nR_XH9r6E5b09ZJAT9w`) with `playlistItems.list`: 1 quota unit per 50 videos. YouTube lists
   only a channel's newest 20,000 uploads.
2. Reads every season's games and players from the public EuroLeague API
   (`api-live.euroleague.net`, the same endpoints the app uses).
3. Matches each upload to that data:
   - a **player clip**: the player's full name in the title, or a surname no other player of that
     season has (and which is not an everyday title word);
   - a **game's highlights**: the two clubs in the title, in the video's season, and the round when
     the title has one; without a round, the game between them that ended up to 5 days before the
     video came out.
4. **Searches YouTube for players** (`search.list`: any channel, `type=video`,
   `videoEmbeddable=true`, `safeSearch=strict`), 65 players a day in priority order: the current
   season's players (most minutes first), then each earlier season's. Each player is searched again
   after 25 days, and his results are dropped at 28 days if not, so only 65 x 25 = 1,625 players
   are covered. A result is kept only if it is clearly about him:
   - his surname in the title;
   - with his full name, also EuroLeague or one of his EuroLeague clubs (title or description); or,
     for a name only he had, basketball words;
   - with his surname only, EuroLeague or one of his clubs in the title.
   It goes on the season page it belongs to: the season in its title, else the one it was published
   in; after his last EuroLeague season, on all his pages (`"season": "*"`); published while he was
   not in the EuroLeague, it is dropped. Videos about awards (MVP, Final Four MVP, Rising Star,
   All-EuroLeague, …) are marked `"kind": "award"`.
5. Checks every video with `videos.list` (1 unit per 50): only **public**, **embeddable** videos that
   are **not made for kids** and **not YouTube Shorts** are kept. Deleted videos are missing from
   YouTube's answer, so they drop out.
6. Writes the index and publishes it on the `index` branch, with `search-state.json` (which players
   were searched when, and the video IDs found; no titles).

**Shorts** are recognised only from documented `videos.list` data, with several signals together;
the length alone never decides:

- a vertical video: the embedded player is taller than wide (`player.embedHeight` >
  `player.embedWidth`, returned because the request gives `maxWidth`);
- a square video of at most 3 minutes;
- `#shorts` or `#short` in the title, description or tags, with at most 3 minutes.

A 58-second or a 1:27 horizontal clip is kept. Each run logs `Shorts excluded this run: N`.

The whole index is rebuilt on every run; nothing from the day before is kept except the search
results, which are checked again every day and dropped at 28 days.

## Quota

- **Units:** YouTube gives a project 10,000 a day. This job may use at most **2,000** per run (20%;
  `CLIPS_QUOTA_CAP` can change it, never above 5,000). A run costs about 600-1,000 (uploads about
  400, `videos.list` the rest).
- **search.list calls:** Google limits them to 100 a day, apart from the units. The daily run uses at
  most **65** (`CLIPS_SEARCHES_PER_DAY` can lower it), the sample mode at most **25**, so both on the
  same day stay at 90. They are counted per Pacific day (when Google's quota resets) in
  `search-state.json`. If Google says the quota is used up, the searches stop for that day and the
  rest is published as usual.

Each run's log ends with what it used, for example:

    YouTube quota used this run: 812 units (channels 1, playlistItems 400, videos 411); cap 2000, daily limit 10000
    search.list calls this run: 65 (this run may use 65; Google's limit is 100 a day)

If a run reaches its unit cap, the index it publishes says `"complete": false` and the run shows a
warning. If YouTube refuses the key, or nothing could be checked, nothing is published and the
run fails (red cross); the app keeps its own copy of the index for up to 30 days.

## The index

```json
{"format":1,"fetched":"2026-10-15T04:17:00Z","complete":true,
 "clips":[{"v":"VIDEO_ID_01","season":"E2025","game":301,"official":true},
          {"v":"VIDEO_ID_02","season":"E2025","players":["003469"],"kind":"award","official":true},
          {"v":"VIDEO_ID_03","season":"E2025","players":["003469"]},
          {"v":"VIDEO_ID_04","season":"*","players":["JUO"]}]}
```

- `v`: the YouTube video ID. `season`, `players`, `game`: our season code (`*`: every season of
  its players), player codes and game code (the EuroLeague API's). `kind: award`: a video about an
  award. `official`: from the official EuroLeague channel.
- `fetched`: when the job ran. The app ignores an index older than 30 days.
- No YouTube titles, descriptions, thumbnails or dates. The app labels clips from its own data and
  YouTube's embedded player shows YouTube's own title.

## Sample mode (for tuning the searches)

**Run workflow** → mode **sample** searches 10 sample players with 2 or 3 query variants each
(`A` = "<name> highlights", `B` = "<name> EuroLeague" (the daily run's), `C` = "<name> <club>
basketball") and writes a report: every result with its decision and reason, title, channel,
length and embedded size. Because this repository is public, the report is kept only **encrypted**
(with the `REPORT_PASSWORD` secret) as the artifact `sample-report`, for 7 days; the log shows counts
only. Nothing is published. See `docs/CLIPS_SETUP.md` in the app's repository for the steps.

## Tests

`python3 -m unittest discover -s tests -v` runs offline on samples in YouTube's documented
response formats. The workflow runs them before every build.

The first real run (14 October 2026) confirmed channels.list, playlistItems.list and videos.list's
status part. **Unverified until the next run:** search.list, and videos.list's snippet,
contentDetails and player parts (used for Shorts).

## No raw responses

This repository is public, so its workflow artifacts would be public too. The daily job therefore
saves no raw YouTube responses at all; the run's log has the counts. The sample report is the one
exception, and it is encrypted.
