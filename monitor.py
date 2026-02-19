#!/usr/bin/env python3

import asyncio
import random
import signal
import sys
from dataclasses import dataclass
from datetime import datetime

import aiohttp


# statuspage.io / incident.io compatible endpoints
PROVIDERS = {
    "OpenAI API": "https://status.openai.com/api/v2",
}

POLL_INTERVAL = 30
MAX_BACKOFF = 300


@dataclass
class Incident:
    provider: str
    product: str
    status: str
    timestamp: str
    impact: str


class Monitor:

    def __init__(self, providers, interval=POLL_INTERVAL):
        self.providers = providers
        self.interval = interval
        self._queue = asyncio.Queue()
        self._seen = {}
        self._page_ts = {}
        self._running = True

    async def run(self):
        conn = aiohttp.TCPConnector(limit=100, limit_per_host=6)
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(connector=conn, timeout=timeout) as session:
            tasks = [self._poll(session, name, url) for name, url in self.providers.items()]
            tasks.append(self._consume())
            await asyncio.gather(*tasks)

    async def _poll(self, session, name, base_url):
        self._seen[name] = set()
        url = f"{base_url}/incidents.json"
        backoff = self.interval
        warm = False

        # stagger startup so we don't blast all providers simultaneously
        await asyncio.sleep(random.uniform(0, min(5, self.interval)))

        while self._running:
            try:
                async with session.get(url) as resp:
                    if resp.status != 200:
                        print(f"[warn] {name}: HTTP {resp.status}", file=sys.stderr)
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, MAX_BACKOFF)
                        continue

                    data = await resp.json()
                    backoff = self.interval

                    # the page-level updated_at changes whenever any incident is
                    # created or modified, so we can skip parsing when it hasn't moved
                    ts = data.get("page", {}).get("updated_at", "")
                    if ts and ts == self._page_ts.get(name):
                        await asyncio.sleep(self.interval)
                        continue
                    self._page_ts[name] = ts

                    for inc in data.get("incidents", []):
                        self._process(name, inc, not warm)
                    warm = True

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                print(f"[warn] {name}: {e}", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
                continue

            await asyncio.sleep(self.interval)

    def _process(self, provider, inc, cold_start):
        updates = inc.get("incident_updates", [])
        tag = f"{inc['id']}:{len(updates)}"

        if tag in self._seen[provider]:
            return
        self._seen[provider].add(tag)

        if cold_start and inc.get("status") == "resolved":
            return

        product = inc.get("name", "Unknown")
        impact = inc.get("impact", "none")

        if updates:
            latest = updates[0]
            body = (latest.get("body") or "").strip()
            status_msg = body if body else latest.get("status", "unknown")
            raw_ts = latest.get("display_at") or latest.get("updated_at", "")
        else:
            status_msg = inc.get("status", "unknown")
            raw_ts = inc.get("updated_at", "")

        self._queue.put_nowait(Incident(
            provider=provider,
            product=product,
            status=status_msg,
            timestamp=_fmt_ts(raw_ts),
            impact=impact,
        ))

    async def _consume(self):
        while self._running:
            try:
                ev = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            print(f"[{ev.timestamp}] Product: {ev.provider} - {ev.product}")
            print(f"  Status: {ev.status}")
            if ev.impact and ev.impact != "none":
                print(f"  Impact: {ev.impact}")
            print()

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


def main():
    mon = Monitor(PROVIDERS)

    def _quit(s, _):
        print("\nshutting down...")
        mon.stop()

    signal.signal(signal.SIGINT, _quit)
    signal.signal(signal.SIGTERM, _quit)

    n = len(PROVIDERS)
    print(f"watching {n} provider{'s' if n != 1 else ''}, poll interval {POLL_INTERVAL}s")
    print("ctrl+c to stop\n")

    asyncio.run(mon.run())


if __name__ == "__main__":
    main()
