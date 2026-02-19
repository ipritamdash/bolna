# openai-status-tracker

Tracks status pages (OpenAI, GitHub, Cloudflare, etc.) for incidents and service degradation.

## Setup

```
pip install -r requirements.txt
python monitor.py
```

## Usage

```
python monitor.py                        # console output, default 30s interval
python monitor.py -i 60                  # poll every 60s
python monitor.py -c providers.json      # load providers from file
python monitor.py --web                  # start SSE web server on :8080
python monitor.py --web --port 3000      # custom port
```

## How it works

Two async pollers per provider:
- **Incidents** picks up new events from `/api/v2/incidents.json`
- **Components** detects when a specific product (Chat Completions, Responses, etc.) changes status

Both push into a shared `asyncio.Queue`. The consumer prints to stdout and optionally broadcasts to connected SSE clients. Swapping the output for a webhook or DB write is a one-function change.

Pollers compare `page.updated_at` across cycles to skip re-parsing when nothing changed. Errors trigger exponential backoff capped at 5 min. Startup is staggered across providers to avoid thundering herd.

The `--web` flag starts an SSE endpoint at `/events` that any browser or `curl` can subscribe to.

## Providers

Any statuspage.io or incident.io compatible service works. Pass `-c` with a JSON file:

```json
{
    "OpenAI API": "https://status.openai.com/api/v2",
    "GitHub": "https://www.githubstatus.com/api/v2",
    "Cloudflare": "https://www.cloudflarestatus.com/api/v2"
}
```

## Deploying

Single script, one dependency. Reads `PORT` from env automatically:

```
python monitor.py --web
```
