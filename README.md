# EchoFlights News Bot

Automated travel and aviation news for the EchoFlights Facebook page and blog.

## Step 1: source checker

`check_sources.py` tests every feed in `sources.json` and reports, for each source:

- **OK** – valid feed, newest item within 24 hours
- **STALE** – valid feed, newest item older than 24 hours
- **NO_DATES** – valid feed but items have no publish dates
- **EMPTY** – valid feed with no items
- **FAIL** – blocked, not found, or not a feed

It also shows whether the site's `robots.txt` allows the bot.

Sources with several URLs try each in order and report the first one that works. Most extra URLs are guesses at common feed paths, not confirmed addresses.

### Run it

GitHub → **Actions** → **Check sources** → **Run workflow**. The report appears on the run's summary page.

Locally: `python3 check_sources.py` (Python 3.11+, no extra packages).

## Step 2: collector

`collect.py` runs every 10 minutes (**Collect news** workflow) and:

- reads every feed in `sources.json`, skipping any whose `robots.txt` disallows the bot
- skips stories already seen (`data/seen.json`, kept 14 days) and stories older than 48 hours
- treats future-dated stories as published now
- appends new stories to `data/items/YYYY-MM-DD.jsonl` (Bangladesh date) and commits them

No AI and no posting at this step.

## Step 3: filter

`filter.py` runs every 30 minutes (**Filter news** workflow). It sends unscored headlines from the last 2 days to Gemma 4 31B (`gemma-4-31b-it`) in batches of 30, falling back to `gemini-3.5-flash-lite` if Gemma fails. Each item gets a 0–10 score for Bangladeshi travellers and expats, a topic, and a short English event label used later to match the same story across sources. Results go to `data/scores/YYYY-MM-DD.jsonl`; the run summary lists everything scoring 4 or more.

Needs the `GEMINI_API_KEY` repository secret.

## Step 4: draft (dry run)

`draft.py` runs after every **Filter news** run (**Draft posts** workflow). Nothing is published.

1. **Group:** stories scoring 7+ from the last 36 hours are grouped by event (Gemma 4 31B, Flash Lite fallback).
2. **Verify:** a group is drafted only if it scores 8+ and at least 2 different publishers report it (or 1 official source). Every decision is logged in `data/stories/YYYY-MM-DD.jsonl`.
3. **Write:** Gemini 3.8 Flash writes the Bangla card headline, subline, Facebook caption with hashtags, and blog post, using only facts from the source articles (fetched only where `robots.txt` allows).
4. **Check:** code checks every number against the sources plus format rules; Gemma reviews facts and Bangla. On any problem the draft is rewritten once; if it still fails it is saved as rejected.
5. **Card:** `card.py` draws the 1080×1080 EchoFlights card (Hind Siliguri font, SIL Open Font License in `assets/fonts/OFL.txt`).

Limits: 5 approved drafts per Dhaka day; at most 12 writer calls per Pacific day (free tier allows 20).

**Review drafts at [`data/drafts/README.md`](data/drafts/README.md).**
