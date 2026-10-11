# earthworm-desk-fetchers

Free, off-estate fetching for the EarthWorm market desk (SICC Pty Ltd non-commercial research project, https://earthworm.sicc.au/news).

`fetch_news.py` reads public RSS/Atom feeds (RBA, APRA, NSW Planning, property news sites) and Google News RSS for a fixed set of Australian property-market queries, and writes one JSON document of headlines, links, dates and the publishers' own snippets. No article bodies, honest User-Agent, one request per second per host. Standard library only.

| Runs on | How | Cost |
|---|---|---|
| GitHub Actions | `.github/workflows/fetch-news.yml`, every 6 h, force-pushes `data/news-latest.json` to the orphan `data` branch | free (public repo) |
| AWS Lambda | `deploy-lambda.sh` → function `earthworm-news-feeds` + Function URL | always-free tier |
| The estate (CT185) | downloads the JSON (`NEWS_FEED_URLS`); SearXNG sweep only if both are down | one GET per run |

Setup: `./deploy-github.sh <github-owner>` after `gh auth login`; `BRAIN_TOKEN=... ./deploy-lambda.sh` after `aws configure`. Add new feeds in `feeds.json`.
