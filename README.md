# openai-status-tracker

Tracks the OpenAI status page for incidents and outages. Prints updates to stdout as they happen.

## Setup

```
pip install -r requirements.txt
python monitor.py
```

## How it works

Polls the status page JSON API (`/api/v2/incidents.json`) asynchronously. The response includes a page-level `updated_at` timestamp that changes whenever any incident is created or modified — when it hasn't moved, we skip parsing entirely.

Each provider runs as its own async coroutine with exponential backoff on errors (capped at 5 min). Events flow through an `asyncio.Queue` to a single consumer, which keeps output clean and makes it easy to swap the consumer for something else (webhook, database, etc.) without touching the polling logic.

On startup, only unresolved incidents are printed. After that, any new incident or status update triggers output.

## Adding providers

Any service running on statuspage.io or incident.io works. Just add the base API URL:

```python
PROVIDERS = {
    "OpenAI API": "https://status.openai.com/api/v2",
    "GitHub": "https://www.githubstatus.com/api/v2",
    "Cloudflare": "https://www.cloudflarestatus.com/api/v2",
}
```

## Deploying

It's a single Python script with one dependency. Works anywhere — a VM, Railway, Render, or a container.
