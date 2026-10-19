import hashlib
import html
import json
import os
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from check_sources import ATOM, DC_DATE, MAX_BYTES, RSS1, TIMEOUT, USER_AGENT, parse_date, robots_allowed

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
SEEN_FILE = os.path.join(DATA, "seen.json")
ITEMS_DIR = os.path.join(DATA, "items")

DHAKA = timezone(timedelta(hours=6))
MAX_AGE = timedelta(hours=48)
SEEN_TTL = timedelta(days=14)
FUTURE_SLACK = timedelta(minutes=10)
SUMMARY_CHARS = 600

CONTENT_ENCODED = "{http://purl.org/rss/1.0/modules/content/}encoded"


def clean_text(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_link(link):
    parts = urlsplit(link.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_") and k != "traffic_source"]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def atom_link(entry):
    for el in entry.findall(ATOM + "link"):
        if el.get("rel", "alternate") == "alternate" and el.get("href"):
            return el.get("href")
    return ""


def parse_items(body):
    root = ET.fromstring(body.lstrip(b"\xef\xbb\xbf \t\r\n"))
    out = []
    if root.tag == "rss":
        for i in root.findall("./channel/item"):
            out.append({
                "title": i.findtext("title"),
                "link": i.findtext("link") or i.findtext("guid") or "",
                "guid": i.findtext("guid"),
                "date": parse_date(i.findtext("pubDate") or i.findtext(DC_DATE)),
                "summary": i.findtext("description") or i.findtext(CONTENT_ENCODED),
            })
    elif root.tag == ATOM + "feed":
        for e in root.findall(ATOM + "entry"):
            out.append({
                "title": e.findtext(ATOM + "title"),
                "link": atom_link(e),
                "guid": e.findtext(ATOM + "id"),
                "date": parse_date(e.findtext(ATOM + "published") or e.findtext(ATOM + "updated")),
                "summary": e.findtext(ATOM + "summary") or e.findtext(ATOM + "content"),
            })
    elif root.tag.endswith("RDF"):
        for i in root.findall(RSS1 + "item"):
            out.append({
                "title": i.findtext(RSS1 + "title"),
                "link": i.findtext(RSS1 + "link") or "",
                "guid": None,
                "date": parse_date(i.findtext(DC_DATE)),
                "summary": i.findtext(RSS1 + "description"),
            })
    else:
        raise ValueError(f"not a feed (root <{root.tag}>)")
    return out


def fetch_source(src):
    url = src["urls"][0]
    if robots_allowed(url) == "no":
        return src, [], "skipped: robots.txt disallows"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read(MAX_BYTES)
        return src, parse_items(body), None
    except urllib.error.HTTPError as e:
        return src, [], f"HTTP {e.code}"
    except Exception as e:
        return src, [], type(e).__name__ + ": " + str(e)[:120]


def item_key(item):
    if item["link"]:
        return normalize_link(item["link"])
    if item["guid"]:
        return "guid:" + item["guid"].strip()
    return "title:" + clean_text(item["title"]).lower()


def load_seen(now):
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
    except FileNotFoundError:
        return {}
    cutoff = now - SEEN_TTL
    return {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= cutoff}


def main():
    now = datetime.now(timezone.utc)
    with open(os.path.join(BASE, "sources.json"), encoding="utf-8") as f:
        sources = json.load(f)

    seen = load_seen(now)
    new_items = []
    report = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch_source, sources))

    for src, items, error in results:
        added = old = 0
        for item in items:
            title = clean_text(item["title"])
            if not title:
                continue
            key = item_key(item)
            if key in seen:
                continue
            seen[key] = now.isoformat()

            published = item["date"]
            if published and published > now + FUTURE_SLACK:
                published = now
            if published and now - published > MAX_AGE:
                old += 1
                continue

            new_items.append({
                "id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:12],
                "source": src["name"],
                "region": src["region"],
                "lang": src["lang"],
                "tier": src["tier"],
                "title": title,
                "link": item["link"].strip(),
                "published": published.astimezone(timezone.utc).isoformat() if published else None,
                "collected": now.isoformat(),
                "summary": clean_text(item["summary"])[:SUMMARY_CHARS],
            })
            added += 1
        report.append((src["name"], len(items), added, old, error))

    os.makedirs(ITEMS_DIR, exist_ok=True)
    day_file = os.path.join(ITEMS_DIR, now.astimezone(DHAKA).strftime("%Y-%m-%d") + ".jsonl")
    if new_items:
        with open(day_file, "a", encoding="utf-8") as f:
            for item in sorted(new_items, key=lambda i: i["published"] or i["collected"]):
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0, sort_keys=True)

    lines = [
        f"# Collect — {now:%Y-%m-%d %H:%M} UTC",
        "",
        f"New items: **{len(new_items)}** → `{os.path.relpath(day_file, BASE)}`",
        "",
        "| Source | In feed | New | Too old | Error |",
        "|---|---|---|---|---|",
    ]
    lines += [f"| {n} | {t} | {a} | {o} | {e or ''} |" for n, t, a, o, e in report]
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
