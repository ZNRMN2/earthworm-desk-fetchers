"""earthworm-news-feeds - AWS Lambda wrapper around fetch_news.py.

Function URL (HTTPS, AuthType NONE; we check x-brain-token in code when
BRAIN_TOKEN is set). GET ?days=7&only=rba-media,gn:auctions -> the JSON document
fetch_news.run() produces. Results are kept in /tmp for 15 minutes so repeated
calls inside one warm container do not re-fetch the feeds.

Why Lambda: the always-free tier (1,000,000 requests + 400,000 GB-seconds per
month, perpetual) covers four calls a day many thousand times over, and the
outbound feed traffic leaves AWS, not the estate. Stateless: nothing is stored.
"""
import json
import os
import time

import fetch_news

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = fetch_news.load_config(os.path.join(HERE, "feeds.json"))
CACHE = "/tmp/news-latest.json"
CACHE_S = 900


def _resp(code, body, ctype="application/json; charset=utf-8", extra=None):
    h = {"content-type": ctype, "cache-control": "public, max-age=900"}
    if extra:
        h.update(extra)
    return {"statusCode": code, "headers": h, "body": body}


def lambda_handler(event, context):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    token = os.environ.get("BRAIN_TOKEN", "")
    if token and headers.get("x-brain-token") != token:
        return _resp(403, json.dumps({"ok": False, "error": "forbidden"}))
    qs = event.get("queryStringParameters") or {}
    try:
        days = max(1, min(14, int(qs.get("days") or 7)))
    except ValueError:
        days = 7
    only = [k for k in (qs.get("only") or "").split(",") if k] or None
    if not only and os.path.exists(CACHE) and time.time() - os.path.getmtime(CACHE) < CACHE_S:
        with open(CACHE, encoding="utf-8") as f:
            return _resp(200, f.read(), extra={"x-cache": "hit"})
    doc = fetch_news.run(CFG, days=days, only=only, log=lambda *a, **k: None)
    doc["served_by"] = "aws-lambda"
    body = json.dumps(doc, ensure_ascii=False)
    if not only:
        try:
            with open(CACHE, "w", encoding="utf-8") as f:
                f.write(body)
        except OSError:
            pass
    return _resp(200, body, extra={"x-cache": "miss"})
