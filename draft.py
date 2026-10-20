import glob
import hashlib
import json
import os
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import card
import gemini
from check_sources import MAX_BYTES, TIMEOUT, USER_AGENT, robots_allowed

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DRAFTS = os.path.join(DATA, "drafts")
STORIES = os.path.join(DATA, "stories")
USAGE_FILE = os.path.join(DRAFTS, "usage.json")
DRAFTED_FILE = os.path.join(STORIES, "drafted.jsonl")

DHAKA = timezone(timedelta(hours=6))
PACIFIC = ZoneInfo("America/Los_Angeles")

LOOKBACK = timedelta(hours=36)
CANDIDATE_MIN_SCORE = 7
DRAFT_MIN_SCORE = 8
MAX_CANDIDATES = 40
MAX_STORIES_PER_RUN = 2
DAILY_APPROVED_LIMIT = 5
DAILY_WRITER_CALLS = 12
MAX_SOURCES_PER_STORY = 4
ARTICLE_CHARS = 4000
REVIEW_CHARS_PER_SOURCE = 1800
RUN_BUDGET_SECONDS = 18 * 60
CLUSTER_TIMEOUT = 90
WRITER_TIMEOUT = 240
REVIEW_TIMEOUT = 200
FETCH_SECONDS = 25
LOG_FILE = os.path.join(DATA, "logs", "draft.log")
DEADLINE = None


def log(message):
    line = f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

CLUSTER_MODELS = ["gemini-3.5-flash-lite", "gemma-4-31b-it"]
WRITER_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash"]
REVIEW_MODELS = ["gemma-4-31b-it", "gemini-3.5-flash-lite"]

CATEGORIES = {
    "flight_disruption": "ফ্লাইট আপডেট",
    "airline_offer": "এয়ারলাইন অফার",
    "visa_rule": "ভিসা আপডেট",
    "labour_rule": "শ্রমবাজার",
    "expat_alert": "প্রবাসী সংবাদ",
    "embassy_notice": "দূতাবাস নোটিশ",
    "accident": "দুর্ঘটনা",
    "airport": "বিমানবন্দর",
    "travel_advisory": "ভ্রমণ সতর্কতা",
    "other": "ভ্রমণ সংবাদ",
}

BN_TO_ASCII = str.maketrans("০১২৩৪৫৬৭৮৯٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "dozen": 12, "twenty": 20, "hundred": 100, "thousand": 1000,
    "এক": 1, "দুই": 2, "তিন": 3, "চার": 4, "পাঁচ": 5, "ছয়": 6, "সাত": 7, "আট": 8, "দশ": 10,
    "এগারো": 11, "বারো": 12, "বিশ": 20, "শত": 100, "হাজার": 1000,
}

CLUSTER_PROMPT = """You group news items that report the SAME real-world event, for a news page that must confirm each story with at least two independent outlets.

Rules:
- Two items belong together only if they report the same specific event (same incident, same decision, same announcement). Same country or same topic is NOT enough.
- A follow-up that adds a new development (e.g. a government reaction to an earlier event) still belongs to the same event group.
- Items can be in English, Bangla or Arabic.
- Every input id must appear in exactly one group. An item with no match is its own group.
- For each group, set "drafted" to the id of an already-drafted story (listed under DRAFTED) if the group is the same event as that story, otherwise null.

Reply with ONLY a JSON array:
[{"ids": ["<id>", ...], "event": "<event in at most 10 English words>", "drafted": "<drafted id or null>"}]"""

WRITER_PROMPT = """You are the news editor of EchoFlights, a Bangladeshi travel brand. You write short, accurate news posts in natural Bangla for Bangladeshi travellers and Bangladeshi workers abroad.

ACCURACY RULES (most important):
- Use ONLY facts stated in the SOURCES. Never add numbers, names, dates, places, quotes, causes or consequences that the sources do not state.
- If sources disagree on a detail, use the detail most sources agree on, or leave it out.
- Do not guess what will happen next. Do not invent advice from officials.
- Practical advice is allowed only as general, safe guidance (for example: check with the airline or the embassy's official channels before travelling).

LANGUAGE:
- Write standard Bangla the way a Bangladeshi news desk writes, not a word-for-word translation from English.
- Use the usual Bangla spellings for countries and cities (সৌদি আরব, সংযুক্ত আরব আমিরাত, কুয়েত, কাতার, ওমান, মালয়েশিয়া, সিঙ্গাপুর, দুবাই, রিয়াদ, জেদ্দা, কুয়ালালামপুর, ঢাকা, চট্টগ্রাম, সিলেট).
- Airline and organisation names may stay in English when that is how Bangladeshis usually write them.
- Write all numbers with Bangla digits.
- No emoji in stories about deaths, accidents, executions, arrests or disasters. Otherwise at most one emoji, only at the start of the caption.

OUTPUT: reply with ONLY one JSON object with these keys:
- "card_headline": the core news in at most 12 Bangla words. No clickbait, no question marks.
- "card_subline": one Bangla sentence of at most 18 words with the most important extra detail.
- "category": exactly one of: ফ্লাইট আপডেট, এয়ারলাইন অফার, ভিসা আপডেট, শ্রমবাজার, প্রবাসী সংবাদ, দূতাবাস নোটিশ, দুর্ঘটনা, বিমানবন্দর, ভ্রমণ সতর্কতা, ভ্রমণ সংবাদ
- "caption": Facebook caption in Bangla, 60-120 words. First line states the news plainly. Then 1-2 short paragraphs of key details. Then a line "সূত্র: <outlet names>". Then 4-6 hashtags on the last line, always including #EchoFlights. No links.
- "blog_title": Bangla title for the blog post.
- "blog_slug": English URL slug, lowercase words joined by hyphens, at most 8 words.
- "meta_description": Bangla, at most 150 characters.
- "blog_body": the blog post in Bangla Markdown, 200-350 words: an opening paragraph, a "## কী ঘটেছে" section, a "## বাংলাদেশি যাত্রী ও প্রবাসীদের জন্য" section (only what the sources support, plus general safe guidance), and a "## সূত্র" list of outlet names.
- "facts": list of {"fact": "<one fact you used, in Bangla>", "source": "<outlet name>"}."""

REVIEW_PROMPT = """You are a strict fact-checker and Bangla copy editor for a news page. Compare the DRAFT with the SOURCES.

Report a problem when:
1. The draft states something the sources do not support (any number, name, place, date, cause, quote or consequence).
2. A number, name, place or date is different from the sources.
3. The headline is misleading or exaggerates.
4. The Bangla is wrong or clearly unnatural (wrong word, broken grammar, literal translation).
5. The sources do not all report the same event, so the draft mixes different stories.

Set "verdict" to "fail" if there is any problem of type 1, 2, 3 or 5, or a serious type 4 problem. Minor style suggestions do not cause a fail.

Reply with ONLY one JSON object:
{"verdict": "pass" or "fail", "issues": [{"field": "<card_headline|card_subline|caption|blog_body>", "type": <1-5>, "problem": "<short English description>", "fix": "<suggested correction>"}]}"""


def load_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def append_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def parse_time(value):
    return datetime.fromisoformat(value) if value else None


class ParagraphText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.skip = 0
        self.parts = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.skip += 1
        elif tag == "p":
            self.depth += 1
            self.current = []

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self.skip:
            self.skip -= 1
        elif tag == "p" and self.depth:
            self.depth -= 1
            text = re.sub(r"\s+", " ", "".join(self.current)).strip()
            if len(text) > 40:
                self.parts.append(text)

    def handle_data(self, data):
        if self.depth and not self.skip:
            self.current.append(data)


def fetch_article(url):
    if not url or robots_allowed(url) == "no":
        return ""
    try:
        started = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            chunks, size = [], 0
            while size < MAX_BYTES and time.monotonic() - started < FETCH_SECONDS:
                chunk = resp.read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            body = b"".join(chunks).decode(resp.headers.get_content_charset() or "utf-8", "replace")
        parser = ParagraphText()
        parser.feed(body)
        return "\n".join(parser.parts)[:ARTICLE_CHARS]
    except Exception:
        return ""


def load_sources():
    with open(os.path.join(BASE, "sources.json"), encoding="utf-8") as f:
        return {s["name"]: s for s in json.load(f)}


def load_candidates(now, sources):
    items, scores = {}, {}
    days = {(now - timedelta(days=d)).astimezone(DHAKA).strftime("%Y-%m-%d") for d in range(3)}
    for d in days:
        for i in load_jsonl(os.path.join(DATA, "items", d + ".jsonl")):
            items[i["id"]] = i
        for s in load_jsonl(os.path.join(DATA, "scores", d + ".jsonl")):
            scores[s["id"]] = s
    out = []
    for item_id, s in scores.items():
        item = items.get(item_id)
        if not item or s["score"] < CANDIDATE_MIN_SCORE:
            continue
        when = parse_time(item.get("published")) or parse_time(item["collected"])
        if now - when > LOOKBACK:
            continue
        src = sources.get(item["source"], {})
        out.append({**item, **s, "when": when.isoformat(), "publisher": src.get("publisher", item["source"]),
                    "display": src.get("display", item["source"]), "official": src.get("official", False)})
    out.sort(key=lambda c: (-c["score"], c["when"]))
    return out[:MAX_CANDIDATES]


def cluster(candidates, drafted, errors):
    lines = [f'{c["id"]} | {c["source"]} | {c["title"]} | {c["summary"][:150]} | label: {c["event"]}' for c in candidates]
    drafted_lines = [f'{d["story_id"]} | {d["event"]} | {d["headline"]}' for d in drafted] or ["(none)"]
    prompt = "ITEMS:\n" + "\n".join(lines) + "\n\nDRAFTED:\n" + "\n".join(drafted_lines)
    _, groups = gemini.generate_json(CLUSTER_MODELS, CLUSTER_PROMPT, prompt, opener="[", json_mode=True, errors=errors,
                                     deadline=DEADLINE, log=log, timeout=CLUSTER_TIMEOUT)
    by_id = {c["id"]: c for c in candidates}
    drafted_ids = {d["story_id"] for d in drafted}
    used, out = set(), []
    for g in groups:
        ids = [i for i in g.get("ids", []) if i in by_id and i not in used]
        if not ids:
            continue
        used.update(ids)
        out.append({"items": [by_id[i] for i in ids], "event": str(g.get("event", ""))[:120],
                    "drafted": g.get("drafted") if g.get("drafted") in drafted_ids else None})
    for c in candidates:
        if c["id"] not in used:
            out.append({"items": [c], "event": c["event"], "drafted": None})
    return out


def judge(group):
    items = group["items"]
    publishers = sorted({i["publisher"] for i in items})
    official = any(i["official"] for i in items)
    top = max(items, key=lambda i: i["score"])
    group.update({
        "story_id": hashlib.sha1(min(i["id"] for i in items).encode()).hexdigest()[:10],
        "publishers": publishers,
        "max_score": top["score"],
        "topic": top["topic"],
    })
    if group["drafted"]:
        group["status"] = "already_drafted"
    elif top["score"] < DRAFT_MIN_SCORE:
        group["status"] = "below_threshold"
    elif len(publishers) >= 2 or official:
        group["status"] = "verified"
    else:
        group["status"] = "single_source"
    return group


def source_blocks(group, fetch=True):
    chosen, seen = [], set()
    for item in sorted(group["items"], key=lambda i: -i["score"]):
        if item["publisher"] in seen and len(chosen) >= 2:
            continue
        seen.add(item["publisher"])
        chosen.append(item)
        if len(chosen) >= MAX_SOURCES_PER_STORY:
            break
    blocks = []
    for item in chosen:
        log(f'fetching {item["display"]}: {item["link"][:100]}')
        text = fetch_article(item["link"]) if fetch else ""
        blocks.append({
            "outlet": item["display"],
            "link": item["link"],
            "published": item.get("published"),
            "title": item["title"],
            "text": text if len(text) > len(item["summary"]) else item["summary"],
            "full_text": len(text) > len(item["summary"]),
        })
    return blocks


def render_sources(blocks, limit=None):
    out = []
    for n, b in enumerate(blocks, 1):
        text = b["text"][:limit] if limit else b["text"]
        out.append(f'SOURCE {n}: {b["outlet"]} (published {b["published"]})\nTITLE: {b["title"]}\nTEXT: {text}')
    return "\n\n".join(out)


def numbers(text):
    text = text.translate(BN_TO_ASCII).replace(",", "")
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def source_numbers(text):
    found = numbers(text)
    words = set(re.findall(r"[a-z]+|[\u0980-\u09ff]+", text.lower()))
    found |= {str(v) for w, v in NUMBER_WORDS.items() if w in words}
    return found


def code_checks(draft, blocks, now):
    issues = []
    source_text = " ".join(b["title"] + " " + b["text"] + " " + (b["published"] or "") for b in blocks)
    allowed = source_numbers(source_text) | {str(now.year), str(now.astimezone(DHAKA).day)}
    for field in ("card_headline", "card_subline", "caption", "blog_body"):
        text = re.sub(r"#\S+", "", draft.get(field, ""))
        for n in sorted(numbers(text) - allowed):
            issues.append({"field": field, "type": 2, "problem": f"number {n} does not appear in any source", "fix": "remove or correct it"})
    if len(draft.get("card_headline", "")) > 90:
        issues.append({"field": "card_headline", "type": 3, "problem": "headline too long for the card", "fix": "shorten"})
    if len(draft.get("card_subline", "")) > 150:
        issues.append({"field": "card_subline", "type": 3, "problem": "subline too long for the card", "fix": "shorten"})
    caption = draft.get("caption", "")
    tags = re.findall(r"#\S+", caption)
    if "#EchoFlights" not in tags or not 3 <= len(tags) <= 8:
        issues.append({"field": "caption", "type": 4, "problem": f"needs 4-6 hashtags including #EchoFlights (has {len(tags)})", "fix": "fix hashtags"})
    if "http" in caption or "www." in caption:
        issues.append({"field": "caption", "type": 4, "problem": "caption contains a link", "fix": "remove link"})
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+){0,9}", draft.get("blog_slug", "")):
        issues.append({"field": "blog_slug", "type": 4, "problem": "slug is not lowercase-hyphenated English", "fix": "fix slug"})
    if draft.get("category") not in CATEGORIES.values():
        issues.append({"field": "category", "type": 4, "problem": "unknown category", "fix": "use a listed category"})
    return issues


def review(draft, blocks, errors):
    shown = {k: draft.get(k, "") for k in ("card_headline", "card_subline", "caption", "blog_body")}
    prompt = "SOURCES:\n" + render_sources(blocks, REVIEW_CHARS_PER_SOURCE) + "\n\nDRAFT:\n" + json.dumps(shown, ensure_ascii=False, indent=1)
    model, result = gemini.generate_json(REVIEW_MODELS, REVIEW_PROMPT, prompt, errors=errors, deadline=DEADLINE, log=log,
                                         timeout=REVIEW_TIMEOUT)
    issues = [i for i in result.get("issues", []) if isinstance(i, dict)]
    return model, result.get("verdict") == "pass", issues


def load_usage(now):
    try:
        with open(USAGE_FILE, encoding="utf-8") as f:
            usage = json.load(f)
    except FileNotFoundError:
        usage = {}
    key = now.astimezone(PACIFIC).strftime("%Y-%m-%d")
    return usage, key


def save_usage(usage):
    os.makedirs(DRAFTS, exist_ok=True)
    keep = dict(sorted(usage.items())[-14:])
    with open(USAGE_FILE, "w", encoding="utf-8") as f:
        json.dump(keep, f, indent=1)


def write_story(group, now, usage, usage_key, errors):
    blocks = source_blocks(group)
    base_prompt = (f"TOPIC: {group['topic']}\nEVENT: {group['event']}\n\nSOURCES:\n" + render_sources(blocks))
    attempts = []
    prompt = base_prompt
    for attempt in range(2):
        if usage[usage_key] >= DAILY_WRITER_CALLS or time.monotonic() > DEADLINE:
            break
        usage[usage_key] += 1
        try:
            writer_model, draft = gemini.generate_json(WRITER_MODELS, WRITER_PROMPT, prompt, json_mode=True, errors=errors,
                                                       deadline=DEADLINE, log=log, timeout=WRITER_TIMEOUT)
        except RuntimeError as e:
            if not attempts:
                raise
            attempts[-1]["issues"].append({"field": "-", "type": 0, "problem": f"rewrite could not run: {str(e)[:150]}", "fix": ""})
            break
        issues = code_checks(draft, blocks, now)
        try:
            review_model, passed, review_issues = review(draft, blocks, errors)
        except RuntimeError as e:
            review_model, passed, review_issues = None, False, [{"field": "-", "type": 0, "problem": f"review failed: {e}", "fix": ""}]
        all_issues = issues + review_issues
        ok = passed and not issues
        log(f'attempt {attempt + 1}: {"passed" if ok else "failed"} with {len(all_issues)} issues')
        attempts.append({"writer": writer_model, "reviewer": review_model, "passed": ok, "issues": all_issues, "draft": draft})
        if ok:
            break
        prompt = (base_prompt + "\n\nYOUR PREVIOUS DRAFT:\n" + json.dumps(draft, ensure_ascii=False)
                  + "\n\nPROBLEMS FOUND (fix all of them, keep everything else accurate):\n"
                  + "\n".join(f'- {i.get("field")}: {i.get("problem")} -> {i.get("fix")}' for i in all_issues))
    return blocks, attempts


def save_draft(group, blocks, attempts, now):
    final = attempts[-1]
    draft = final["draft"]
    status = "approved" if final["passed"] else "rejected"
    day = now.astimezone(DHAKA).strftime("%Y-%m-%d")
    folder = os.path.join(DRAFTS, day, group["story_id"])
    os.makedirs(folder, exist_ok=True)
    category = draft.get("category") if draft.get("category") in CATEGORIES.values() else CATEGORIES.get(group["topic"], CATEGORIES["other"])
    outlets = list(dict.fromkeys(b["outlet"] for b in blocks))
    card.render(os.path.join(folder, "card.png"), draft.get("card_headline", ""), draft.get("card_subline", ""),
                category, group["topic"], outlets, now)
    with open(os.path.join(folder, "blog.md"), "w", encoding="utf-8") as f:
        f.write(f'# {draft.get("blog_title", "")}\n\n{draft.get("blog_body", "")}\n')
    record = {
        "story_id": group["story_id"],
        "status": status,
        "created": now.isoformat(),
        "event": group["event"],
        "topic": group["topic"],
        "max_score": group["max_score"],
        "publishers": group["publishers"],
        "sources": [{k: b[k] for k in ("outlet", "link", "published", "title", "full_text")} for b in blocks],
        "draft": draft,
        "attempts": [{k: a[k] for k in ("writer", "reviewer", "passed", "issues")} for a in attempts],
    }
    with open(os.path.join(folder, "draft.json"), "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=1)
    append_jsonl(DRAFTED_FILE, [{"story_id": group["story_id"], "status": status, "created": now.isoformat(),
                                 "event": group["event"], "headline": draft.get("card_headline", ""),
                                 "item_ids": [i["id"] for i in group["items"]]}])
    return record


def write_review_page(now):
    records = []
    for path in glob.glob(os.path.join(DRAFTS, "*", "*", "draft.json")):
        with open(path, encoding="utf-8") as f:
            r = json.load(f)
        r["folder"] = os.path.relpath(os.path.dirname(path), DRAFTS)
        records.append(r)
    records.sort(key=lambda r: r["created"], reverse=True)
    lines = ["# Draft posts (dry run)", "", "Newest first. Nothing here has been published.", ""]
    for r in records[:30]:
        d = r["draft"]
        created = datetime.fromisoformat(r["created"]).astimezone(DHAKA).strftime("%d %b %H:%M")
        mark = "✅ approved" if r["status"] == "approved" else "❌ rejected"
        lines += [
            f'## {d.get("card_headline", "")}',
            "",
            f'{mark} · {created} Dhaka · score {r["max_score"]} · {r["topic"]} · sources: {", ".join(r["publishers"])}',
            "",
            f'<img src="{r["folder"]}/card.png" width="420">',
            "",
            "**Facebook caption**",
            "",
            "```",
            d.get("caption", ""),
            "```",
            "",
            f'**Blog:** [{d.get("blog_title", "")}]({r["folder"]}/blog.md) · slug `{d.get("blog_slug", "")}`',
            "",
            "**Sources:** " + " · ".join(f'[{s["outlet"]}]({s["link"]}){"" if s["full_text"] else " (feed summary only)"}' for s in r["sources"]),
            "",
        ]
        last = r["attempts"][-1]
        if last["issues"]:
            lines += ["**Review notes (last attempt):**", ""] + [f'- {i.get("field")}: {i.get("problem")}' for i in last["issues"]] + [""]
        if len(r["attempts"]) > 1:
            lines += [f'_Rewritten once after review ({len(r["attempts"][0]["issues"])} issues in first draft)._', ""]
        lines += ["---", ""]
    with open(os.path.join(DRAFTS, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    global DEADLINE
    DEADLINE = time.monotonic() + RUN_BUDGET_SECONDS
    now = datetime.now(timezone.utc)
    errors = []
    log("draft run started")
    sources = load_sources()
    candidates = load_candidates(now, sources)
    drafted = [d for d in load_jsonl(DRAFTED_FILE) if now - parse_time(d["created"]) < timedelta(days=3)]
    usage, usage_key = load_usage(now)
    usage.setdefault(usage_key, 0)
    today = now.astimezone(DHAKA).strftime("%Y-%m-%d")
    approved_today = sum(1 for d in load_jsonl(DRAFTED_FILE)
                         if d["status"] == "approved" and parse_time(d["created"]).astimezone(DHAKA).strftime("%Y-%m-%d") == today)

    log(f"{len(candidates)} candidates, {len(drafted)} drafted in last 3 days, writer calls used {usage[usage_key]}")
    groups = []
    if candidates:
        try:
            groups = [judge(g) for g in cluster(candidates, drafted, errors)]
        except RuntimeError as e:
            errors.append(f"cluster failed: {e}")

    run = now.isoformat()
    append_jsonl(os.path.join(STORIES, today + ".jsonl"), [
        {"run": run, "story_id": g["story_id"], "status": g["status"], "event": g["event"], "max_score": g["max_score"],
         "publishers": g["publishers"], "items": [{"id": i["id"], "source": i["source"], "title": i["title"]} for i in g["items"]]}
        for g in groups])

    made = []
    for g in sorted((g for g in groups if g["status"] == "verified"), key=lambda g: (-g["max_score"], -len(g["publishers"]))):
        if (approved_today >= DAILY_APPROVED_LIMIT or usage[usage_key] >= DAILY_WRITER_CALLS
                or time.monotonic() > DEADLINE or len(made) >= MAX_STORIES_PER_RUN):
            break
        log(f'writing story {g["story_id"]}: {g["event"]}')
        try:
            blocks, attempts = write_story(g, now, usage, usage_key, errors)
        except RuntimeError as e:
            errors.append(f'write failed for {g["story_id"]}: {e}')
            continue
        if not attempts:
            break
        rec = save_draft(g, blocks, attempts, now)
        save_usage(usage)
        made.append(rec)
        log(f'saved {rec["status"]} draft {g["story_id"]}')
        if rec["status"] == "approved":
            approved_today += 1
    save_usage(usage)
    if made or not os.path.exists(os.path.join(DRAFTS, "README.md")):
        os.makedirs(DRAFTS, exist_ok=True)
        write_review_page(now)

    count = {}
    for g in groups:
        count[g["status"]] = count.get(g["status"], 0) + 1
    lines = [f"# Draft — {now:%Y-%m-%d %H:%M} UTC", "",
             f"Candidates (score ≥ {CANDIDATE_MIN_SCORE}): {len(candidates)} · groups: " + ", ".join(f"{k} {v}" for k, v in sorted(count.items())),
             f"Writer calls today (Pacific day): {usage[usage_key]}/{DAILY_WRITER_CALLS} · approved today (Dhaka): {approved_today}/{DAILY_APPROVED_LIMIT}", ""]
    lines += ["| Status | Score | Sources | Event |", "|---|---|---|---|"]
    for g in sorted(groups, key=lambda g: (-g["max_score"], g["status"])):
        lines.append(f'| {g["status"]} | {g["max_score"]} | {", ".join(g["publishers"])} | {g["event"]} |')
    if made:
        lines += ["", "New drafts:"] + [f'- {r["status"]}: {r["draft"].get("card_headline", "")}' for r in made]
    if errors:
        lines += ["", "Errors:"] + [f"- {e[:300]}" for e in errors]
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    log(f"done: candidates={len(candidates)} groups={count} drafts={len(made)} errors={len(errors)}")
    for e in errors:
        log(f"  error: {e[:300]}")


if __name__ == "__main__":
    main()
