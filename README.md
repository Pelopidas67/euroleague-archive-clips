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
   `UCGr3nR_XH9r6E5b09ZJAT9w`) with `playlistItems.list`: 1 quota unit per 50 videos. It never uses
   `search.list` (100 units).
2. Reads the seasons, each season's games and each season's players from the public EuroLeague API
   (`api-live.euroleague.net`, the same endpoints the app uses).
3. Matches each video to that data:
   - a **player clip**: the player's full name in the title, or a surname no other player of that
     season has (and which is not an everyday title word);
   - a **game's highlights**: the two clubs in the title, in the video's season, and the round when
     the title has one; without a round, the game between them that ended up to 5 days before the
     video came out.
4. Checks the matched videos with `videos.list` (1 unit per 50): only **public**, **embeddable**
   videos that are **not made for kids** are kept. Deleted videos are missing from YouTube's
   answer, so they drop out.
5. Writes the index and publishes it on the `index` branch.

The whole index is rebuilt on every run; nothing from the day before is kept.

## Quota

YouTube gives a project 10,000 units a day. This job may use at most **2,000** per run (20%;
`CLIPS_QUOTA_CAP` can change it, never above 5,000). 60% of that is for reading the uploads, the
rest for checking the matches. A whole run normally costs a few hundred units (about one unit per
50 uploads, twice). Each run's log ends with the units it used, for example:

    YouTube quota used this run: 412 units (channels 1, playlistItems 321, videos 90); cap 2000, daily limit 10000

If a run reaches its cap, the index it publishes says `"complete": false` and the run shows a
warning. If YouTube refuses the key, or nothing could be checked, nothing is published and the
run fails (red cross); the app keeps its own copy of the index for up to 30 days.

## The index

```json
{"format":1,"fetched":"2026-10-13T04:17:00Z","complete":true,
 "clips":[{"v":"VIDEO_ID_01","season":"E2025","game":301},
          {"v":"VIDEO_ID_02","season":"E2025","players":["003469"]},
          {"v":"VIDEO_ID_03","season":"E2025","players":["003469"],"game":25}]}
```

- `v`: the YouTube video ID. `season`, `players`, `game`: our season code, player codes and game
  code (the EuroLeague API's). Newest video first.
- `fetched`: when the job ran. The app ignores an index older than 30 days.
- No YouTube titles, descriptions, thumbnails or dates. The app labels clips from its own data and
  YouTube's embedded player shows YouTube's own title.

## Tests

`python3 -m unittest discover -s tests -v` runs offline on samples in YouTube's documented
response formats. The workflow runs them before every build.

**Unverified until the first real run:** the YouTube parsing was written from YouTube's documented
response formats (channels.list, playlistItems.list, videos.list); no live YouTube response has
been read yet.

## No raw responses

This repository is public, so its workflow artifacts would be public too. The job therefore saves
no raw YouTube responses at all; the run's log has the counts.
