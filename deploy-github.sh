#!/usr/bin/env bash
# deploy-github.sh - create the PUBLIC GitHub repository, push this folder, enable the
# scheduled workflow and run it once. Needs `gh auth login` done first (Chris).
#
#   ./deploy-github.sh <github-owner>        e.g. ./deploy-github.sh czinnerc
#
# Public repository = GitHub Actions minutes are free and unlimited. Nothing in this
# folder is private: public feed URLs, a stdlib fetcher, no keys, no VG data.
set -euo pipefail
OWNER="${1:?github owner (user or org)}"
REPO="${REPO:-earthworm-desk-fetchers}"
HERE="$(cd "$(dirname "$0")" && pwd)"
command -v gh >/dev/null || { echo "gh CLI not found"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "run: gh auth login   (then re-run this script)"; exit 1; }
cd "$HERE"
[[ -d .git ]] || git init -q -b main
git add -A && git commit -qm "EarthWorm desk fetchers" || true
if ! gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  gh repo create "$OWNER/$REPO" --public --source . --remote github --push \
    --description "EarthWorm market desk: free off-estate feed fetcher (GitHub Actions + AWS Lambda). Non-commercial research, SICC Pty Ltd."
else
  git remote get-url github >/dev/null 2>&1 || git remote add github "https://github.com/$OWNER/$REPO.git"
  git push -u github main
fi
gh workflow enable fetch-news -R "$OWNER/$REPO" || true
gh workflow run fetch-news -R "$OWNER/$REPO"
echo
echo "Started. In ~2 minutes check:  gh run list -R $OWNER/$REPO -w fetch-news -L 1"
echo "Raw JSON the estate will pull:"
echo "  https://raw.githubusercontent.com/$OWNER/$REPO/data/data/news-latest.json"
echo "On CT185 put that URL first in NEWS_FEED_URLS in /etc/landgrab/news-feeds.env."
