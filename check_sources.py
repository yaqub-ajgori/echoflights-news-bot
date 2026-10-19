import json
import os
import sys
import urllib.error
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

USER_AGENT = "EchoFlightsNewsBot/0.1 (+https://echoflights.com)"
TIMEOUT = 20
MAX_BYTES = 5 * 1024 * 1024
FRESH_HOURS = 24

ATOM = "{http://www.w3.org/2005/Atom}"
RSS1 = "{http://purl.org/rss/1.0/}"
DC_DATE = "{http://purl.org/dc/elements/1.1/}date"

_robots_cache = {}


def robots_allowed(url):
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(origin + "/robots.txt", headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                rp.parse(resp.read(MAX_BYTES).decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rp.disallow_all = True
            else:
                rp.allow_all = True
        except Exception:
            rp = None
        _robots_cache[origin] = rp
    rp = _robots_cache[origin]
    if rp is None:
        return "unknown"
    return "yes" if rp.can_fetch(USER_AGENT, url) else "no"


def parse_date(text):
    if not text:
        return None
    text = text.strip()
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def parse_feed(body):
    root = ET.fromstring(body.lstrip(b"\xef\xbb\xbf \t\r\n"))
    if root.tag == "rss":
        items = root.findall("./channel/item")
        dates = [parse_date(i.findtext("pubDate") or i.findtext(DC_DATE)) for i in items]
    elif root.tag == ATOM + "feed":
        items = root.findall(ATOM + "entry")
        dates = [parse_date(i.findtext(ATOM + "published") or i.findtext(ATOM + "updated")) for i in items]
    elif root.tag.endswith("RDF"):
        items = root.findall(RSS1 + "item")
        dates = [parse_date(i.findtext(DC_DATE)) for i in items]
    else:
        raise ValueError(f"not a feed (root <{root.tag}>)")
    return len(items), [d for d in dates if d]


def check_url(url):
    result = {"url": url, "robots": robots_allowed(url)}
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result["http"] = resp.status
            result["final_url"] = resp.geturl()
            body = resp.read(MAX_BYTES)
    except urllib.error.HTTPError as e:
        result["http"] = e.code
        result["error"] = f"HTTP {e.code}"
        return result
    except Exception as e:
        result["http"] = None
        result["error"] = type(e).__name__ + ": " + str(e)[:120]
        return result

    try:
        count, dates = parse_feed(body)
    except Exception as e:
        result["error"] = "not a valid feed: " + str(e)[:120]
        return result

    result["items"] = count
    result["dated_items"] = len(dates)
    if dates:
        newest = max(dates)
        result["newest"] = newest.isoformat()
        result["age_hours"] = round((datetime.now(timezone.utc) - newest).total_seconds() / 3600, 1)
    return result


def classify(r):
    if "items" not in r:
        return "FAIL"
    if r["items"] == 0:
        return "EMPTY"
    if r.get("age_hours") is None:
        return "NO_DATES"
    if r["age_hours"] > FRESH_HOURS:
        return "STALE"
    return "OK"


def check_source(src):
    attempts = []
    for url in src["urls"]:
        r = check_url(url)
        r["status"] = classify(r)
        attempts.append(r)
        if r["status"] in ("OK", "STALE", "NO_DATES"):
            break
    best = next((a for a in attempts if a["status"] != "FAIL"), attempts[-1])
    return {**src, "result": best, "attempts": attempts}


ORDER = {"OK": 0, "NO_DATES": 1, "STALE": 2, "EMPTY": 3, "FAIL": 4}


def to_markdown(results):
    lines = [
        f"# Source check — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC",
        "",
        "| Status | Source | Region | Items | Newest (h ago) | Robots | URL / error |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in sorted(results, key=lambda s: (ORDER[s["result"]["status"]], s["region"], s["name"])):
        r = s["result"]
        detail = r.get("final_url", r["url"]) if r["status"] != "FAIL" else f'{r["url"]} — {r.get("error", "")}'
        lines.append(
            f'| {r["status"]} | {s["name"]} | {s["region"]} | {r.get("items", "")} '
            f'| {r.get("age_hours", "")} | {r["robots"]} | {detail} |'
        )
    counts = {}
    for s in results:
        counts[s["result"]["status"]] = counts.get(s["result"]["status"], 0) + 1
    lines += ["", "Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: ORDER[kv[0]]))]
    return "\n".join(lines)


def main():
    with open(os.path.join(os.path.dirname(__file__), "sources.json"), encoding="utf-8") as f:
        sources = json.load(f)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check_source, sources))

    report = to_markdown(results)
    print(report)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(report + "\n")

    with open("report.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(main())
