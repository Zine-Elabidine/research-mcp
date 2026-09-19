"""Reddit via the official Data API.

Free for non-commercial use at 100 QPM with OAuth (10 without), averaged over a
rolling 10-minute window -- generous enough for real complaint mining.

The gate is registration, not rate: Reddit's Responsible Builder Policy closed
self-service app creation in late 2025, so every OAuth client is manually
approved and silent rejection happens. Register at
https://www.reddit.com/prefs/apps (type: script) before relying on this.

Why it matters despite the hassle: this is unmediated user language. A web
search scoped to reddit.com returns the threads that *rank*; this returns the
threads that *exist*, comments included.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import httpx

from .base import COMMUNITY, Provider, ProviderError, Result

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"
UA = os.environ.get("REDDIT_USER_AGENT", "research-mcp/0.1 (personal research tool)")


class Reddit(Provider):
    name = "reddit"
    source_class = COMMUNITY

    def __init__(self) -> None:
        self._id = os.environ.get("REDDIT_CLIENT_ID")
        self._secret = os.environ.get("REDDIT_CLIENT_SECRET")
        self._token: str | None = None
        self._expires = 0.0

    def available(self) -> bool:
        return bool(self._id and self._secret)

    async def _auth(self, client: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = await client.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._id or "", self._secret or ""),
            headers={"User-Agent": UA},
        )
        if r.status_code != 200:
            raise ProviderError(f"reddit auth {r.status_code}: {r.text[:200]}")
        d = r.json()
        self._token = d["access_token"]
        self._expires = time.time() + d.get("expires_in", 3600)
        return self._token

    async def search(
        self,
        query: str,
        *,
        limit: int = 20,
        since: str | None = None,
        until: str | None = None,
        subreddits: list[str] | None = None,
        sort: str = "relevance",     # relevance | hot | top | new | comments
        time_filter: str = "all",    # hour | day | week | month | year | all
    ) -> list[Result]:
        async with httpx.AsyncClient(timeout=25) as client:
            token = await self._auth(client)
            headers = {"Authorization": f"bearer {token}", "User-Agent": UA}

            paths = [f"/r/{'+'.join(subreddits)}/search"] if subreddits else ["/search"]
            params = {
                "q": query,
                "limit": min(limit, 100),
                "sort": sort,
                "t": time_filter,
                "type": "link",
                "raw_json": 1,
            }
            if subreddits:
                params["restrict_sr"] = 1

            out: list[Result] = []
            for path in paths:
                try:
                    r = await client.get(API + path, params=params, headers=headers)
                    r.raise_for_status()
                except httpx.HTTPError as e:
                    raise ProviderError(f"reddit: {e}") from e
                for child in r.json().get("data", {}).get("children", []):
                    d = child.get("data", {})
                    created = d.get("created_utc")
                    published = (
                        datetime.fromtimestamp(created, tz=timezone.utc) if created else None
                    )
                    # Client-side date bounds: the search endpoint has no since/until.
                    if published and _out_of_range(published, since, until):
                        continue
                    out.append(
                        Result(
                            source=self.name,
                            source_class=self.source_class,
                            title=d.get("title", ""),
                            url="https://www.reddit.com" + d.get("permalink", ""),
                            text=d.get("selftext", "") or "",
                            author=d.get("author"),
                            published_at=published,
                            score=d.get("score"),
                            comments=d.get("num_comments"),
                            query=query,
                            raw={"subreddit": d.get("subreddit"), "flair": d.get("link_flair_text")},
                        )
                    )
            return out


def _out_of_range(dt: datetime, since: str | None, until: str | None) -> bool:
    if since and dt < datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc):
        return True
    if until and dt > datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc):
        return True
    return False
