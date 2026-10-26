# ad-library

Ad Library microservice (:8775) + the 'Save to Psykick Studio' Chrome extension.

Part of the Psykick / home-lab microservices split (2026-10-25). Each service has its own repo, port, database, keys and Cloudflare Access app.

## Contents

- `.gitignore`
- `README.md`
- `extension/background.js`
- `extension/config.js`
- `extension/connect.js`
- `extension/finder.js`
- `extension/icon128.png`
- `extension/lastclick.js`
- `extension/manifest.json`
- `extension/popup.html`
- `extension/popup.js`
- `library_service.py`
- `test_library.py`
- `tests/ext/canon.test.js`
- `tests/ext/canon_cases.json`
- `tests/ext/fixtures/blob_nolink.html`
- `tests/ext/fixtures/facebook_feed.html`
- `tests/ext/fixtures/instagram_feed.html`
- `tests/ext/fixtures/instagram_reels.html`
- `tests/ext/fixtures/landing.html`
- `tests/ext/fixtures/meta_adlibrary.html`
- `tests/ext/fixtures/tiktok_creative_center.html`
- `tests/ext/fixtures/tiktok_explore.html`
- `tests/ext/fixtures/tiktok_foryou.html`
- `tests/ext/fixtures/tiktok_video.html`
- `tests/ext/fixtures/x_timeline.html`
- `tests/ext/fixtures/youtube_shorts.html`
- `tests/ext/test_e2e.py`
- `tests/ext/test_finder.py`

## Deploy

The server pulls `main` with a read-only deploy key and runs `sudo ai-deploy` (see ai-lab `ops/deploy.sh`). Secrets never live here: they are in `/etc/ai-secrets/` on the server.
