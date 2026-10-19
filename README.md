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
