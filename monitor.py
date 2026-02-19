#!/usr/bin/env python3

import argparse
import asyncio
import json
import os
import random
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp
from aiohttp import web


DEFAULT_PROVIDERS = {
    "OpenAI API": "https://status.openai.com/api/v2",
}

DEFAULT_INTERVAL = 30
MAX_BACKOFF = 300

INDEX_PAGE = """<!DOCTYPE html>
<html><head><title>Status Tracker</title></head>
<body style="margin:0;background:#0d1117;color:#c9d1d9;font-family:ui-monospace,monospace;font-size:14px">
<div style="max-width:900px;margin:0 auto;padding:24px">
<h2 style="color:#58a6ff;margin-bottom:4px">status tracker</h2>
<p id="status" style="color:#8b949e;margin-top:0">connecting...</p>
<div id="log" style="border:1px solid #30363d;border-radius:6px;padding:16px;min-height:200px"></div>
</div>
<script>
const log = document.getElementById("log");
const status = document.getElementById("status");
let count = 0;
const es = new EventSource("/events");
es.onopen = () => { status.textContent = "connected — streaming updates"; status.style.color = "#3fb950"; };
es.onmessage = e => {
    count++;
    status.textContent = count + " event" + (count === 1 ? "" : "s") + " received";
    const pre = document.createElement("pre");
    pre.style.cssText = "margin:0;padding:10px 12px;border-bottom:1px solid #21262d;white-space:pre-wrap";
    pre.textContent = e.data;
    log.prepend(pre);
};
es.onerror = () => { status.textContent = "connection lost, reconnecting..."; status.style.color = "#f85149"; };
</script></body></html>"""


@dataclass
class StatusEvent:
    provider: str
    product: str
    message: str
    timestamp: str


class Monitor:

    def __init__(self, providers, interval=DEFAULT_INTERVAL):
        self.providers = providers
        self.interval = interval
        self._queue = asyncio.Queue()
        self._seen = {}
        self._page_ts = {}
        self._comp_state = {}
        self._subscribers = []
        self._event_log = []
        self._running = True

    async def run(self, web_port=None):
        conn = aiohttp.TCPConnector(limit=100, limit_per_host=6)
        timeout = aiohttp.ClientTimeout(total=15)

        async with aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
            tasks = []
            for name, url in self.providers.items():
                tasks.append(self._poll_incidents(session, name, url))
                tasks.append(self._poll_components(session, name, url))
            tasks.append(self._consume())

            if web_port:
                tasks.append(self._serve(web_port))

            await asyncio.gather(*tasks)

    async def _poll_incidents(self, session, name, base_url):
        self._seen[name] = set()
        url = f"{base_url}/incidents.json"
        backoff = self.interval
        warm = False

        # stagger startup so we don't blast all providers at once
        await asyncio.sleep(random.uniform(0, min(5, self.interval)))

        while self._running:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        print(f"[warn] {name} incidents: HTTP {resp.status}", file=sys.stderr)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        continue

                    data = await resp.json()
                    backoff = self.interval

                    # page-level updated_at only moves when something actually changes
                    ts = data.get("page", {}).get("updated_at", "")
                    if ts and ts == self._page_ts.get(name):
                        await asyncio.sleep(self.interval)
                        continue
                    self._page_ts[name] = ts

                    incidents = data.get("incidents", [])
                    for inc in incidents:
                        self._handle_incident(name, inc, not warm)
                    warm = True

                    # cap the seen set so it doesn't grow forever
                    if len(self._seen[name]) > 200:
                        self._seen[name] = {
                            f"{i['id']}:{len(i.get('incident_updates', []))}"
                            for i in incidents
                        }

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"[warn] {name}: {e}", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

            await asyncio.sleep(self.interval)

    async def _poll_components(self, session, name, base_url):
        """Watches individual product statuses (Chat Completions, Responses, etc.)"""
        url = f"{base_url}/components.json"
        backoff = self.interval

        # offset from the incident poller
        await asyncio.sleep(random.uniform(2, min(7, self.interval)))

        while self._running:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        continue

                    data = await resp.json()
                    backoff = self.interval
                    prev = self._comp_state.get(name, {})
                    current = {}

                    for comp in data.get("components", []):
                        cid = comp["id"]
                        status = comp["status"]
                        current[cid] = status

                        # first poll just records the baseline
                        if not prev:
                            continue

                        old = prev.get(cid)
                        if old and old != status:
                            self._queue.put_nowait(StatusEvent(
                                provider=name,
                                product=comp["name"],
                                message=status.replace("_", " "),
                                timestamp=_fmt_ts(comp.get("updated_at", "")),
                            ))

                    self._comp_state[name] = current

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"[warn] {name} components: {e}", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

            await asyncio.sleep(self.interval)

    def _handle_incident(self, provider, inc, cold_start):
        updates = inc.get("incident_updates", [])
        tag = f"{inc['id']}:{len(updates)}"

        if tag in self._seen[provider]:
            return
        self._seen[provider].add(tag)

        if cold_start and inc.get("status") == "resolved":
            return

        if updates:
            latest = updates[0]
            body = (latest.get("body") or "").strip()
            msg = body if body else latest.get("status", "unknown")
            raw_ts = latest.get("display_at") or latest.get("updated_at", "")
        else:
            msg = inc.get("status", "unknown")
            raw_ts = inc.get("updated_at", "")

        self._queue.put_nowait(StatusEvent(
            provider=provider,
            product=inc.get("name", "Unknown"),
            message=msg,
            timestamp=_fmt_ts(raw_ts),
        ))

    async def _consume(self):
        while self._running:
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            line = f"[{ev.timestamp}] Product: {ev.provider} - {ev.product}\n  Status: {ev.message}"
            print(line)
            print()

            # buffer for SSE replay to late-joining clients
            self._event_log.append(line)
            if len(self._event_log) > 50:
                self._event_log = self._event_log[-50:]

            for sub in list(self._subscribers):
                try:
                    sub.put_nowait(line)
                except asyncio.QueueFull:
                    pass

    async def _serve(self, port):
        app = web.Application()
        app.router.add_get("/", self._web_index)
        app.router.add_get("/events", self._web_sse)
        app.router.add_get("/health", self._web_health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        print(f"web ui: http://0.0.0.0:{port}\n")

    async def _web_index(self, req):
        return web.Response(text=INDEX_PAGE, content_type="text/html")

    async def _web_health(self, req):
        return web.json_response({
            "status": "ok",
            "providers": len(self.providers),
            "events_buffered": len(self._event_log),
        })

    async def _web_sse(self, req):
        resp = web.StreamResponse()
        resp.headers["Content-Type"] = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["X-Accel-Buffering"] = "no"
        await resp.prepare(req)

        # replay recent events so late-joining clients aren't staring at a blank page
        for old in self._event_log:
            await resp.write(f"data: {old}\n\n".encode())

        q = asyncio.Queue(maxsize=50)
        self._subscribers.append(q)
        try:
            while True:
                data = await q.get()
                await resp.write(f"data: {data}\n\n".encode())
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._subscribers.remove(q)
        return resp

    def stop(self):
        self._running = False


def _fmt_ts(raw):
    if not raw:
        return "unknown"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw


def load_providers(path):
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("expected JSON object mapping provider names to base URLs")
    return data


def main():
    ap = argparse.ArgumentParser(description="track status page incidents")
    ap.add_argument("-i", "--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument("-c", "--config", help="JSON file with provider name -> URL mappings")
    ap.add_argument("--web", action="store_true", help="start SSE web server")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    args = ap.parse_args()

    providers = load_providers(args.config) if args.config else DEFAULT_PROVIDERS
    mon = Monitor(providers, interval=args.interval)

    def _quit(s, _):
        print("\nshutting down...")
        mon.stop()

    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)

    n = len(providers)
    print(f"watching {n} provider{'s' if n != 1 else ''}, poll interval {args.interval}s")
    print("ctrl+c to stop\n")

    asyncio.run(mon.run(web_port=args.port if args.web else None))


if __name__ == "__main__":
    main()
