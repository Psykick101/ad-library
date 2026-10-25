# ad-library

Ad Library microservice (:8775) + the 'Save to Psykick Studio' Chrome extension.

Part of the Psykick / home-lab microservices split (2026-10-25). Each service has its own repo, port, database, keys and Cloudflare Access app.

## Contents

- `README.md`
- `extension/background.js`
- `extension/icon128.png`
- `extension/lastclick.js`
- `extension/manifest.json`
- `extension/popup.html`
- `extension/popup.js`
- `library_service.py`

## Deploy

The server pulls `main` with a read-only deploy key and runs `sudo ai-deploy` (see ai-lab `ops/deploy.sh`). Secrets never live here: they are in `/etc/ai-secrets/` on the server.
