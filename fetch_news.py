#!/usr/bin/env python3
"""EarthWorm market desk - off-estate headline fetcher.

Runs the same way in three places, so the estate does as little as possible:

  * GitHub Actions (free minutes on a public repo) -> orphan `data` branch,
    served as raw JSON
  * AWS Lambda (always-free tier, Function URL)    -> JSON on request
  * CT185 itself, only as the fallback when neither of the above answers

Sources are RSS / Atom / RDF feeds that publish Australian property-market
news (official regulators first) plus Google News RSS for the topic queries
the desk has always used. The output is ONE JSON document that the estate's
news_ingest.py stores: title, link, publisher, date, the publisher's own
snippet, a topic guess. Nothing else is fetched - no article bodies.

Conduct (the EarthWorm crawler rules): honest User-Agent with a contact, one
request per second per host, 15 s timeout, gzip accepted, a refusal is logged
and never retried into submission. Standard library only, Python 3.9+.

Usage:
  fetch_news.py --config feeds.json --out news-latest.json [--days 7]
                [--only rba-media,gn:auctions] [--max-items 500] [--quiet]
"""
import argparse
import datetime as _dt
import email.utils
import gzip
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

VERSION = "fetch_news.py/1.0"
CONTACT = "czinnerc@gmail.com"
UA = "EarthWormBot/1.0 (+https://earthworm.sicc.au; market desk feeds; %s)" % CONTACT
TIMEOUT_S = 15.0
GAP_S = 1.0                  # per host

JUNK_DOMAINS = ("pinterest.com", "pinterest.com.au", "facebook.com", "fb.com", "instagram.com",
                "tiktok.com", "x.com", "twitter.com", "youtube.com", "youtu.be", "linkedin.com",
                "reddit.com", "scribd.com")
TRACKING_PARAMS = ("fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "oc",
                   "ref", "source", "cmpid", "ito", "ncid", "sr_share", "spm")

# Same vocabulary gate as backend/news_ingest.py on the estate: a headline with
# none of these words is kept but hidden (hidden=1), never shown.
_RELEVANT = re.compile(
    r"\b(propert(y|ies)|housing|houses?|homes?|homeowners?|real estate|apartments?|units?|dwellings?|"
    r"rent(al|s|er|ers)?|tenants?|landlords?|mortgages?|lend(er|ers|ing)|borrowers?|auctions?|clearance|"
    r"strata|buyers?|sellers?|vendors?|developers?|development|land (value|values|tax)|stamp duty|"
    r"transfer duty|conveyanc\w*|planning|zoning|rezon\w*|construction|suburbs?|first.home|firb|"
    r"build.to.rent|cash rate|interest rates?|rate (cut|hike|rise|decision)|monetary policy|"
    r"serviceability|valuers?)\b", re.I)

# Keyword -> topic guess for feeds that carry no topic of their own. The
# estate's optional LLM pass may relabel; this keeps the page usable without it.
_TOPIC_RULES = [
    ("auctions",       r"\bauction|clearance rate"),
    ("rba",            r"\brba\b|reserve bank|cash rate|monetary policy|rate (cut|hike|rise|decision)"),
    ("apra",           r"\bapra\b|serviceability|lending (rules|standards|buffer)"),
    ("rents",          r"\brent(s|al|ers)?\b|vacancy|tenants?|landlords?"),
    ("first-home",     r"first.home|help to buy|home guarantee"),
    ("strata",         r"\bstrata\b|defects?\b|owners corporation"),
    ("firb",           r"\bfirb\b|foreign (buyers?|investment|investors)"),
    ("build-to-rent",  r"build.to.rent|\bbtr\b"),
    ("hazards",        r"\bflood|bushfire|fire.prone|hazard"),
    ("revenue-nsw",    r"stamp duty|transfer duty|land tax|revenue nsw"),
    ("land-values",    r"valuer.general|land values?"),
    ("property-law",   r"conveyanc\w*|settlement|contract of sale|cooling.off|e-conveyancing|pexa"),
    ("nsw-planning",   r"\bplanning\b|rezon\w*|zoning|\bdas?\b|development application|housing targets?|tod\b"),
    ("infrastructure", r"\bmetro\b|motorway|light rail|rail link|infrastructure"),
    ("supply",         r"building approvals|housing supply|construction|new homes|dwelling(s)? (commenced|completed)|builders?"),
    ("regional-nsw",   r"regional nsw|newcastle|wollongong|central coast|hunter|illawarra|northern rivers|riverina|orange\b|dubbo|tamworth|bathurst|wagga"),
    ("sydney-market",  r"\bsydney\b|\bnsw\b|new south wales"),
    ("federal-policy", r"federal|treasurer|albanese|ministers?|budget|negative gearing|capital gains"),
]
_TOPIC_RULES = [(t, re.compile(p, re.I)) for t, p in _TOPIC_RULES]

_last_hit = {}


def log(*a, **k):
    print(*a, file=sys.stderr, **k)


# ----------------------------------------------------------------- fetching
def _get(url, timeout=TIMEOUT_S):
    host = urllib.parse.urlsplit(url).netloc.lower()
    wait = GAP_S - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
                                               "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(4 * 1024 * 1024)
            if (r.headers.get("Content-Encoding") or "").lower() == "gzip" or data[:2] == b"\x1f\x8b":
                data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
            return data, None
    except urllib.error.HTTPError as e:
        return None, "http %s" % e.code
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:120])
    finally:
        _last_hit[host] = time.time()


# ------------------------------------------------------------------ parsing
def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _text(s):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def norm_date(s):
    """ISO-8601 or RFC 2822 -> naive UTC 'YYYY-MM-DDTHH:MM:SS', else None."""
    if not s:
        return None
    s = s.strip()
    dt = None
    try:
        dt = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(s)
        except (TypeError, ValueError, IndexError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_dt.timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def parse_feed(data):
    """RSS 2.0 <item>, Atom <entry> or RSS 1.0 (RDF) <item> -> list of dicts
    {title, link, published, summary, source, source_url}."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError("not XML (%s)" % str(e)[:60])
    kind = _local(root.tag).lower()
    if kind == "feed":
        entries = [e for e in root if _local(e.tag) == "entry"]
    elif kind == "rdf":
        entries = [e for e in root if _local(e.tag) == "item"]
    else:
        entries = [e for e in root.iter() if _local(e.tag) == "item"]
    out = []
    for e in entries:
        f = {}
        for c in e:
            name = _local(c.tag).lower()
            if name == "link":
                href = c.get("href")
                rel = (c.get("rel") or "alternate").lower()
                if href and rel == "alternate" and "link" not in f:
                    f["link"] = href.strip()
                elif c.text and "link" not in f:
                    f["link"] = c.text.strip()
            elif name == "source":
                f["source"] = _text(c.text)
                f["source_url"] = (c.get("url") or "").strip()
            elif name in ("title", "description", "summary", "content", "encoded", "pubdate", "published", "updated", "date"):
                val = c.text or ""
                if name == "content" and not val:          # Atom content with children
                    val = "".join(c.itertext())
                f.setdefault(name, val)
        title = _text(f.get("title"))
        link = (f.get("link") or "").strip()
        if not title or not link.startswith("http"):
            continue
        summary = _text(f.get("description") or f.get("summary") or f.get("encoded") or f.get("content") or "")[:600]
        published = norm_date(f.get("pubdate") or f.get("published") or f.get("date") or f.get("updated"))
        out.append({"title": title[:300], "link": link, "published": published, "summary": summary or None,
                    "source": f.get("source") or None, "source_url": f.get("source_url") or None})
    return out


# --------------------------------------------------------------- normalising
def clean_url(u):
    try:
        p = urllib.parse.urlsplit(u.strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", urllib.parse.urlencode(q), ""))


def canon_url(u):
    p = urllib.parse.urlsplit(u)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = (p.path or "/").rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", host, path, p.query, ""))


def domain_of(u):
    host = urllib.parse.urlsplit(u).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def is_junk(domain):
    return any(domain == j or domain.endswith("." + j) for j in JUNK_DOMAINS)


def relevant(title, snippet):
    return bool(_RELEVANT.search((title or "") + " " + (snippet or "")))


def guess_topic(title, snippet, default=None):
    text = (title or "") + " " + (snippet or "")
    for topic, rx in _TOPIC_RULES:
        if rx.search(text):
            return topic
    return default or "sydney-market"


def _title_key(t):
    return re.sub(r"\W+", " ", (t or "").lower()).strip()[:120]


# --------------------------------------------------------------------- run
def load_config(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run(cfg, days=7, only=None, max_items=500, log=log):
    """Fetch every configured feed and Google News query; return the document."""
    since = (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(days=max(1, int(days)))).strftime("%Y-%m-%dT%H:%M:%S")
    sources, items = [], []
    seen_canon, seen_title = set(), set()
    jobs = []
    for f in cfg.get("feeds", []):
        jobs.append(("feed", f["key"], f["url"], f))
    gn = cfg.get("google_news") or {}
    for key, spec in (gn.get("queries") or {}).items():
        q, topic = spec if isinstance(spec, (list, tuple)) else (spec, None)
        url = gn["base"].format(q=urllib.parse.quote_plus(q))
        jobs.append(("google-news", "gn:" + key, url, {"topic": topic, "publisher": None, "filter": True, "query": q}))
    if only:
        jobs = [j for j in jobs if j[1] in set(only)]
    for kind, key, url, spec in jobs:
        data, err = _get(url)
        rec = {"key": key, "kind": kind, "url": url, "ok": False, "items": 0, "kept": 0}
        if err:
            rec["error"] = err
            sources.append(rec)
            log("%-18s %s" % (key, err))
            continue
        try:
            entries = parse_feed(data)
        except ValueError as e:
            rec["error"] = str(e)
            sources.append(rec)
            log("%-18s %s" % (key, e))
            continue
        rec["ok"], rec["items"] = True, len(entries)
        kept = 0
        for en in entries:
            link = clean_url(en["link"])
            if not link:
                continue
            if kind == "google-news" and en.get("source_url"):
                dom = domain_of(en["source_url"])
                publisher = en.get("source")
            else:
                dom = domain_of(link)
                publisher = spec.get("publisher") or en.get("source") or dom
            if not dom or is_junk(dom):
                continue
            canon = canon_url(link)
            tkey = (dom, _title_key(en["title"]))
            if canon in seen_canon or tkey in seen_title:
                continue
            f_since = since
            if spec.get("days"):          # rare publishers (regulators) get a longer window
                f_since = (_dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
                           - _dt.timedelta(days=int(spec["days"]))).strftime("%Y-%m-%dT%H:%M:%S")
            if en["published"] and en["published"] < f_since:
                continue
            # Google News titles end with " - Publisher"; drop that suffix.
            title = en["title"]
            if kind == "google-news" and publisher and title.endswith(" - " + publisher):
                title = title[: -len(publisher) - 3].rstrip()
            hidden = 0 if (relevant(title, en["summary"]) or not spec.get("filter", False)) else 1
            if hidden and spec.get("drop_irrelevant"):
                continue
            seen_canon.add(canon)
            seen_title.add(tkey)
            items.append({
                "title": title, "url": link, "canon_url": canon, "source_domain": dom, "publisher": publisher,
                "published_at": en["published"], "snippet": en["summary"],
                "query": key, "topic": spec.get("topic") or guess_topic(title, en["summary"]),
                "engines": kind if kind == "google-news" else "feed:" + key, "hidden": hidden,
                "official": bool(spec.get("official")),
            })
            kept += 1
        rec["kept"] = kept
        sources.append(rec)
        log("%-18s %3d entries -> %3d kept" % (key, len(entries), kept))
    items.sort(key=lambda x: (x["published_at"] or "", x["title"]), reverse=True)
    return {"generator": VERSION, "generated_at": _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "window_days": int(days), "sources": sources, "items": items[:max_items],
            "note": "Headlines, links and the publishers' own snippets only. Public feeds, honest User-Agent, 1 request/s per host."}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "feeds.json"))
    ap.add_argument("--out", help="write JSON here (default stdout)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--only", help="comma-separated source keys (feed key or gn:<query key>)")
    ap.add_argument("--max-items", type=int, default=500)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    only = [k.strip() for k in a.only.split(",") if k.strip()] if a.only else None
    doc = run(cfg, days=a.days, only=only, max_items=a.max_items, log=(lambda *x, **k: None) if a.quiet else log)
    text = json.dumps(doc, ensure_ascii=False, indent=1)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        tmp = a.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, a.out)
        ok = sum(1 for s in doc["sources"] if s["ok"])
        log("wrote %s: %d items from %d/%d sources" % (a.out, len(doc["items"]), ok, len(doc["sources"])))
    else:
        sys.stdout.write(text)
    return 0 if any(s["ok"] for s in doc["sources"]) else 1


if __name__ == "__main__":
    sys.exit(main())
