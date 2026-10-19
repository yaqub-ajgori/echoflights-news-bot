import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
ITEMS_DIR = os.path.join(BASE, "data", "items")
SCORES_DIR = os.path.join(BASE, "data", "scores")
LOG_FILE = os.path.join(BASE, "data", "logs", "filter.log")

DHAKA = timezone(timedelta(hours=6))
LOOKBACK_DAYS = 2
BATCH_SIZE = 15
SUMMARY_CHARS = 200
MIN_SCORE_KEPT = 4
MAX_RUN_SECONDS = 20 * 60

PRIMARY = {"model": "gemma-4-31b-it", "tokens_per_minute": 16_000}
FALLBACK = {"model": "gemini-3.5-flash-lite", "tokens_per_minute": 250_000}
TOKEN_BUDGET_SHARE = 0.7

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PROMPT = """You screen news headlines for EchoFlights, a Facebook page that gives Bangladeshi travellers and Bangladeshi workers abroad (mainly in Saudi Arabia, UAE, Qatar, Kuwait, Oman, Malaysia and Singapore) breaking travel and aviation news.

Score each item 0-10 for how useful it is to post for that audience today:
- 9-10: directly affects Bangladeshis travelling or living abroad right now: flight cancellations, suspensions or schedule changes on routes to or from Bangladesh; airport closures or disruptions at Dhaka, Chattogram, Sylhet or Gulf/Malaysian hubs; visa openings, bans or rule changes for Bangladeshis; work-visa, recruitment or labour-rule changes in Gulf countries, Malaysia, Singapore, Japan or Europe that affect Bangladeshi workers; embassy notices; Bangladeshis stranded, detained, deported, executed or killed abroad.
- 7-8: airline fare offers or new routes from Bangladesh; major air accidents or incidents; large aviation disruption in the Gulf, Malaysia or Singapore.
- 4-6: indirect travel or aviation news Bangladeshi travellers might care about (travel advisories, airline policy changes, big airport news in hub countries).
- 0-3: everything else: politics, sports, crime, business, entertainment, general world news, and generic "visa-free countries" lists.

Rules:
- Crime, arrests, raids and executions score 0-3 unless the item itself says Bangladeshis are involved, or it is a mass crackdown on foreign workers' residency or labour status. Never assume someone's nationality.
- Weather scores 0-3 unless it closes airports or disrupts flights.
- Items can be in English, Bangla or Arabic. Judge the meaning, not keywords.

Reply with ONLY a JSON array, no other text. Include only items scoring 4 or higher; leave out the rest. Each element:
{"id": "<item id>", "score": <0-10>, "topic": "<one of: flight_disruption, airline_offer, visa_rule, labour_rule, expat_alert, embassy_notice, accident, airport, travel_advisory, other>", "event": "<what THIS item's own headline reports, in at most 8 English words, so the same event from different sources gets the same label>"}
Judge every item on its own text only; never carry details from one item to another.
If no item scores 4 or higher, reply with []."""


def load_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def recent_days(now):
    today = now.astimezone(DHAKA).date()
    return [(today - timedelta(days=d)).isoformat() for d in range(LOOKBACK_DAYS)]


def pending_items(now):
    days = recent_days(now)
    scored = {r["id"] for d in days for r in load_jsonl(os.path.join(SCORES_DIR, d + ".jsonl"))}
    items = [i for d in days for i in load_jsonl(os.path.join(ITEMS_DIR, d + ".jsonl"))]
    seen, out = set(), []
    for i in items:
        if i["id"] not in scored and i["id"] not in seen:
            seen.add(i["id"])
            out.append(i)
    return out


def render_batch(batch):
    lines = []
    for i in batch:
        summary = i.get("summary", "")[:SUMMARY_CHARS]
        lines.append(f'id={i["id"]} | {i["source"]} ({i["region"]}, {i["lang"]}) | {i["title"]} | {summary}')
    return "\n".join(lines)


def estimate_tokens(text):
    return len(text) // 2 + 1


def call_model(model, prompt):
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }).encode("utf-8")
    req = urllib.request.Request(
        API_URL.format(model=model),
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    parts = data["candidates"][0]["content"]["parts"]
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return text, data.get("usageMetadata", {}).get("promptTokenCount")


def parse_answer(text, batch_ids):
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end < start:
        raise ValueError("no JSON array in answer: " + text[:200])
    rows = json.loads(text[start:end + 1])
    out = {}
    for r in rows:
        if not isinstance(r, dict) or r.get("id") not in batch_ids:
            continue
        try:
            score = max(0, min(10, int(r.get("score", 0))))
        except (TypeError, ValueError):
            continue
        out[r["id"]] = {
            "score": score,
            "topic": str(r.get("topic", "other"))[:30],
            "event": re.sub(r"\s+", " ", str(r.get("event", "")))[:120],
        }
    return out


def score_batch(batch):
    prompt = render_batch(batch)
    ids = {i["id"] for i in batch}
    errors = []
    for cfg in (PRIMARY, FALLBACK):
        for attempt in range(2):
            t0 = time.monotonic()
            try:
                text, prompt_tokens = call_model(cfg["model"], prompt)
                results = parse_answer(text, ids)
                return cfg["model"], results, prompt_tokens or estimate_tokens(SYSTEM_PROMPT + prompt), errors
            except urllib.error.HTTPError as e:
                errors.append(f'{cfg["model"]}: HTTP {e.code} after {time.monotonic() - t0:.0f}s {e.read()[:200]!r}')
                if e.code == 429:
                    time.sleep(60)
                elif e.code >= 500:
                    time.sleep(10)
                else:
                    break
            except Exception as e:
                errors.append(f'{cfg["model"]}: {type(e).__name__} after {time.monotonic() - t0:.0f}s: {str(e)[:200]}')
    raise RuntimeError("; ".join(errors))


def main():
    now = datetime.now(timezone.utc)
    started = time.monotonic()
    pending = pending_items(now)
    os.makedirs(SCORES_DIR, exist_ok=True)
    score_file = os.path.join(SCORES_DIR, now.astimezone(DHAKA).strftime("%Y-%m-%d") + ".jsonl")

    kept, done, failures, notes = [], 0, [], []
    window_start, window_tokens = time.monotonic(), 0
    token_ratio = 1.0

    for n in range(0, len(pending), BATCH_SIZE):
        if time.monotonic() - started > MAX_RUN_SECONDS:
            break
        batch = pending[n:n + BATCH_SIZE]

        estimate = estimate_tokens(SYSTEM_PROMPT + render_batch(batch))
        need = estimate * token_ratio
        if time.monotonic() - window_start >= 60:
            window_start, window_tokens = time.monotonic(), 0
        if window_tokens + need > PRIMARY["tokens_per_minute"] * TOKEN_BUDGET_SHARE:
            time.sleep(max(0, 60 - (time.monotonic() - window_start)) + 1)
            window_start, window_tokens = time.monotonic(), 0

        try:
            model, results, used, errors = score_batch(batch)
        except RuntimeError as e:
            failures.append(str(e))
            continue
        window_tokens += used
        if model == PRIMARY["model"]:
            token_ratio = max(1.0, used / estimate)
        notes.extend(errors)

        scored_at = datetime.now(timezone.utc).isoformat()
        with open(score_file, "a", encoding="utf-8") as f:
            for item in batch:
                r = results.get(item["id"], {"score": 0, "topic": "other", "event": ""})
                rec = {"id": item["id"], **r, "model": model, "scored": scored_at}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                if r["score"] >= MIN_SCORE_KEPT:
                    kept.append((r, item))
        done += len(batch)

    kept.sort(key=lambda x: -x[0]["score"])
    lines = [
        f"# Filter — {now:%Y-%m-%d %H:%M} UTC",
        "",
        f"Pending: {len(pending)} · scored: {done} · kept (score ≥ {MIN_SCORE_KEPT}): {len(kept)} · failed batches: {len(failures)}",
        "",
        "| Score | Topic | Source | Title | Event |",
        "|---|---|---|---|---|",
    ]
    lines += [f'| {r["score"]} | {r["topic"]} | {i["source"]} | {i["title"][:90]} | {r["event"]} |' for r, i in kept]
    if failures:
        lines += ["", "Failed batches:"] + [f"- {f[:300]}" for f in failures]
    if notes:
        lines += ["", "Errors before a successful retry or fallback:"] + [f"- {n[:300]}" for n in notes]
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"{now:%Y-%m-%d %H:%M} pending={len(pending)} scored={done} kept={len(kept)} "
                f"failed={len(failures)} errors={len(notes)}\n")
        for e in failures + notes:
            f.write(f"  {e[:300]}\n")


if __name__ == "__main__":
    main()
