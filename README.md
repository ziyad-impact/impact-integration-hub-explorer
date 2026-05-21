# impact.com Integration Hub Explorer

An interactive single-page app for exploring impact.com's developer documentation
architecture — portals, API surfaces, integration patterns, tracking flows, and
the live content behind each. Built with FastAPI + Jinja, talking to the GitBook
API.

## Run it in one click — GitHub Codespaces

The simplest way to use the app, no install required:

1. From this repo on GitHub, click **`Code → Codespaces → Create codespace on main`**.
2. Wait ~60 seconds for the container to build and dependencies to install.
3. When prompted, click **Open in Browser** on the port-8001 notification.
4. Paste your personal GitBook API token into the modal that appears (see below).
5. Done — the app is running, scoped to your token.

The codespace shuts down on its own after 30 minutes of inactivity. Free GitHub
accounts get 60 codespace hours per month, far more than a tech writer will use.

## Get a GitBook API token

Each user supplies their own — no shared credential lives in the repo.

1. Sign in to [app.gitbook.com](https://app.gitbook.com).
2. Open **your avatar (bottom-left) → Settings → Developer → Personal Access Tokens**.
3. Click **Create token**, give it a name (e.g. *"Hub Explorer"*), and copy the value.
4. Paste it into the token modal the first time the app loads. It stays in your
   browser's local storage — never sent anywhere except the GitBook API.

## Local development (optional)

If you'd rather run it on your own machine:

```bash
git clone https://github.com/ziyad-impact/impact-integration-hub-explorer.git
cd impact-integration-hub-explorer
pip install -r requirements.txt
uvicorn main:app --port 8001
```

Then open <http://localhost:8001> and paste your token.

## Project structure

```
.
├── main.py                  # FastAPI routes, in-memory caching
├── gitbook_client.py        # GitBook API wrapper
├── config.py                # Settings + the portal / space ID map
├── templates/index.html     # Single-page app (HTML + CSS + JS)
├── requirements.txt
└── .devcontainer/           # Codespaces config
```
