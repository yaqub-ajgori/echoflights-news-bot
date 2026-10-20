import json
import os
import time
import urllib.error
import urllib.request

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def generate(model, system, prompt, json_mode=False, timeout=180):
    config = {"responseMimeType": "application/json"} if json_mode else {}
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    if config:
        body["generationConfig"] = config
    req = urllib.request.Request(
        API_URL.format(model=model),
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


def extract_json(text, opener="{"):
    closer = "}" if opener == "{" else "]"
    start, end = text.find(opener), text.rfind(closer)
    if start == -1 or end < start:
        raise ValueError("no JSON in answer: " + text[:200])
    return json.loads(text[start:end + 1])


def generate_json(models, system, prompt, opener="{", json_mode=False, errors=None):
    errors = errors if errors is not None else []
    for model in models:
        for _ in range(2):
            t0 = time.monotonic()
            try:
                return model, extract_json(generate(model, system, prompt, json_mode=json_mode), opener)
            except urllib.error.HTTPError as e:
                errors.append(f"{model}: HTTP {e.code} after {time.monotonic() - t0:.0f}s {e.read()[:200]!r}")
                if e.code == 429:
                    time.sleep(60)
                elif e.code >= 500:
                    time.sleep(10)
                else:
                    break
            except Exception as e:
                errors.append(f"{model}: {type(e).__name__} after {time.monotonic() - t0:.0f}s: {str(e)[:200]}")
    raise RuntimeError("; ".join(errors[-4:]))
